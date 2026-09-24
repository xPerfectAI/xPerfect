from __future__ import annotations

import gc
import sqlite3
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


def _service_background_threads(service) -> tuple[Thread, ...]:
    return tuple(
        thread
        for thread in (
            service._startup_recovery_thread,
            service._callback_retry_thread,
            service._idle_reaper_thread,
            service._scheduler_thread,
            service._host_lease_heartbeat_thread,
            service._isolated_readiness_thread,
        )
        if thread is not None
    )


def _sqlite_sidecars(db_path: Path) -> tuple[Path, Path]:
    return Path(f"{db_path}-wal"), Path(f"{db_path}-shm")


def _assert_connection_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def _create_worker(store: Store) -> tuple[dict, dict]:
    project = store.create_project(
        owner_id="owner",
        title="SQLite lifetime",
        goal="Keep runtime state available",
        default_worker_profile="codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Lifetime worker",
        role="exercise concurrent persistence",
        profile="codex-cli",
        backend="stub",
        runtime="stub",
        model="test-model",
    )
    return project, worker


def test_store_keeps_wal_sidecars_for_its_explicit_lifetime(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    wal_path, shm_path = _sqlite_sidecars(db_path)
    store = Store(str(db_path))

    assert wal_path.is_file()
    assert shm_path.is_file()

    project, _worker = _create_worker(store)
    assert store.get_project(project["project_id"]) is not None
    assert wal_path.is_file()
    assert shm_path.is_file()
    keeper = store._lifetime_connection
    assert keeper is not None

    store.close()
    store.close()

    assert store._lifetime_connection is None
    _assert_connection_closed(keeper)


def test_api_service_background_threads_are_owned_by_lifespan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "true")
    baseline_thread_ids = {
        thread.ident for thread in threading.enumerate() if thread.ident is not None
    }
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    service = app.state.service

    assert _service_background_threads(service) == ()
    assert {
        thread.ident
        for thread in threading.enumerate()
        if thread.ident is not None and thread.ident not in baseline_thread_ids
    } == set()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        owned_threads = _service_background_threads(service)
        assert owned_threads
        assert all(thread.is_alive() for thread in owned_threads)

    assert all(not thread.is_alive() for thread in owned_threads)
    assert {
        thread.ident
        for thread in threading.enumerate()
        if thread.ident is not None and thread.ident not in baseline_thread_ids
    } == set()


def test_short_lived_store_finalizer_closes_forgotten_keeper(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "runtime.db"))
    keeper = store._lifetime_connection
    store_reference = weakref.ref(store)
    assert keeper is not None

    del store
    gc.collect()

    assert store_reference() is None
    _assert_connection_closed(keeper)


def test_api_accepts_health_while_owned_worker_recovery_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "true")
    entered = Event()
    release = Event()
    ready = Event()
    calls: list[str] = []
    errors: list[BaseException] = []

    def reconcile(_service) -> None:
        calls.append("workers")
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(WorkersProjectsService, "reconcile_all_workers", reconcile)
    monkeypatch.setattr(
        WorkersProjectsService,
        "reconcile_host_run_leases",
        lambda _service: calls.append("leases"),
    )
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=True,
    )

    def serve() -> None:
        try:
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200
                ready.set()
                release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(target=serve)
    owner.start()
    try:
        assert entered.wait(timeout=2)
        assert ready.wait(timeout=1), "retained worker recovery blocked API startup"
        assert calls == ["leases", "workers"]
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()
    assert not errors
    assert not app.state.service._startup_recovery_thread.is_alive()


def test_worker_reconciliation_stops_between_rows_on_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    seen: list[str] = []
    monkeypatch.setattr(store, "reconcile_invalid_running_runs", lambda: None)
    monkeypatch.setattr(service, "reconcile_terminal_artifact_observations", lambda: None)
    monkeypatch.setattr(store, "list_all_workers", lambda: [{"worker_id": "first"}, {"worker_id": "next"}])

    def reconcile(worker) -> None:
        seen.append(worker["worker_id"])
        service._shutdown_event.set()

    monkeypatch.setattr(service, "_reconcile_worker_row", reconcile)
    try:
        service.reconcile_all_workers()
        assert seen == ["first"]
    finally:
        service.shutdown()
        store.close()


