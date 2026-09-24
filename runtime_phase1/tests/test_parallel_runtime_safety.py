from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from threading import Event, Thread

import pytest

from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime, HostCodexCliRuntime
from workers_projects_runtime.run_states import TERMINAL_RUN_STATES
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _truthfully_invoke_host_run(store: Store, worker: dict, run: dict) -> dict:
    executor_id = "parallel-safety-running-fixture"
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=executor_id
    )
    assert claimed is not None and claimed["run_id"] == run["run_id"]
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    assert store.admit_claimed_run(
        run["run_id"], lease_id=lease["lease_id"], executor_id=executor_id
    )
    invoked = store.mark_run_runtime_invoked(
        run["run_id"], lease_id=lease["lease_id"], executor_id=executor_id
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=executor_id,
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-host-process-generation",
        container_id="",
        session_id="host-parallel-safety",
    )
    assert confirmed is not None
    return confirmed["run"]


def test_canonical_terminal_run_states_exclude_internal_interruption() -> None:
    assert TERMINAL_RUN_STATES == frozenset({"completed", "failed", "cancelled"})


def test_unsafe_same_cli_concurrency_override_is_ignored(tmp_path, monkeypatch) -> None:
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    monkeypatch.setenv("WPR_HOST_ALLOW_CONCURRENT_SAME_CLI", "1")
    # The deprecated boolean must not bypass the real bounded lane setting.
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "1")
    first = {
        "worker_id": "wrk_first",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    second = {**first, "worker_id": "wrk_second"}

    runtime._acquire_host_slot(first)
    try:
        with pytest.raises(RuntimeErrorBase, match="mission lane is at capacity"):
            runtime._acquire_host_slot(second)
    finally:
        runtime._release_host_slot(first["worker_id"])


def test_callback_success_records_http_acceptance_not_user_delivery(tmp_path) -> None:
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Callback transport",
        "Distinguish HTTP acceptance from user delivery",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Callback transport worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Exercise callback acceptance",
    )
    record = store.upsert_callback_outbox(
        callback_id="cb_transport",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        event_type="run.completed",
        url="http://127.0.0.1/callback",
        payload_json=json.dumps({"callback_ts": 1_800_000_000}),
    )
    claim = store.claim_pending_callback(record["callback_id"])
    assert claim is not None

    accepted = store.mark_callback_http_accepted(
        record["callback_id"],
        lease_token=claim["delivery_lease_token"],
        delivery_generation=claim["delivery_generation"],
        attempts=1,
        payload_json="{}",
    )

    assert accepted is not None
    assert accepted["status"] == "http_accepted"
    assert accepted["http_accepted_at"]
    assert accepted["delivered_at"] is None


def test_cross_process_stop_uses_exact_persisted_pid_and_start_identity(tmp_path) -> None:
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    worker_id = "wrk_cross_process"
    run_id = "run_exact"
    runtime._ensure_dirs(worker_id)
    process = subprocess.Popen(
        ["/bin/sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"],
        start_new_session=True,
    )
    try:
        process_start_identity = runtime._process_start_identity(process.pid)
        assert process_start_identity
        runtime._write_active_session(
            worker_id,
            {
                "session_name": "host-run_exact",
                "run_id": run_id,
                "process_pid": process.pid,
                "process_start_identity": process_start_identity,
            },
        )

        confirmed = runtime._stop_active_process(worker_id, run_id=run_id)

        assert confirmed is True
        assert _wait_until(lambda: process.poll() is not None)
        assert not runtime._active_session_meta_path(worker_id).exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)


def test_cross_process_stop_never_signals_a_reused_pid_identity(tmp_path) -> None:
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    worker_id = "wrk_reused_pid"
    run_id = "run_reused_pid"
    runtime._ensure_dirs(worker_id)
    process = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    try:
        runtime._write_active_session(
            worker_id,
            {
                "session_name": "host-run_reused",
                "run_id": run_id,
                "process_pid": process.pid,
                "process_start_identity": "different-process-instance",
            },
        )

        confirmed = runtime._stop_active_process(worker_id, run_id=run_id)

        assert confirmed is True
        assert process.poll() is None
        assert not runtime._active_session_meta_path(worker_id).exists()
    finally:
        process.terminate()
        process.wait(timeout=2)


