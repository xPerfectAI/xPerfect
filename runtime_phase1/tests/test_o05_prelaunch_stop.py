"""O05: prelaunch failure settlement and truthful Stop around host startup."""
from __future__ import annotations

import pytest

import workers_projects_runtime.service as service_module
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, StubRuntime
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store

from test_host_run_leases import _active_worker_and_run, _admit_and_invoke, _running_host_run


@pytest.fixture(autouse=True)
def _synthetic_healthy_storage_probe(monkeypatch):
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: service_module.shutil._ntuple_diskusage(100, 50, 50),
    )


class _RecordingRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.started = []

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        self.started.append(run_id)
        return super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)


def _processor(service, worker_id, generation=7):
    with service._processors_lock:
        service._active_processors.add(worker_id)
        service._processor_generations[worker_id] = generation
    return generation


def _exact_lease(store, service):
    def acquire(worker_row, run_row):
        return store.acquire_host_run_lease(
            runtime_family="codex", lane="mission", tenant_id="local", owner_id="owner-a",
            worker_id=worker_row["worker_id"], run_id=run_row["run_id"],
            executor_id=service.executor_id, conversation_limit=2, mission_limit=3,
            account_mission_limit=4, tenant_mission_limit=12, lease_ttl_s=30,
        )
    return acquire