def test_store_keeper_survives_concurrent_event_poll_reads_and_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    wal_path, shm_path = _sqlite_sidecars(db_path)
    store = Store(str(db_path))
    project, worker = _create_worker(store)
    start = Event()
    writes_complete = Event()
    observed_event_counts: list[int] = []
    missing_sidecars: list[tuple[bool, bool]] = []

    def write_events() -> None:
        start.wait()
        try:
            for index in range(20):
                store.add_event(
                    project["project_id"],
                    worker["worker_id"],
                    None,
                    "worker.progress",
                    f"synthetic event {index}",
                    tenant_id="local",
                )
        finally:
            writes_complete.set()

    def read_project() -> None:
        start.wait()
        for _ in range(20):
            assert store.get_project(project["project_id"])["title"] == "SQLite lifetime"

    def poll_event_stream() -> None:
        start.wait()
        while not writes_complete.is_set():
            observed_event_counts.append(len(store.list_events(worker["worker_id"])))
            time.sleep(0.001)
        observed_event_counts.append(len(store.list_events(worker["worker_id"])))

    def observe_lifetime() -> None:
        start.wait()
        while not writes_complete.is_set():
            if not wal_path.is_file() or not shm_path.is_file():
                missing_sidecars.append((wal_path.exists(), shm_path.exists()))
            time.sleep(0.001)

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(write_events),
                executor.submit(read_project),
                executor.submit(poll_event_stream),
                executor.submit(observe_lifetime),
            ]
            start.set()
            for future in futures:
                future.result(timeout=30)

        events = store.list_events(worker["worker_id"])
        assert len(events) == 21
        assert observed_event_counts == sorted(observed_event_counts)
        assert not missing_sidecars
        assert wal_path.is_file()
        assert shm_path.is_file()
    finally:
        store.close()


def test_api_lifespan_closes_store_keeper_after_service_shutdown(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    wal_path, shm_path = _sqlite_sidecars(db_path)
    app = create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime())
    keeper = app.state.store._lifetime_connection
    shutdown_order: list[str] = []
    assert keeper is not None

    provider_shutdown = app.state.provider_setup.shutdown
    conversation_shutdown = getattr(app.state.conversation_provider, "shutdown", lambda: None)
    service_shutdown = app.state.service.shutdown
    store_close = app.state.store.close

    def record_provider_shutdown() -> None:
        shutdown_order.append("provider")
        provider_shutdown()

    def record_service_shutdown() -> None:
        shutdown_order.append("service")
        service_shutdown()

    def record_conversation_shutdown() -> None:
        shutdown_order.append("conversation")
        conversation_shutdown()

    def record_store_close() -> None:
        shutdown_order.append("store")
        store_close()

    app.state.provider_setup.shutdown = record_provider_shutdown
    app.state.conversation_provider.shutdown = record_conversation_shutdown
    app.state.service.shutdown = record_service_shutdown
    app.state.store.close = record_store_close

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert wal_path.is_file()
        assert shm_path.is_file()

    assert shutdown_order == ["provider", "conversation", "service", "store"]
    assert app.state.store._lifetime_connection is None
    _assert_connection_closed(keeper)


def test_conversation_provider_shutdown_waits_and_cannot_restart_reconciliation(
    tmp_path: Path,
) -> None:
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    provider = app.state.conversation_provider
    store = app.state.store
    reconciliation_started = Event()
    release_reconciliation = Event()
    shutdown_complete = Event()
    shutdown_errors: list[BaseException] = []

    def blocking_reconciliation(_request_id: str) -> bool:
        reconciliation_started.set()
        release_reconciliation.wait(timeout=5)
        store.get_project("synthetic-missing-project")
        return False

    def shut_down_provider() -> None:
        try:
            provider.shutdown()
        except BaseException as error:
            shutdown_errors.append(error)
        finally:
            shutdown_complete.set()

    provider._reconcile_detached_request_once = blocking_reconciliation
    provider._ensure_detached_reconciliation("synthetic-blocked-request")
    assert reconciliation_started.wait(timeout=2)
    shutdown_thread = Thread(target=shut_down_provider)
    shutdown_thread.start()

    try:
        assert not shutdown_complete.wait(timeout=0.1)
        assert store._lifetime_connection is not None
    finally:
        release_reconciliation.set()
        shutdown_thread.join(timeout=5)
        with provider._detached_reconciliation_lock:
            provider._detached_reconciliations.clear()
        reconciliation_thread = provider._detached_reconciliation_thread
        if reconciliation_thread is not None:
            reconciliation_thread.join(timeout=5)

    assert not shutdown_errors
    assert not shutdown_thread.is_alive()
    assert provider._detached_reconciliation_thread is None

    provider.shutdown()
    provider._ensure_detached_reconciliation("synthetic-after-shutdown")

    assert provider._detached_reconciliation_thread is None
    assert "synthetic-after-shutdown" not in provider._detached_reconciliations
    app.state.service.shutdown()
    store.close()