@pytest.mark.parametrize(
    ("runtime_cls", "event", "session_id"),
    [
        (HostCodexCliRuntime, {"type": "thread.started", "thread_id": "thread_early"}, "thread_early"),
        (HostClaudeCodeRuntime, {"type": "system", "subtype": "init", "session_id": "claude_early"}, "claude_early"),
    ],
)
def test_native_session_observer_persists_jsonl_session_before_exit(
    tmp_path, runtime_cls, event, session_id
) -> None:
    runtime = runtime_cls(base_dir=str(tmp_path / "runtime"))
    worker_id = f"wrk_{runtime.runtime_name}"
    run_id = f"run_{runtime.runtime_name}"
    runtime._ensure_dirs(worker_id)
    stdout_path = tmp_path / f"{runtime.runtime_name}.jsonl"
    stdout_path.write_text("")
    runtime._write_active_session(
        worker_id,
        {"session_name": "host-native", "run_id": run_id},
    )
    stop_event = Event()
    observer = Thread(
        target=runtime._observe_native_session_events,
        args=(worker_id, stdout_path, stop_event),
        daemon=True,
    )
    observer.start()
    try:
        with stdout_path.open("a") as handle:
            handle.write(json.dumps(event) + "\n")
            handle.flush()

        assert _wait_until(
            lambda: runtime._read_session_key(worker_id) == session_id
        ), "native session id was not persisted while the process was still active"
        active_session = runtime._read_active_session(worker_id)
        assert active_session is not None
        assert active_session["native_session_id"] == session_id
    finally:
        stop_event.set()
        observer.join(timeout=1)


def test_host_claude_mission_uses_line_delimited_stream_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "runtime"))
    runtime.binary = "/bin/echo"
    monkeypatch.setattr(runtime, "_chrome_enabled", lambda: False)
    worker = {
        "worker_id": "wrk_claude_mission",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    info = RuntimeInfo(
        runtime="claude-code",
        model="opus",
        gateway_url="",
        gateway_port=None,
        gateway_token=None,
        session_key=None,
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        pid=None,
    )

    command, _ = runtime._build_command(worker, "do work", info)

    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command


class _PendingStopRuntime:
    def __init__(self) -> None:
        self.running = True

    def resolve_model(self, profile: str) -> str:
        return "stub/model"

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker, pid=4242 if self.running else None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=4242 if self.running else None)

    def _info(self, worker: dict, *, pid: int | None) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="stub",
            model="stub/model",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir="/tmp/state",
            workspace_dir="/tmp/workspace",
            pid=pid,
        )


def test_exact_stop_remains_pending_until_runtime_termination_is_proven(tmp_path) -> None:
    store = Store(str(tmp_path / "runtime.db"))
    runtime = _PendingStopRuntime()
    service = WorkersProjectsService(store, runtime, max_workers=1)
    try:
        project = store.create_project(
            "owner", "Pending stop", "Prove termination", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Stop worker",
            role="general",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/model",
            execution_mode="host",
            alias="alias-stop",
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(
            worker["worker_id"], project["project_id"], "keep working", state="queued"
        )
        run = _truthfully_invoke_host_run(store, worker, run)

        pending = service.stop_run(worker["worker_id"], run["run_id"])

        assert pending["confirmation_pending"] is True
        assert store.get_worker(worker["worker_id"])["state"] == "stopping"
        assert store.get_run(run["run_id"])["state"] == "running"

        runtime.running = False
        with store._connect() as conn:  # synthetic crash/reaper clock advance
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
            )
        recovered = service.recover_expired_compute_release_claims_once()

        assert [item["kind"] for item in recovered] == ["stop_run"]
        assert store.get_run(run["run_id"])["state"] == "cancelled"
        assert store.get_worker(worker["worker_id"])["state"] == "ready"
        assert not store.get_worker(worker["worker_id"])["compute_release_token"]
    finally:
        service.shutdown()