def test_prelaunch_projection_failure_settles_the_exact_invoked_generation(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "prelaunch.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "prelaunch", run_state="queued")
    store.update_worker_state(worker["worker_id"], "ready")
    runtime = _RecordingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    monkeypatch.setattr(service, "_acquire_host_run_lease", _exact_lease(store, service))
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    window = {}

    def failing_projection(worker_row, run_row):
        # The window between the durable invocation mark and adapter dispatch.
        window["invoked_at"] = (store.get_run(run_row["run_id"]) or {}).get("runtime_invoked_at")
        raise ValueError("Native coordinator endpoint must be an HTTP(S) origin")

    monkeypatch.setattr(service.peers, "project_native_tools", failing_projection)
    try:
        service._process_worker_queue(worker["worker_id"], _processor(service, worker["worker_id"]))
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"])
    assert window["invoked_at"]
    assert durable["state"] == "failed", durable["state"]
    assert "HTTP(S) origin" in durable["error_text"]
    assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
    assert runtime.started == []
    events = [event["event_type"] for event in store.list_events(worker["worker_id"])]
    assert "run.failed" in events and "run.late_completion_ignored" not in events
    assert (store.get_worker(worker["worker_id"]) or {})["state"] == "ready"


def test_after_a_prelaunch_failure_the_owner_can_send_again(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "prelaunch-retry.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "prelaunch-retry", run_state="queued")
    store.update_worker_state(worker["worker_id"], "ready")
    runtime = _RecordingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    monkeypatch.setattr(service, "_acquire_host_run_lease", _exact_lease(store, service))
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    original = service.peers.project_native_tools
    failures = iter([ValueError("transient projection failure")])

    def fail_once(worker_row, run_row):
        failure = next(failures, None)
        if failure:
            raise failure
        return original(worker_row, run_row)

    monkeypatch.setattr(service.peers, "project_native_tools", fail_once)
    try:
        service._process_worker_queue(worker["worker_id"], _processor(service, worker["worker_id"]))
        assert store.get_run(run["run_id"])["state"] == "failed"
        # Stop after terminal is a truthful no-op, not an error.
        service.cancel_run(worker["worker_id"], run["run_id"])
        retry = service.assign_run(worker["worker_id"], "Try again", start_processor=False)
        service._process_worker_queue(worker["worker_id"], _processor(service, worker["worker_id"], 8))
    finally:
        service.shutdown()
    assert store.get_run(run["run_id"])["state"] == "failed"
    assert store.get_run(retry["run_id"])["state"] == "completed"
    assert runtime.started == [retry["run_id"]]


def _host_service(store, tmp_path, runtime=None):
    runtime = runtime or HostCodexCliRuntime(base_dir=str(tmp_path / "host-runtime"))
    return WorkersProjectsService(store, runtime, reconcile_on_startup=False)


def _invoked_host_run(store, service, suffix, *, confirmed=False):
    worker, run = _running_host_run(store, suffix)
    lease = store.acquire_host_run_lease(
        runtime_family="codex", lane="mission", tenant_id="tenant-a", owner_id="owner-a",
        worker_id=worker["worker_id"], run_id=run["run_id"], executor_id=service.executor_id,
        conversation_limit=2, mission_limit=3, account_mission_limit=4,
        tenant_mission_limit=12, lease_ttl_s=30,
    )
    run = _admit_and_invoke(store, run, lease)
    store.update_worker_state(worker["worker_id"], "running")
    if confirmed:
        assert _confirm(store, worker, run, lease, service.executor_id) is not None
    return store.get_worker(worker["worker_id"]), store.get_run(run["run_id"]), lease


def _confirm(store, worker, run, lease, executor_id, pid=4242):
    return store.confirm_host_run_start(
        worker_id=worker["worker_id"], run_id=run["run_id"],
        run_started_at=str(run["runtime_invoked_at"]), lease_id=lease["lease_id"],
        startup_token=lease["startup_token"], executor_id=executor_id,
        identity_kind="host_process", pid=pid, process_group=pid,
        process_start_identity=f"start-{pid}", container_id="", session_id=f"session-{pid}",
    )


def test_stop_before_any_process_started_cancels_the_exact_pending_run(tmp_path):
    store = Store(str(tmp_path / "prestart-stop.sqlite3"))
    service = _host_service(store, tmp_path)
    worker, run, lease = _invoked_host_run(store, service, "prestart-stop")
    assert store.get_host_run_lease(lease["lease_id"])["startup_state"] == "reserved"
    try:
        # O05: this raised "The exact host process identity is not confirmed"
        # after durably cancelling the run.
        service.cancel_run(worker["worker_id"], run["run_id"])
        durable = store.get_run(run["run_id"])
        assert durable["state"] == "cancelled"
        assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
        events = [event["event_type"] for event in store.list_events(worker["worker_id"])]
        assert "run.cancelled" in events
        assert "run.interrupted" not in events
        assert not (store.get_worker(worker["worker_id"]) or {}).get("compute_release_token")
        # A start callback that arrives after Stop can no longer claim the run.
        assert _confirm(store, worker, run, lease, service.executor_id) is None
        assert store.get_run(run["run_id"])["state"] == "cancelled"
        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "ready"
    finally:
        service.shutdown()


class _ExactHostRuntime(StubRuntime):
    requires_run_start_identity = True

    def __init__(self):
        super().__init__()
        self.interrupted = []

    def interrupt_worker(self, worker, run_id=None):
        self.interrupted.append((run_id, dict(worker.get("_host_run_lease") or {})))
        return RuntimeInfo(
            runtime="codex-cli", model="test", gateway_url="", gateway_port=None,
            gateway_token=None, session_key=None, state_dir="/synthetic/state",
            workspace_dir="/synthetic/workspace", pid=None,
        )


def test_stop_of_a_live_host_process_signals_only_its_exact_generation(tmp_path):
    store = Store(str(tmp_path / "live-stop.sqlite3"))
    runtime = _ExactHostRuntime()
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "live-stop", confirmed=True)
    try:
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    assert len(runtime.interrupted) == 1
    run_id, exact_lease = runtime.interrupted[0]
    assert run_id == run["run_id"]
    assert exact_lease["lease_id"] == lease["lease_id"]
    assert exact_lease["startup_state"] == "confirmed" and int(exact_lease["pid"]) == 4242
    assert store.get_run(run["run_id"])["state"] == "cancelled"
    assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
    # One terminal outcome: the Stop fence publishes no interrupt events or callbacks.
    events = [event["event_type"] for event in store.list_events(worker["worker_id"])]
    assert events.count("run.cancelled") == 1
    assert "run.interrupted" not in events and "worker.interrupted" not in events
    settled = store.get_worker(worker["worker_id"])
    assert settled["state"] == "ready" and not settled.get("compute_release_token")


def test_live_process_without_exact_identity_is_not_reported_stopped(tmp_path):
    """The exact-host guard stays: a confirmed PID is never cancelled blindly."""

    store = Store(str(tmp_path / "live-guard.sqlite3"))
    service = _host_service(store, tmp_path)
    worker, run, _lease = _invoked_host_run(store, service, "live-guard", confirmed=True)
    try:
        with pytest.raises(Exception, match="exact host process identity is not confirmed"):
            service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    # No active session file proves this PID; nothing claims it stopped.
    assert store.get_run(run["run_id"])["state"] == "running"
    assert store.get_active_host_run_lease_for_run(run["run_id"]) is not None


def test_stop_after_terminal_is_a_truthful_no_op(tmp_path):
    store = Store(str(tmp_path / "terminal-stop.sqlite3"))
    service = _host_service(store, tmp_path)
    worker, run, _lease = _invoked_host_run(store, service, "terminal-stop")
    try:
        service.cancel_run(worker["worker_id"], run["run_id"])
        before = len(store.list_events(worker["worker_id"]))
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    assert store.get_run(run["run_id"])["state"] == "cancelled"
    assert len(store.list_events(worker["worker_id"])) == before


class _RecordingCoordinator:
    """Mirrors the coordinator's own endpoint validation for its role workers."""

    def __init__(self):
        self.endpoints = []

    def bind_native_worker(self, worker, run, peers, endpoint):
        self.endpoints.append(endpoint)
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("Coordinator native endpoint invalid")
        return worker


@pytest.mark.parametrize("configured", ["", "http://127.0.0.1:39766/"])
def test_coordinator_binding_uses_only_the_configured_address_and_settles_refusal(tmp_path, monkeypatch, configured):
    store = Store(str(tmp_path / "coordinator-origin.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "coordinator-origin", run_state="queued")
    store.update_worker_state(worker["worker_id"], "ready")
    runtime = _RecordingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    coordinator = _RecordingCoordinator()
    service.coordinator = coordinator
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", configured)
    monkeypatch.setattr(service, "_acquire_host_run_lease", _exact_lease(store, service))
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    try:
        service._process_worker_queue(worker["worker_id"], _processor(service, worker["worker_id"]))
    finally:
        service.shutdown()
    durable = store.get_run(run["run_id"])
    assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
    if configured:
        assert coordinator.endpoints == ["http://127.0.0.1:39766/v1/native/coordinator/"]
        assert durable["state"] == "completed"
    else:
        # No invented origin: the coordinator refuses, and the refusal settles
        # the exact invoked run instead of orphaning it (the O05 trigger).
        assert coordinator.endpoints == ["/v1/native/coordinator/"] and runtime.started == []
        assert durable["state"] == "failed"
        assert "Coordinator native endpoint invalid" in durable["error_text"]


class _NoProcessRuntime(StubRuntime):
    requires_run_start_identity = True

    def reconcile_worker(self, worker):
        return RuntimeInfo(
            runtime="codex-cli", model="test", gateway_url="", gateway_port=None,
            gateway_token=None, session_key=None, state_dir="/synthetic/state",
            workspace_dir="/synthetic/workspace", pid=None,
        )


@pytest.mark.parametrize("before", ["ready", "paused"])
def test_restart_reclaims_idle_host_compute_without_inventing_a_pause(tmp_path, before):
    """After a host restart the next conversation turn restarts reclaimed compute;
    an operator Pause stays a Pause."""

    store = Store(str(tmp_path / f"restart-{before}.sqlite3"))
    service = WorkersProjectsService(store, _NoProcessRuntime(), reconcile_on_startup=False)
    worker, run = _running_host_run(store, f"restart-{before}")
    store.finalize_run_if_state(run["run_id"], expected_state="claimed", state="completed", output_text="done")
    store.update_worker_state(worker["worker_id"], before)
    try:
        service._reconcile_worker_row(store.get_worker(worker["worker_id"]))
        reconciled = store.get_worker(worker["worker_id"])
        assert reconciled["state"] == "paused"
        assert bool(reconciled.get("compute_released_at")) is (before == "ready")
        next_turn = service.assign_run(
            worker["worker_id"], "next conversation turn", start_processor=False,
            resume_paused_worker=False,
        )
    finally:
        service.shutdown()
    assert next_turn["state"] == "queued"
    expected = "starting" if before == "ready" else "paused"
    assert store.get_worker(worker["worker_id"])["state"] == expected