def test_api_shutdown_prevents_detached_store_calls_after_keeper_close(
    tmp_path: Path,
) -> None:
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    provider = app.state.conversation_provider
    store = app.state.store
    reconciliation_started = Event()
    observed_closed_keeper: list[bool] = []

    def observe_store_lifetime(_request_id: str) -> bool:
        keeper_closed = store._lifetime_connection is None
        observed_closed_keeper.append(keeper_closed)
        store.get_project("synthetic-missing-project")
        reconciliation_started.set()
        return not keeper_closed

    provider._reconcile_detached_request_once = observe_store_lifetime

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        provider._ensure_detached_reconciliation("synthetic-lifetime-request")
        assert reconciliation_started.wait(timeout=2)

    reconciliation_thread = provider._detached_reconciliation_thread
    if reconciliation_thread is not None:
        reconciliation_thread.join(timeout=2)

    assert observed_closed_keeper
    assert True not in observed_closed_keeper
    assert reconciliation_thread is None or not reconciliation_thread.is_alive()


def test_conversation_provider_shutdown_timeout_keeps_store_open(tmp_path: Path) -> None:
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    provider = app.state.conversation_provider
    store = app.state.store
    reconciliation_started = Event()
    release_reconciliation = Event()

    def blocking_reconciliation(_request_id: str) -> bool:
        reconciliation_started.set()
        release_reconciliation.wait(timeout=5)
        return False

    provider._reconcile_detached_request_once = blocking_reconciliation
    provider._ensure_detached_reconciliation("synthetic-timeout-request")
    assert reconciliation_started.wait(timeout=2)

    try:
        with pytest.raises(RuntimeError, match="detached reconciliation did not stop"):
            provider.shutdown(timeout_seconds=0.01)

        assert store._lifetime_connection is not None
        assert provider._detached_reconciliation_thread is not None
        assert provider._detached_reconciliation_thread.is_alive()
    finally:
        release_reconciliation.set()
        provider.shutdown(timeout_seconds=2)
        app.state.service.shutdown()
        store.close()


def test_concurrent_reconciliation_start_is_published_before_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    provider = app.state.conversation_provider
    store = app.state.store
    original_thread_start = Thread.start
    first_start_entered = Event()
    release_first_start = Event()
    second_ensure_entered = Event()
    second_ensure_complete = Event()
    shutdown_complete = Event()
    start_calls: list[Thread] = []
    post_close_store_calls: list[str] = []
    reconciliation_start_count = 0

    def delayed_first_start(thread: Thread) -> None:
        nonlocal reconciliation_start_count
        start_calls.append(thread)
        if thread.name == "glasshive-provider-reconcile":
            reconciliation_start_count += 1
            if reconciliation_start_count == 1:
                first_start_entered.set()
                assert release_first_start.wait(timeout=5)
        original_thread_start(thread)

    def observe_store_lifetime(request_id: str) -> bool:
        if store._lifetime_connection is None:
            post_close_store_calls.append(request_id)
        store.get_project("synthetic-missing-project")
        return False

    monkeypatch.setattr(Thread, "start", delayed_first_start)
    provider._reconcile_detached_request_once = observe_store_lifetime
    first_ensure = Thread(
        target=provider._ensure_detached_reconciliation,
        args=("synthetic-concurrent-one",),
    )
    shutdown_errors: list[BaseException] = []

    def second_reconciliation_request() -> None:
        second_ensure_entered.set()
        provider._ensure_detached_reconciliation("synthetic-concurrent-two")
        second_ensure_complete.set()

    def shut_down_provider() -> None:
        try:
            provider.shutdown()
            app.state.service.shutdown()
            store.close()
        except BaseException as error:
            shutdown_errors.append(error)
        finally:
            shutdown_complete.set()

    second_ensure = Thread(target=second_reconciliation_request)
    shutdown_thread = Thread(target=shut_down_provider)

    second_ensure_blocked = False
    shutdown_blocked = False
    first_ensure.start()
    try:
        assert first_start_entered.wait(timeout=2)
        second_ensure.start()
        assert second_ensure_entered.wait(timeout=2)
        second_ensure_blocked = not second_ensure_complete.wait(timeout=0.2)
        shutdown_thread.start()
        shutdown_blocked = not shutdown_complete.wait(timeout=0.2)
    finally:
        release_first_start.set()
        for thread in (first_ensure, second_ensure, shutdown_thread):
            if thread.ident is not None:
                thread.join(timeout=5)

    # Both callers must wait until the one assigned reconciliation thread is started.
    assert second_ensure_blocked
    assert shutdown_blocked
    reconciliation_thread = provider._detached_reconciliation_thread
    if reconciliation_thread is not None and reconciliation_thread.ident is not None:
        reconciliation_thread.join(timeout=5)

    assert not shutdown_errors
    assert not first_ensure.is_alive()
    assert not second_ensure.is_alive()
    assert not shutdown_thread.is_alive()
    reconciliation_starts = [
        thread for thread in start_calls if thread.name == "glasshive-provider-reconcile"
    ]
    assert len(reconciliation_starts) == 1
    assert provider._detached_reconciliation_thread is None
    assert not post_close_store_calls
    assert store._lifetime_connection is None


