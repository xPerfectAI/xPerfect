"""Process-local wake hints. Persisted run/request/transcript data remains authoritative."""

from __future__ import annotations

import asyncio
import time
from threading import Lock


class RunNotifications:
    def __init__(self) -> None:
        self._lock = Lock()
        self._subscribers: dict[str, set[RunSubscription]] = {}

    def subscribe(self) -> RunSubscription:
        return RunSubscription(self, asyncio.get_running_loop())

    def notify(self, run_id: str) -> None:
        with self._lock:
            subscribers = []
            for item in self._subscribers.get(run_id, ()):
                # One queued callback or set event already covers every durable write until
                # the consumer starts its next read. Coalesce without adding a timer/delay.
                if item._pending_generation == item.generation or item.event.is_set():
                    if item.metrics is not None:
                        item.metrics["coalescedHints"] += 1
                    continue
                item._pending_generation = item.generation
                subscribers.append((item, item.generation))
        for subscriber, generation in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(
                    subscriber._notify_if_current, run_id, generation
                )
            except RuntimeError:
                # A disconnected consumer can finish closing its event loop concurrently.
                subscriber.close()


class RunSubscription:
    def __init__(self, owner: RunNotifications, loop: asyncio.AbstractEventLoop) -> None:
        self.owner = owner
        self.loop = loop
        self.event = asyncio.Event()
        self.run_id = ""
        self.generation = 0
        self._pending_generation: int | None = None
        self.metrics: dict[str, float] | None = None

    def _notify_if_current(self, run_id: str, generation: int) -> None:
        # A queued hint can outlive a fallback switch or disconnect, including A→B→A.
        with self.owner._lock:
            if self._pending_generation == generation:
                self._pending_generation = None
            if self.run_id == run_id and self.generation == generation:
                self.event.set()
                if self.metrics is not None:
                    self.metrics["deliveredHints"] += 1

    def observe(self, run_id: str) -> None:
        """Subscribe and clear BEFORE reading durable state, preventing lost wakeups."""
        with self.owner._lock:
            if self.run_id != run_id:
                self._remove_locked()
                self.generation += 1
                self._pending_generation = None
                self.run_id = run_id
                if run_id:
                    self.owner._subscribers.setdefault(run_id, set()).add(self)
            self.event.clear()

    async def wait(self, *, timeout: float = 0.2) -> None:
        # Other processes, missed file notifications, deadlines and cancellation still reconcile.
        started = time.monotonic() if self.metrics is not None else 0.0
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout)
            if self.metrics is not None:
                self.metrics["hintWokenWaits"] += 1
        except TimeoutError:
            if self.metrics is not None:
                self.metrics["timedOutWaits"] += 1
        finally:
            if self.metrics is not None:
                self.metrics["waitMs"] += (time.monotonic() - started) * 1000

    def _remove_locked(self) -> None:
        subscribers = self.owner._subscribers.get(self.run_id)
        if subscribers is not None:
            subscribers.discard(self)
            if not subscribers:
                self.owner._subscribers.pop(self.run_id, None)

    def close(self) -> None:
        with self.owner._lock:
            self._remove_locked()
            self.run_id = ""
            self.generation += 1
            self._pending_generation = None
