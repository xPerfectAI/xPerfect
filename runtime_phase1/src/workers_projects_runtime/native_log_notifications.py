"""Optional OS file-change wakeup; native transcript reads still own data and identity."""

from __future__ import annotations

import os
from pathlib import Path
import select


class NativeLogNotifications:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._queue = select.kqueue() if hasattr(select, "kqueue") else None
        except OSError:
            self._queue = None
        self._fd: int | None = None
        self._identity: tuple[int, int] | None = None

    def prepare(self) -> None:
        """Arm before the read so a write between reading and waiting cannot be lost."""
        if self._queue is None:
            return
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self._fd is not None and identity == self._identity:
                return
            self._close_file()
            self._fd = os.open(self.path, os.O_RDONLY)
            stat = os.fstat(self._fd)
            self._identity = (stat.st_dev, stat.st_ino)
            self._queue.control([select.kevent(
                self._fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=(select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                        | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE),
            )], 0, 0)
        except OSError:
            # File creation/rotation or an unsupported filesystem reconciles on the timeout.
            self._close_file()

    def wait(self, stop_event, timeout: float = 0.05) -> None:
        if stop_event.is_set():
            return
        if self._queue is None or self._fd is None:
            stop_event.wait(timeout)
            return
        try:
            self._queue.control(None, 1, timeout)
        except OSError:
            self._close_file()
            stop_event.wait(timeout)

    def _close_file(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self._identity = None

    def close(self) -> None:
        self._close_file()
        if self._queue is not None:
            self._queue.close()
            self._queue = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