def test_api_shutdown_leaves_keeper_open_when_provider_cannot_stop(tmp_path: Path) -> None:
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    store = app.state.store
    original_conversation_shutdown = getattr(app.state.conversation_provider, "shutdown", None)
    original_store_close = store.close
    store_close_called = False

    def refuse_shutdown() -> None:
        raise RuntimeError("synthetic reconciliation did not stop")

    def record_store_close() -> None:
        nonlocal store_close_called
        store_close_called = True
        original_store_close()

    app.state.conversation_provider.shutdown = refuse_shutdown
    app.state.store.close = record_store_close

    try:
        with pytest.raises(RuntimeError, match="synthetic reconciliation did not stop"):
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200

        assert not store_close_called
        assert store._lifetime_connection is not None
    finally:
        if original_conversation_shutdown is not None:
            app.state.conversation_provider.shutdown = original_conversation_shutdown
            original_conversation_shutdown()
        app.state.service.shutdown()
        original_store_close()


def test_api_shutdown_keeps_store_open_when_service_loop_cannot_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    service = app.state.service
    store = app.state.store
    keeper = store._lifetime_connection
    callback_started = Event()
    release_callback = Event()
    callback_store_calls_after_close: list[str] = []
    cooperative_loops_stopped = [Event(), Event()]
    store_close_called = False

    assert keeper is not None

    def blocked_callback_loop() -> None:
        callback_started.set()
        service._shutdown_event.wait(timeout=5)
        release_callback.wait(timeout=5)
        if store._lifetime_connection is None:
            callback_store_calls_after_close.append("callback")
        store.get_project("synthetic-missing-project")

    def cooperative_loop(stopped: Event) -> None:
        service._shutdown_event.wait(timeout=5)
        stopped.set()

    callback_thread = Thread(
        target=blocked_callback_loop,
        daemon=True,
        name="wpr-callback-retry",
    )
    idle_thread = Thread(
        target=cooperative_loop,
        args=(cooperative_loops_stopped[0],),
        daemon=True,
        name="wpr-idle-reaper",
    )
    scheduler_thread = Thread(
        target=cooperative_loop,
        args=(cooperative_loops_stopped[1],),
        daemon=True,
        name="wpr-scheduler",
    )
    service._callback_retry_thread = callback_thread
    service._idle_reaper_thread = idle_thread
    service._scheduler_thread = scheduler_thread
    for thread in (callback_thread, idle_thread, scheduler_thread):
        thread.start()
    assert callback_started.wait(timeout=2)

    original_service_shutdown = service.shutdown
    original_store_close = store.close

    def bounded_service_shutdown() -> None:
        original_service_shutdown(timeout_seconds=0.05)

    def record_store_close() -> None:
        nonlocal store_close_called
        store_close_called = True
        original_store_close()

    service.shutdown = bounded_service_shutdown
    store.close = record_store_close
    shutdown_error: BaseException | None = None

    try:
        try:
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200
        except BaseException as error:
            shutdown_error = error

        assert isinstance(shutdown_error, RuntimeError)
        assert "background loops did not stop" in str(shutdown_error)
        assert "wpr-callback-retry" in str(shutdown_error)
        assert service._shutdown_event.is_set()
        assert all(stopped.wait(timeout=1) for stopped in cooperative_loops_stopped)
        assert callback_thread.is_alive()
        assert not idle_thread.is_alive()
        assert not scheduler_thread.is_alive()
        assert not store_close_called
        assert store._lifetime_connection is keeper
    finally:
        service._shutdown_event.set()
        release_callback.set()
        for thread in (callback_thread, idle_thread, scheduler_thread):
            thread.join(timeout=2)
        service.shutdown = original_service_shutdown
        store.close = original_store_close
        original_service_shutdown(timeout_seconds=2)
        original_store_close()

    assert not callback_store_calls_after_close
