from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo, StubRuntime
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.service import WorkersProjectsService, callback_run_state
from workers_projects_runtime.store import (
    ActiveWorkActionConflictError,
    Store,
    WorkAdmissionError,
)


def _worker_with_run(
    tmp_path,
    runtime: StubRuntime,
    *,
    run_state: str = "running",
    worker_state: str | None = None,
    execution_mode: str = "docker",
    background: bool = True,
) -> tuple[Store, WorkersProjectsService, dict, dict]:
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner-a", "Control mission", "Exercise exact lifecycle controls", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Control worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        execution_mode=execution_mode,
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Keep this exact mission under control",
        state="queued",
    )
    if run_state in {"running", "settling", "paused"}:
        executor_id = "phase3-fixture"
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id=executor_id
        )
        assert claimed is not None
        lease = store.acquire_host_run_lease(
            runtime_family="openclaw",
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
            run["run_id"],
            lease_id=lease["lease_id"],
            executor_id=executor_id,
        )
        invoked = store.mark_run_runtime_invoked(
            run["run_id"],
            lease_id=lease["lease_id"],
            executor_id=executor_id,
        )
        assert invoked is not None
        confirmed = store.confirm_host_run_start(
            worker_id=str(worker["worker_id"]),
            run_id=str(run["run_id"]),
            run_started_at=str(invoked["runtime_invoked_at"]),
            lease_id=str(lease["lease_id"]),
            startup_token=str(lease["startup_token"]),
            executor_id=executor_id,
            identity_kind="in_process",
            pid=None,
            process_group=None,
            process_start_identity="",
            container_id="",
            session_id="in-process",
        )
        assert confirmed is not None
        run = dict(confirmed["run"])
        if run_state != "running":
            run = store.transition_run_if_state(
                run["run_id"], "running", run_state, ended_at=None
            ) or run
    elif run_state != "queued":
        run = store.transition_run_if_state(
            run["run_id"], "queued", run_state
        ) or run
    store.update_worker_state(
        worker["worker_id"], worker_state or ("paused" if run_state == "paused" else "running")
    )
    # Start scheduler threads only after the exact lifecycle fixture is durable;
    # otherwise they can race the test's manual claim/admit/invoke sequence.
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False,
                                     start_background_consumers=background)
    return store, service, store.get_worker(worker["worker_id"]) or worker, store.get_run(run["run_id"]) or run


def test_interrupt_retries_one_benign_snapshot_change_on_the_same_run(tmp_path, monkeypatch):
    store, service, worker, run = _worker_with_run(tmp_path, StubRuntime())
    original_claim = store.try_claim_worker_compute_release
    attempts = 0

    def touch_before_first_claim(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            store.update_worker(worker["worker_id"], last_error="status refreshed")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "try_claim_worker_compute_release", touch_before_first_claim)
    try:
        service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
        assert attempts == 2
        assert store.get_run(run["run_id"])["state"] == "interrupted"
        assert not store.get_worker(worker["worker_id"])["compute_release_token"]
    finally:
        service.shutdown()


def test_interrupt_never_retargets_after_run_changes_before_claim(tmp_path, monkeypatch):
    store, service, worker, run = _worker_with_run(tmp_path, StubRuntime())
    original_claim = store.try_claim_worker_compute_release
    attempts = 0

    def finish_before_first_claim(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            assert store.finalize_run_if_state(
                run["run_id"], "running", "completed", output_text="finished",
                **_exact_terminal_generation(store, run["run_id"]),
            )
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "try_claim_worker_compute_release", finish_before_first_claim)
    try:
        with pytest.raises(RuntimeErrorBase, match="active_work_generation_changed"):
            service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
        assert attempts == 1
        assert store.get_run(run["run_id"])["state"] == "completed"
        assert not store.get_worker(worker["worker_id"])["compute_release_token"]
    finally:
        service.shutdown()


def test_active_work_retry_rejects_changed_exact_source_run(tmp_path, monkeypatch):
    store, service, worker, run = _worker_with_run(
        tmp_path, StubRuntime(), run_state="failed"
    )
    delegation = {
        "work_ref": "work-exact", "tenant_id": "local", "owner_id": "owner-a",
        "worker_id": worker["worker_id"], "project_id": worker["project_id"],
        "run_id": run["run_id"],
    }
    monkeypatch.setattr(store, "get_delegation", lambda *args, **kwargs: delegation)
    try:
        with pytest.raises(RuntimeError, match="active_work_generation_changed"):
            service.execute_active_work_action(
                delegation, action="retry", idempotency_key="exact-source",
                expected_run_id="run_stale",
            )
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
    finally:
        service.shutdown()


def _delegation_with_running_run(
    tmp_path,
    runtime: StubRuntime,
    *,
    start_background_consumers: bool = True,
) -> tuple[Store, WorkersProjectsService, dict, dict, dict]:
    store = Store(str(tmp_path / "delegation-runtime.db"))
    work = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key="phase3-steer-settlement-work",
        request_digest="phase3-steer-settlement-digest",
        origin_ref="phase3-steer-settlement-origin",
        title="Steer settlement",
        goal="Settle one exact interrupted Steer",
        instruction="Original exact run",
        origin_surface="web",
        worker_name="Settlement worker",
        worker_role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        execution_mode="docker",
    )
    worker = store.get_worker(str(work["worker_id"])) or {}
    run = store.get_run(str(work["run_id"])) or {}
    executor_id = "phase3-delegation-fixture"
    claimed = store.claim_next_queued_run(
        str(worker["worker_id"]), executor_id=executor_id
    )
    assert claimed is not None
    lease = store.acquire_host_run_lease(
        runtime_family="openclaw",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    admitted = store.admit_claimed_run(
        str(run["run_id"]),
        lease_id=str(lease["lease_id"]),
        executor_id=executor_id,
    )
    assert admitted is not None
    invoked = store.mark_run_runtime_invoked(
        str(run["run_id"]),
        lease_id=str(lease["lease_id"]),
        executor_id=executor_id,
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=executor_id,
        identity_kind="in_process",
        pid=None,
        process_group=None,
        process_start_identity="",
        container_id="",
        session_id="in-process",
    )
    assert confirmed is not None
    store.update_worker_state(str(worker["worker_id"]), "running")
    service = WorkersProjectsService(
        store,
        runtime,
        reconcile_on_startup=False,
        start_background_consumers=start_background_consumers,
    )
    return (
        store,
        service,
        store.get_delegation(
            str(work["work_ref"]), tenant_id="tenant-a", owner_id="owner-a"
        )
        or work,
        store.get_worker(str(worker["worker_id"])) or worker,
        store.get_run(str(run["run_id"])) or run,
    )


def _exact_terminal_generation(store: Store, run_id: str) -> dict[str, str]:
    run = store.get_run(run_id)
    assert run is not None
    return {
        **(store.get_run_retry_generation(run_id) or {}),
        "expected_runtime_invoked_at": str(run.get("runtime_invoked_at") or ""),
    }


class _BlockingControlRuntime(StubRuntime):
    def __init__(self, control: str) -> None:
        super().__init__()
        self.control = control
        self.entered = Event()
        self.release = Event()

    def _block(self, worker: dict) -> RuntimeInfo:
        self.entered.set()
        assert self.release.wait(2)
        return RuntimeInfo(
            runtime="stub",
            model="stub-model",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=worker.get("state_dir"),
            workspace_dir=worker.get("workspace_dir"),
            pid=None,
        )

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        if self.control == "pause":
            return self._block(worker)
        return super().pause_worker(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        if self.control == "interrupt":
            return self._block(worker)
        return super().interrupt_worker(worker, run_id=run_id)


@pytest.mark.parametrize(
    ("control", "expected_kind"),
    [("pause", "pause_run"), ("interrupt", "interrupt_run")],
)
def test_destructive_exact_run_controls_hold_durable_claim_and_terminal_write_wins(
    tmp_path, control: str, expected_kind: str
):
    runtime = _BlockingControlRuntime(control)
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            if control == "pause":
                service.pause_worker(worker["worker_id"], run_id=run["run_id"])
            else:
                service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
        except BaseException as exc:  # preserved for the assertion thread
            failures.append(exc)

    thread = Thread(target=invoke)
    thread.start()
    try:
        assert runtime.entered.wait(2)
        claimed = store.get_worker(worker["worker_id"]) or {}
        assert claimed["compute_release_kind"] == expected_kind
        assert claimed["compute_release_target_run_id"] == run["run_id"]
        assert claimed["compute_release_token"]
        assert store.finalize_run_if_state(
            run["run_id"],
            "running",
            "completed",
            output_text="COMPLETION WON",
            **_exact_terminal_generation(store, run["run_id"]),
        )
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()

    assert not thread.is_alive()
    assert failures == []
    assert (store.get_run(run["run_id"]) or {})["state"] == "completed"


class _ResumeStartupFailureRuntime(StubRuntime):
    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        raise RuntimeErrorBase("synthetic resume startup failure")


class _UncompensatedResumeFailureRuntime(_ResumeStartupFailureRuntime):
    requires_run_start_identity = True

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        raise RuntimeErrorBase("synthetic compensating pause failure")


class _SteerControlFailureRuntime(StubRuntime):
    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        raise RuntimeErrorBase("synthetic steer control crash")


class _ResumeContainerGenerationRuntime(StubRuntime):
    requires_run_start_identity = True

    def __init__(self, before: str, after: str) -> None:
        super().__init__()
        self.container_id = before
        self.after = after

    def compute_identity(self, _worker: dict) -> dict[str, str]:
        return {"container_id": self.container_id}

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.container_id = self.after
        return super().ensure_worker_ready(worker)


class _CountingControlRuntime(StubRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.pause_calls = 0
        self.resume_calls = 0
        self.interrupt_calls = 0

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.pause_calls += 1
        return super().pause_worker(worker)

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.resume_calls += 1
        return super().ensure_worker_ready(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.interrupt_calls += 1
        return super().interrupt_worker(worker, run_id=run_id)


def test_resume_startup_failure_preserves_paused_truth_and_emits_no_resumed_event(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path,
        _ResumeStartupFailureRuntime(),
        run_state="paused",
        worker_state="paused",
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="resume startup failure"):
            service.resume_worker(worker["worker_id"], run_id=run["run_id"])

        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "paused"
        assert (store.get_run(run["run_id"]) or {})["state"] == "paused"
        assert not any(
            event["event_type"] in {"worker.resumed", "run.resumed"}
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_resume_partial_start_failure_keeps_claim_fenced_when_repause_unproven(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path,
        _UncompensatedResumeFailureRuntime(),
        run_state="paused",
        worker_state="paused",
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="claim remains fenced"):
            service.resume_worker(worker["worker_id"], run_id=run["run_id"])
        durable_worker = store.get_worker(worker["worker_id"]) or {}
        assert durable_worker["state"] == "paused"
        assert durable_worker["compute_release_kind"] == "resume_run"
        assert durable_worker["compute_release_token"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "paused"
        assert not any(
            event["event_type"] in {"worker.resumed", "run.resumed"}
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_reaped_paused_run_resumes_by_requeueing_same_run_generation(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path, StubRuntime(), run_state="paused", worker_state="paused"
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    store.update_worker(
        worker["worker_id"],
        compute_released_at="2026-01-01T00:00:00+00:00",
        pid=None,
    )
    try:
        updated = service.resume_worker(worker["worker_id"], run_id=run["run_id"])
        durable = store.get_run(run["run_id"]) or {}
        assert durable["state"] == "queued"
        assert updated["state"] == "starting"
        assert updated["compute_released_at"] is None
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
    finally:
        service.shutdown()


def test_paused_run_resume_retries_one_unrelated_worker_generation_touch(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path, StubRuntime(), run_state="paused", worker_state="paused"
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    original_claim = store.try_claim_worker_compute_release
    claim_attempts = 0

    def touch_before_first_claim(*args, **kwargs):
        nonlocal claim_attempts
        claim_attempts += 1
        if claim_attempts == 1:
            current = store.get_worker(worker["worker_id"]) or {}
            store.update_worker(worker["worker_id"], model=current.get("model"))
        return original_claim(*args, **kwargs)

    store.try_claim_worker_compute_release = touch_before_first_claim  # type: ignore[method-assign]
    try:
        service.resume_worker(worker["worker_id"], run_id=run["run_id"])
        assert claim_attempts == 2
        assert (store.get_run(run["run_id"]) or {})["state"] in {"queued", "running"}
    finally:
        service.shutdown()


def test_allowed_ai_admission_keeps_exact_steer_worker_generation_current(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path, StubRuntime(), run_state="queued"
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    service.bind_allowed_ai_policy(SimpleNamespace(
        admission_snapshot=lambda current: {"project_id": current["project_id"], "revision": 1}
    ))
    try:
        result = service.steer_worker(
            worker["worker_id"], "Keep this exact run brief",
            run_id=run["run_id"], idempotency_key="allowed-ai-steer-generation",
        )
        assert result["run_id"] != run["run_id"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "cancelled"
        replacement = store.get_run(result["run_id"]) or {}
        assert replacement.get("allowed_ai_admission")
        assert replacement.get("execution_binding")
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("before_container", "after_container"),
    [("", "container-new"), ("container-old", "container-new")],
)
def test_paused_run_with_missing_or_recreated_container_requeues_same_run(
    tmp_path, before_container: str, after_container: str
):
    runtime = _ResumeContainerGenerationRuntime(
        before_container, after_container
    )
    # Without background loops, which may also ensure a processor for the queued run,
    # this counts only what resume itself does.
    store, service, worker, run = _worker_with_run(
        tmp_path, runtime, run_state="paused", worker_state="paused", background=False
    )
    processor_starts: list[str] = []
    service._ensure_worker_processor = processor_starts.append  # type: ignore[method-assign]
    try:
        updated = service.resume_worker(worker["worker_id"], run_id=run["run_id"])
        durable = store.get_run(run["run_id"]) or {}
        assert durable["state"] == "queued"
        assert updated["state"] == "starting"
        assert processor_starts == [worker["worker_id"]]
        assert [item["run_id"] for item in store.list_runs_for_worker(worker["worker_id"])] == [
            run["run_id"]
        ]
    finally:
        service.shutdown()


class _UnprovenHostControlRuntime(StubRuntime):
    requires_run_start_identity = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.calls += 1
        return super().pause_worker(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.calls += 1
        return super().interrupt_worker(worker, run_id=run_id)


@pytest.mark.parametrize("control", ["pause", "interrupt"])
def test_host_control_without_confirmed_exact_process_identity_fails_closed(tmp_path, control: str):
    runtime = _UnprovenHostControlRuntime()
    store, service, worker, run = _worker_with_run(
        tmp_path, runtime, execution_mode="host"
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="identity.*not confirmed|confirmed.*identity"):
            if control == "pause":
                service.pause_worker(worker["worker_id"], run_id=run["run_id"])
            else:
                service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
        assert runtime.calls == 0
        assert (store.get_run(run["run_id"]) or {})["state"] == "running"
    finally:
        service.shutdown()


def test_public_queued_stop_sets_permanent_work_tombstone_and_cancels_full_cohort(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "0")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "0")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "1")
    monkeypatch.setenv("WPR_DOCKER_DISK_RESERVATION_MB", "1")
    store = Store(str(tmp_path / "runtime.db"))
    # Nothing may process the queued cohort while the public Stop is exercised.
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False,
                                     start_background_consumers=False)
    service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        delegation = service.reserve_delegation(
            tenant_id="tenant-a",
            owner_id="owner-a",
            idempotency_key="phase3-stop-delegation",
            request_digest="digest-a",
            origin_ref="origin-a",
            title="Stop queued mission",
            goal="Prove permanent Stop",
            instruction="First queued run",
            origin_surface="web",
            worker_name="Stop worker",
            worker_role="research",
            profile="openclaw-general",
            execution_mode="docker",
        )
        worker_id = str(delegation["worker_id"])
        first_run_id = str(delegation["initial_run_id"])
        sibling = service.assign_run(
            worker_id,
            "Queued sibling",
            start_processor=False,
            resume_paused_worker=False,
        )

        result = service.execute_active_work_action(
            store.get_delegation(
                str(delegation["work_ref"]), tenant_id="tenant-a", owner_id="owner-a"
            )
            or delegation,
            action="stop",
            idempotency_key="phase3-public-stop",
        )

        assert result["state"] == "cancelled"
        durable_worker = store.get_worker(worker_id) or {}
        assert durable_worker["work_stop_id"]
        assert durable_worker["work_stop_settled_at"]
        assert {str((store.get_run(run_id) or {})["state"]) for run_id in (first_run_id, sibling["run_id"])} == {
            "cancelled"
        }
        assert store.list_active_host_run_leases() == []
        with pytest.raises(WorkAdmissionError) as blocked:
            service.assign_run(worker_id, "Must never reopen", start_processor=False)
        assert blocked.value.code == "work_stopped"
    finally:
        service.shutdown()


def test_assign_run_auto_resume_does_not_publish_resumed_truth_before_startup_succeeds(tmp_path):
    store, service, worker, paused_run = _worker_with_run(
        tmp_path,
        _ResumeStartupFailureRuntime(),
        run_state="paused",
        worker_state="paused",
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeErrorBase, match="resume startup failure"):
            service.assign_run(worker["worker_id"], "New queued follow-up")

        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "paused"
        assert (store.get_run(paused_run["run_id"]) or {})["state"] == "paused"
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
        assert not any(
            event["event_type"] == "worker.resumed"
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_no_run_pause_targets_running_generation_before_newer_queued_sibling(tmp_path):
    runtime = _CountingControlRuntime()
    store, service, worker, active = _worker_with_run(tmp_path, runtime)
    queued = store.create_run(
        worker["worker_id"],
        worker["project_id"],
        "Queued follow-up must remain untouched",
        state="queued",
    )
    try:
        service.pause_worker(worker["worker_id"])
        assert runtime.pause_calls == 1
        assert (store.get_run(active["run_id"]) or {})["state"] == "paused"
        assert (store.get_run(queued["run_id"]) or {})["state"] == "queued"
    finally:
        service.shutdown()


def test_no_run_resume_targets_paused_generation_before_newer_queued_sibling(tmp_path):
    runtime = _CountingControlRuntime()
    store, service, worker, paused = _worker_with_run(
        tmp_path, runtime, run_state="paused", worker_state="paused"
    )
    queued = store.create_run(
        worker["worker_id"],
        worker["project_id"],
        "Queued follow-up must remain untouched",
        state="queued",
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        service.resume_worker(worker["worker_id"])
        assert runtime.resume_calls == 1
        assert (store.get_run(paused["run_id"]) or {})["state"] == "running"
        assert (store.get_run(queued["run_id"]) or {})["state"] == "queued"
    finally:
        service.shutdown()


@pytest.mark.parametrize("action", ["pause", "steer", "stop"])
def test_active_work_action_reservation_binds_observed_exact_run_generation(
    tmp_path, action: str
):
    store = Store(str(tmp_path / "runtime.db"))
    work = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key=f"phase3-bind-{action}",
        request_digest="digest",
        origin_ref="origin",
        title="Bind action generation",
        goal="Never silently retarget a control",
        instruction="Initial run",
        origin_surface="web",
        worker_name="Binding worker",
        worker_role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="stub",
        model="stub-model",
        execution_mode="docker",
    )
    observed_run_id = str(work["run_id"])
    observed_state = str(work["run_state"])
    observed_started_at = str(work.get("run_started_at") or "")
    store.create_run(
        str(work["worker_id"]),
        str(work["project_id"]),
        "Newer queued generation",
        state="queued",
    )
    with pytest.raises(ActiveWorkActionConflictError) as stale:
        store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=str(work["work_ref"]),
            idempotency_key=f"phase3-stale-{action}",
            action=action,
            payload_digest="payload",
            expected_current_run_id=observed_run_id,
            expected_source_run_id=observed_run_id,
            expected_source_state=observed_state,
            expected_source_started_at=observed_started_at,
        )
    assert stale.value.code == "active_work_generation_changed"


def test_active_work_action_stale_executor_cannot_checkpoint_finish_or_fail_after_takeover(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.db"))
    work = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key="phase3-action-owner",
        request_digest="digest",
        origin_ref="origin",
        title="Action owner CAS",
        goal="Fence stale action executors",
        instruction="Initial run",
        origin_surface="web",
        worker_name="Owner worker",
        worker_role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="stub",
        model="stub-model",
        execution_mode="docker",
    )
    reserved = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="phase3-owner-action",
        action="pause",
        payload_digest="payload",
        executor_id="executor-old",
        lease_seconds=1,
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
            ("2000-01-01T00:00:00+00:00", reserved["action_use_id"]),
        )
    takeover = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="phase3-owner-action",
        action="pause",
        payload_digest="payload",
        executor_id="executor-new",
        lease_seconds=30,
    )
    assert takeover["recovery_takeover"] is True
    assert store.checkpoint_active_work_action(
        str(reserved["action_use_id"]),
        "stale",
        executor_id="executor-old",
    ) is None
    assert store.finish_active_work_action(
        str(reserved["action_use_id"]),
        response={"state": "paused"},
        executor_id="executor-old",
    ) is None
    assert store.fail_active_work_action(
        str(reserved["action_use_id"]),
        "stale failure",
        executor_id="executor-old",
    ) is False
    current = store.get_active_work_action(str(reserved["action_use_id"])) or {}
    assert current["executor_id"] == "executor-new"
    assert current["status"] == "pending"
    assert current["effect_phase"] == ""


def test_bound_active_work_action_failure_stays_recoverable(tmp_path):
    store = Store(str(tmp_path / "bound-action-recovery.db"))
    work = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key="bound-action-recovery-work",
        request_digest="digest",
        origin_ref="origin",
        title="Bound action recovery",
        goal="Recover the exact durable control effect",
        instruction="Initial run",
        origin_surface="web",
        worker_name="Recovery worker",
        worker_role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="stub",
        model="stub-model",
        execution_mode="docker",
    )
    reserved = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="bound-action-recovery-steer",
        action="steer",
        payload_digest="payload",
        executor_id="executor-a",
        lease_seconds=30,
    )
    action_use_id = str(reserved["action_use_id"])
    bound = store.bind_active_work_action_lifecycle(
        action_use_id,
        operation_id="op_bound_action_recovery",
        operation_kind="steer_run",
        target_run_id=str(work["run_id"]),
        executor_id="executor-a",
    )
    assert bound is not None

    assert store.fail_active_work_action(
        action_use_id,
        "Docker run termination could not be confirmed",
        executor_id="executor-a",
        failure_response={
            "statusCode": 409,
            "detail": {"code": "runtime_confirmation_unavailable"},
        },
    )
    recoverable = store.get_active_work_action(action_use_id) or {}
    assert recoverable["status"] == "pending"
    assert recoverable["response_json"] == ""
    assert recoverable["last_error"] == (
        "Docker run termination could not be confirmed"
    )

    finished = store.finish_active_work_action(
        action_use_id,
        response={"status": "accepted", "state": "queued"},
        executor_id="executor-a",
    )
    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["last_error"] == ""


def test_interrupted_steer_settles_exact_fence_and_bound_action_immediately(
    tmp_path,
):
    store, service, work, worker, run = _delegation_with_running_run(
        tmp_path,
        _SteerControlFailureRuntime(),
        start_background_consumers=False,
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    reserved = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="phase3-interrupted-steer-action",
        action="steer",
        payload_digest="phase3-interrupted-steer-payload",
        executor_id=service.executor_id,
        lease_seconds=30,
    )
    action_use_id = str(reserved["action_use_id"])
    try:
        with pytest.raises(RuntimeErrorBase, match="synthetic steer control crash"):
            service.steer_worker(
                str(worker["worker_id"]),
                "Use the exact replacement",
                run_id=str(run["run_id"]),
                idempotency_key="phase3-interrupted-steer-effect",
                action_use_id=action_use_id,
            )
        action = store.get_active_work_action(action_use_id) or {}
        operation_id = str(action["lifecycle_operation_id"])
        assert operation_id
        assert store.fail_active_work_action(
            action_use_id,
            "Runtime steer did not confirm the exact process stopped",
            executor_id=service.executor_id,
        )
        claimed = store.get_worker(str(worker["worker_id"])) or {}
        replacement_run_id = str(claimed["compute_release_replacement_run_id"])
        assert store.finalize_run_if_state(
            str(run["run_id"]),
            "running",
            "interrupted",
            error_text="Provider observed the exact interrupt",
            **_exact_terminal_generation(store, str(run["run_id"])),
        )

        settled = service._settle_interrupted_steer_claim(
            str(worker["worker_id"]), str(run["run_id"])
        )

        assert settled is not None
        assert (store.get_worker(str(worker["worker_id"])) or {})[
            "compute_release_token"
        ] == ""
        assert (store.get_delegation(
            str(work["work_ref"]), tenant_id="tenant-a", owner_id="owner-a"
        ) or {})["current_run_id"] == replacement_run_id
        assert store.has_lifecycle_operation_event(
            operation_id=operation_id,
            operation_kind="steer_run",
            event_type="run.interrupted",
            worker_id=str(worker["worker_id"]),
            run_id=str(run["run_id"]),
        )
        finished = store.get_active_work_action(action_use_id) or {}
        assert finished["status"] == "completed"
        response = json.loads(str(finished["response_json"]))
        assert response["workRef"] == str(work["work_ref"])
        assert response["action"] == "steer"
        assert response["status"] == "queued"
        assert response["state"] == "queued"
        assert response["confirmationPending"] is False
    finally:
        service.shutdown()


def test_scheduler_recovers_expired_steer_before_due_worker_retries(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_SCHEDULER_INTERVAL_S", "3600")
    store, service, work, worker, run = _delegation_with_running_run(
        tmp_path,
        _SteerControlFailureRuntime(),
        start_background_consumers=False,
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    reserved = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="phase3-expired-steer-action",
        action="steer",
        payload_digest="phase3-expired-steer-payload",
        executor_id=service.executor_id,
        lease_seconds=30,
    )
    action_use_id = str(reserved["action_use_id"])
    try:
        with pytest.raises(RuntimeErrorBase, match="synthetic steer control crash"):
            service.steer_worker(
                str(worker["worker_id"]),
                "Recover the exact replacement",
                run_id=str(run["run_id"]),
                idempotency_key="phase3-expired-steer-effect",
                action_use_id=action_use_id,
            )
        assert store.fail_active_work_action(
            action_use_id,
            "Runtime steer did not confirm the exact process stopped",
            executor_id=service.executor_id,
        )
        assert store.finalize_run_if_state(
            str(run["run_id"]),
            "running",
            "interrupted",
            error_text="Provider observed the exact interrupt",
            **_exact_terminal_generation(store, str(run["run_id"])),
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", str(worker["worker_id"])),
            )

        phases: list[tuple[str, str]] = []
        real_recovery = service.recover_expired_compute_release_claims_once
        service.process_due_schedules_once = lambda: None  # type: ignore[method-assign]
        service.process_queued_work_status_once = lambda: None  # type: ignore[method-assign]
        service.process_provider_liveness_once = lambda: None  # type: ignore[method-assign]

        def recover() -> list[dict[str, object]]:
            phases.append(("recovery", "before"))
            result = real_recovery()
            phases.append(
                (
                    "recovery",
                    str(
                        (store.get_worker(str(worker["worker_id"])) or {}).get(
                            "compute_release_token"
                        )
                        or "cleared"
                    ),
                )
            )
            return result

        def retry() -> None:
            phases.append(
                (
                    "retry",
                    str(
                        (store.get_worker(str(worker["worker_id"])) or {}).get(
                            "compute_release_token"
                        )
                        or "cleared"
                    ),
                )
            )

        service.recover_expired_compute_release_claims_once = recover  # type: ignore[method-assign]
        service.process_due_worker_retries_once = retry  # type: ignore[method-assign]

        service._process_scheduler_cycle()

        assert phases == [
            ("recovery", "before"),
            ("recovery", "cleared"),
            ("retry", "cleared"),
        ]
        assert (store.get_active_work_action(action_use_id) or {})[
            "status"
        ] == "completed"
    finally:
        service.shutdown()


def test_steer_holds_one_exact_claim_until_interruption_and_replacement_commit(tmp_path):
    runtime = _BlockingControlRuntime("interrupt")
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    results: list[dict] = []
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(
                service.steer_worker(
                    worker["worker_id"],
                    "Use the replacement direction",
                    run_id=run["run_id"],
                    idempotency_key="phase3-steer-once",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=invoke)
    thread.start()
    try:
        assert runtime.entered.wait(2)
        claimed = store.get_worker(worker["worker_id"]) or {}
        assert claimed["compute_release_kind"] == "steer_run"
        assert claimed["compute_release_target_run_id"] == run["run_id"]
        replacement_run_id = str(claimed["compute_release_replacement_run_id"])
        assert replacement_run_id
        durable_replacement = store.get_run(replacement_run_id) or {}
        assert durable_replacement["state"] == "queued"
        assert store.claim_next_queued_run(worker["worker_id"]) is None
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()

    assert not thread.is_alive()
    assert failures == []
    durable_runs = store.list_runs_for_worker(worker["worker_id"])
    assert len(durable_runs) == 2
    assert (store.get_run(run["run_id"]) or {})["state"] == "interrupted"
    replacement = results[0]
    assert replacement["state"] == "queued"
    assert (store.get_run(replacement["run_id"]) or {})["state"] == "queued"
    delegation_state, terminal = service._delegation_callback_state(
        {"worker_id": worker["worker_id"], "current_run_id": replacement["run_id"]},
        callback_run=store.get_run(run["run_id"]),
    )
    assert (delegation_state, terminal) == ("queued", False)
    assert callback_run_state(
        "run.interrupted", store.get_run(run["run_id"])
    ) == "cancelled"


def test_steer_terminal_wins_without_creating_replacement(tmp_path):
    store, service, worker, run = _worker_with_run(tmp_path, StubRuntime())
    try:
        assert store.finalize_run_if_state(
            run["run_id"],
            "running",
            "completed",
            output_text="Completion won",
            **_exact_terminal_generation(store, run["run_id"]),
        )
        with pytest.raises(RuntimeErrorBase, match="No exact active run"):
            service.steer_worker(
                worker["worker_id"],
                "Too late to replace",
                run_id=run["run_id"],
                idempotency_key="phase3-steer-too-late",
            )
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
        assert (store.get_run(run["run_id"]) or {})["state"] == "completed"
    finally:
        service.shutdown()


def test_inflight_steer_terminal_wins_and_does_not_report_queued_success(tmp_path):
    runtime = _BlockingControlRuntime("interrupt")
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    results: list[dict] = []

    thread = Thread(
        target=lambda: results.append(
            service.steer_worker(
                worker["worker_id"],
                "Replacement must be suppressed",
                run_id=run["run_id"],
                idempotency_key="phase3-steer-completion-wins",
            )
        )
    )
    thread.start()
    try:
        assert runtime.entered.wait(2)
        assert store.finalize_run_if_state(
            run["run_id"],
            "running",
            "completed",
            output_text="COMPLETION WON",
            **_exact_terminal_generation(store, run["run_id"]),
        )
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()

    assert results[0]["_control_outcome"] == "terminal_won"
    assert results[0]["state"] == "completed"
    runs = store.list_runs_for_worker(worker["worker_id"])
    replacement = next(item for item in runs if item["run_id"] != run["run_id"])
    assert replacement["state"] == "cancelled"


def test_active_work_pause_terminal_winner_is_returned_instead_of_false_paused(tmp_path):
    runtime = _BlockingControlRuntime("pause")
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    delegation = {
        "work_ref": "work_terminal_pause",
        "tenant_id": "local",
        "owner_id": "owner-a",
        "worker_id": worker["worker_id"],
        "project_id": worker["project_id"],
        "current_run_id": run["run_id"],
        "run_id": run["run_id"],
    }
    store.update_worker(worker["worker_id"], last_run_id=run["run_id"])
    # Use the real public action selection via a synthetic delegation row.
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO delegations (
                work_ref, tenant_id, owner_id, idempotency_key, request_digest,
                origin_ref, title, origin_surface, project_id, worker_id,
                initial_run_id, current_run_id, created_at, updated_at
            ) VALUES (?, 'local', 'owner-a', ?, ?, '', ?, 'web', ?, ?, ?, ?, ?, ?)
            """,
            (
                delegation["work_ref"],
                "phase3-terminal-pause",
                "digest",
                "Terminal pause",
                worker["project_id"],
                worker["worker_id"],
                run["run_id"],
                run["run_id"],
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    result: list[dict] = []
    thread = Thread(
        target=lambda: result.append(
            service.execute_active_work_action(
                delegation,
                action="pause",
                idempotency_key="terminal-pause-action",
            )
        )
    )
    thread.start()
    try:
        assert runtime.entered.wait(2)
        assert store.finalize_run_if_state(
            run["run_id"],
            "running",
            "completed",
            **_exact_terminal_generation(store, run["run_id"]),
        )
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()
    assert result[0]["state"] == "completed"
    assert result[0]["control_outcome"] == "terminal_won"


def test_expired_pause_claim_recovery_replays_exact_idempotent_control_and_finalizes(tmp_path):
    store, service, worker, run = _worker_with_run(tmp_path, StubRuntime())
    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=str(worker["updated_at"]),
        expected_last_run_id=str(worker["last_run_id"]),
        expected_state=str(worker["state"]),
        expected_container_id="",
        owner="crashed-service",
        ttl_s=30,
        kind="pause_run",
        target_run_id=str(run["run_id"]),
        expected_target_started_at=str(run["started_at"]),
    )
    assert claim
    with store._connect() as conn:  # synthetic crash clock advance
        conn.execute(
            "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
            ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
        )

    try:
        recovered = service.recover_expired_compute_release_claims_once()
        assert [item["kind"] for item in recovered] == ["pause_run"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "paused"
        durable_worker = store.get_worker(worker["worker_id"]) or {}
        assert durable_worker["state"] == "paused"
        assert durable_worker["compute_release_token"] == ""
    finally:
        service.shutdown()


def test_expired_interrupt_claim_terminal_winner_clears_fence_without_runtime_action(tmp_path):
    runtime = _CountingControlRuntime()
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=str(worker["updated_at"]),
        expected_last_run_id=str(worker["last_run_id"]),
        expected_state=str(worker["state"]),
        expected_container_id="",
        owner="crashed-service",
        ttl_s=30,
        kind="interrupt_run",
        target_run_id=str(run["run_id"]),
        expected_target_started_at=str(run["started_at"]),
    )
    assert claim
    assert store.finalize_run_if_state(
        run["run_id"],
        "running",
        "completed",
        output_text="COMPLETION WON",
        **_exact_terminal_generation(store, run["run_id"]),
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
            ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
        )
    try:
        recovered = service.recover_expired_compute_release_claims_once()
        assert recovered[0]["terminal_won"] is True
        assert runtime.interrupt_calls == 0
        assert (store.get_run(run["run_id"]) or {})["state"] == "completed"
        assert (store.get_worker(worker["worker_id"]) or {})[
            "compute_release_token"
        ] == ""
    finally:
        service.shutdown()


def test_control_callback_effect_replay_is_insert_once_and_preserves_delivered_row(
    tmp_path, monkeypatch
):
    runtime = _CountingControlRuntime()
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    store.update_worker(
        worker["worker_id"],
        bootstrap_bundle_json=json.dumps(
            {"callbacks": {"events_webhook_url": "http://callback.local/events"}}
        ),
    )
    original_replay = service._replay_pending_lifecycle_effects
    monkeypatch.setattr(service, "_replay_pending_lifecycle_effects", lambda: None)
    monkeypatch.setattr(service.executor, "submit", lambda *_args, **_kwargs: None)
    try:
        service.pause_worker(worker["worker_id"], run_id=run["run_id"])
        effects = store.list_lifecycle_operation_effects(worker_id=worker["worker_id"])
        assert len(effects) == 1
        assert effects[0]["status"] == "pending"

        monkeypatch.setattr(service, "_replay_pending_lifecycle_effects", original_replay)
        real_mark = store.mark_lifecycle_effect_applied

        def crash_after_materialize(effect_id: str, executor_id: str, *, lease_epoch: int):
            assert store.retry_lifecycle_effect(
                effect_id,
                executor_id,
                lease_epoch=lease_epoch,
                error_code="callback_enqueue_failed",
            )
            raise RuntimeError("synthetic crash after callback materialization")

        monkeypatch.setattr(store, "mark_lifecycle_effect_applied", crash_after_materialize)
        claim = store.claim_next_lifecycle_effect(
            service._executor_id,
            effect_kinds=("callback.run_paused",),
        )
        assert claim and claim["effect_id"] == effects[0]["effect_id"]
        with pytest.raises(RuntimeError, match="synthetic crash"):
            service._apply_lifecycle_effect(claim)

        with store._connect() as conn:
            callback = conn.execute(
                "SELECT * FROM callback_outbox WHERE worker_id = ?",
                (worker["worker_id"],),
            ).fetchone()
        assert callback is not None
        original_payload = str(callback["payload_json"])
        callback_claim = store.claim_pending_callback(str(callback["callback_id"]))
        assert callback_claim is not None
        assert store.mark_callback_http_accepted(
            str(callback["callback_id"]),
            lease_token=callback_claim["delivery_lease_token"],
            delivery_generation=callback_claim["delivery_generation"],
            attempts=1,
            payload_json=original_payload,
        )

        monkeypatch.setattr(store, "mark_lifecycle_effect_applied", real_mark)
        original_replay()
        with store._connect() as conn:
            callbacks = conn.execute(
                "SELECT * FROM callback_outbox WHERE worker_id = ?",
                (worker["worker_id"],),
            ).fetchall()
        assert len(callbacks) == 1
        assert callbacks[0]["status"] == "http_accepted"
        assert callbacks[0]["payload_json"] == original_payload
        assert store.list_lifecycle_operation_effects(worker_id=worker["worker_id"])[0][
            "status"
        ] == "applied"
    finally:
        service.shutdown()


def test_expired_steer_claim_recovers_only_its_durable_fenced_replacement(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path, _SteerControlFailureRuntime()
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    with pytest.raises(RuntimeErrorBase, match="steer control crash"):
        service.steer_worker(
            worker["worker_id"],
            "Recover this exact replacement",
            run_id=run["run_id"],
            idempotency_key="phase3-steer-recovery",
        )
    claimed = store.get_worker(worker["worker_id"]) or {}
    replacement_run_id = str(claimed["compute_release_replacement_run_id"])
    assert replacement_run_id
    assert (store.get_run(replacement_run_id) or {})["state"] == "queued"
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
            ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
        )
    service.runtime = StubRuntime()

    try:
        recovered = service.recover_expired_compute_release_claims_once()
        assert [item["kind"] for item in recovered] == ["steer_run"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "interrupted"
        assert (store.get_run(replacement_run_id) or {})["state"] == "queued"
        assert (store.get_worker(worker["worker_id"]) or {})[
            "compute_release_token"
        ] == ""
    finally:
        service.shutdown()


def test_expired_steer_claim_finishes_bound_action_after_restart_requeues_source(
    tmp_path,
):
    store, service, work, worker, run = _delegation_with_running_run(
        tmp_path, _SteerControlFailureRuntime()
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    reserved = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="phase3-restart-requeued-steer-action",
        action="steer",
        payload_digest="phase3-restart-requeued-steer-payload",
        executor_id=service.executor_id,
        lease_seconds=30,
    )
    action_use_id = str(reserved["action_use_id"])
    try:
        with pytest.raises(RuntimeErrorBase, match="synthetic steer control crash"):
            service.steer_worker(
                str(worker["worker_id"]),
                "Recover this exact replacement after restart",
                run_id=str(run["run_id"]),
                idempotency_key="phase3-restart-requeued-steer-effect",
                action_use_id=action_use_id,
            )
        assert store.fail_active_work_action(
            action_use_id,
            "Runtime steer did not confirm the exact process stopped",
            executor_id=service.executor_id,
        )
        claimed = store.get_worker(str(worker["worker_id"])) or {}
        operation_id = str(claimed["compute_release_operation_id"])
        replacement_run_id = str(claimed["compute_release_replacement_run_id"])
        assert operation_id
        assert replacement_run_id

        with store._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET state = 'queued', active_attempt_id = '', claimed_at = NULL,
                    admitted_at = NULL, runtime_invoked_at = NULL
                WHERE run_id = ?
                """,
                (str(run["run_id"]),),
            )
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", str(worker["worker_id"])),
            )
        service.runtime = StubRuntime()

        recovered = service.recover_expired_compute_release_claims_once()

        assert [item["kind"] for item in recovered] == ["steer_run"]
        assert (store.get_worker(str(worker["worker_id"])) or {})[
            "compute_release_token"
        ] == ""
        assert (store.get_delegation(
            str(work["work_ref"]), tenant_id="tenant-a", owner_id="owner-a"
        ) or {})["current_run_id"] == replacement_run_id
        finished = store.get_active_work_action(action_use_id) or {}
        assert finished["status"] == "completed"
        response = json.loads(str(finished["response_json"]))
        assert response["workRef"] == str(work["work_ref"])
        assert response["action"] == "steer"
        assert response["state"] == "queued"
        assert response["confirmationPending"] is False
    finally:
        service.shutdown()


def test_expired_steer_claim_settles_after_source_and_replacement_are_terminal(
    tmp_path,
):
    store, service, worker, run = _worker_with_run(
        tmp_path, _SteerControlFailureRuntime()
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    with pytest.raises(RuntimeErrorBase, match="steer control crash"):
        service.steer_worker(
            worker["worker_id"],
            "Preserve this exact replacement",
            run_id=run["run_id"],
            idempotency_key="phase3-terminal-steer-recovery",
        )
    claimed = store.get_worker(worker["worker_id"]) or {}
    replacement_run_id = str(claimed["compute_release_replacement_run_id"])
    assert replacement_run_id
    assert store.finalize_run_if_state(
        run["run_id"],
        "running",
        "interrupted",
        error_text="Synthetic interrupted source",
        **_exact_terminal_generation(store, run["run_id"]),
    )
    assert store.transition_run_if_state(
        replacement_run_id,
        "queued",
        "failed",
        error_text="Synthetic queue timeout",
        failure_class="queue_timeout",
        failure_retryable=1,
    )
    attempts_before = store.list_run_attempts(run["run_id"])
    source_before = store.get_run(run["run_id"])
    replacement_before = store.get_run(replacement_run_id)
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
            ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
        )

    runtime = StubRuntime()

    def unexpected_runtime_action(*_args, **_kwargs):
        raise AssertionError("terminal steer recovery must not touch runtime compute")

    runtime.interrupt_worker = unexpected_runtime_action  # type: ignore[method-assign]
    runtime.terminate_worker = unexpected_runtime_action  # type: ignore[method-assign]
    service.runtime = runtime

    try:
        recovered = service.recover_expired_compute_release_claims_once()
        assert [item["kind"] for item in recovered] == ["steer_run"]
        assert (store.get_worker(worker["worker_id"]) or {})[
            "compute_release_token"
        ] == ""
        assert store.get_run(run["run_id"]) == source_before
        assert store.get_run(replacement_run_id) == replacement_before
        assert store.list_run_attempts(run["run_id"]) == attempts_before
    finally:
        service.shutdown()


def test_idle_compute_release_does_not_reinterpret_a_fenced_steer(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path, _SteerControlFailureRuntime()
    )
    with pytest.raises(RuntimeErrorBase, match="steer control crash"):
        service.steer_worker(
            worker["worker_id"],
            "Keep the steer fence exact",
            run_id=run["run_id"],
            idempotency_key="phase3-idle-steer-fence",
        )
    claimed_before = store.get_worker(worker["worker_id"]) or {}

    try:
        assert service._release_worker_compute(
            claimed_before,
            idle_seconds=3600,
        ) is None
        claimed_after = store.get_worker(worker["worker_id"]) or {}
        assert claimed_after["compute_release_token"] == claimed_before[
            "compute_release_token"
        ]
        assert claimed_after["compute_release_kind"] == "steer_run"
        assert claimed_after["compute_release_replacement_run_id"] == claimed_before[
            "compute_release_replacement_run_id"
        ]
    finally:
        service.shutdown()


def test_host_control_recovery_requires_matching_receipt_and_proven_exact_generation(
    tmp_path,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "host-runtime"))
    worker_id = "wrk_receipt_recovery"
    run_id = "run_receipt_recovery"
    session = {
        "session_name": "host-session-receipt",
        "run_id": run_id,
        "process_pid": 51001,
        "process_group": 51001,
        "process_start_identity": "ps-lstart:synthetic-receipt-generation",
    }
    lease = {
        "worker_id": worker_id,
        "run_id": run_id,
        "status": "active",
        "startup_state": "confirmed",
        "startup_identity_kind": "host_process",
        "pid": 51001,
        "process_group": 51001,
        "process_start_identity": "ps-lstart:synthetic-receipt-generation",
        "startup_session_id": "host-session-receipt",
    }
    worker = {
        "worker_id": worker_id,
        "project_id": "prj_receipt",
        "profile": "codex-cli",
        "model": "synthetic-model",
        "workspace_dir": str(tmp_path / "workspace"),
        "_active_run_id": run_id,
        "_host_run_lease": lease,
        "compute_release_kind": "pause_run",
    }
    runtime._write_host_control_receipt(
        worker,
        active_session=session,
        run_id=run_id,
        operation="pause_run",
        confirmed=False,
    )
    runtime._recorded_process_is_running = (  # type: ignore[method-assign]
        lambda _pid, _identity: False
    )
    runtime._write_stopped_active_run_evidence = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: None
    )

    info = runtime.pause_worker(worker)

    assert info.pid is None
    assert (runtime._read_host_control_receipt(worker_id) or {})["status"] == "confirmed"
    mismatched = {**worker, "_active_run_id": "run_other"}
    with pytest.raises(RuntimeErrorBase, match="identity is not confirmed"):
        runtime.pause_worker(mismatched)

# Model-selected follow-up files use existing queued-run admission. No provider or network.
import asyncio
import hashlib
import ast
from dataclasses import replace
from pathlib import Path
from fastapi import HTTPException
from workers_projects_runtime.workspace_files import FileAdmissionError, materialize_managed_file
from workers_projects_runtime.broker_admission import BrokerAdmissionError
class _FileInputPrivateRuntime(StubRuntime):
    def __init__(self, root, *, block=False, crash=False):
        self.root=Path(root);self.block=block;self.crash=crash
        self.entered=Event();self.release=Event()
    def _runtime_info(self, worker, *, pid):
        base=super()._runtime_info(worker,pid=pid)
        workspace=self.root/worker['worker_id']/'workspace';state=workspace.parent/'state'
        workspace.mkdir(parents=True,exist_ok=True);state.mkdir(exist_ok=True)
        return replace(base,workspace_dir=str(workspace),state_dir=str(state))
    def interrupt_worker(self,worker,run_id=None):
        self.entered.set()
        if self.block:assert self.release.wait(5)
        if self.crash:raise RuntimeErrorBase('synthetic steer control crash')
        return self._runtime_info(worker,pid=None)
    def run_task(self,*args,**kwargs):
        raise AssertionError('Integration fixture must not execute a provider task')


def _file_input_fixture(tmp_path, monkeypatch, *, block=False,crash=False):
    monkeypatch.setenv('WPR_BOOTSTRAP_SOURCE_ROOTS',str(tmp_path))
    runtime=_FileInputPrivateRuntime(tmp_path/'runtime',block=block,crash=crash)
    store,service,work,worker,run=_delegation_with_running_run(tmp_path,runtime,start_background_consumers=False)
    info=runtime._runtime_info(worker,pid=None)
    worker=store.update_worker(worker['worker_id'],workspace_dir=info.workspace_dir,state_dir=info.state_dir)
    service._ensure_worker_processor=lambda _id:None
    return store,service,work,worker,run,runtime


def _file_input_uploads(service):
    payloads=[('selection.csv',b'mode,cost\nwalking,0\ncycling,0\nbus,2.75\n','original'),
              ('selected-notes.txt','Exact extracted text. Café.\n'.encode('utf-8'),'extracted_text')]
    inputs=[]
    for index,(name,body,kind) in enumerate(payloads):
        upload=service.files.create_upload('tenant-a','owner-a',draft_id='synthetic-action-draft',name=name,size_bytes=len(body),idempotency_key='synthetic-upload-'+str(index))
        async def chunks():
            yield body[:3];yield body[3:]
        ready=asyncio.run(service.files.receive('tenant-a','owner-a',upload['upload_id'],chunks()))
        assert ready['state']=='ready' and ready['sha256']==hashlib.sha256(body).hexdigest()
        manifest=service.files.manifest('tenant-a','owner-a',[upload['upload_id']])[0]
        inputs.append({'upload_id':manifest['upload_id'],'revision':manifest['revision'],'sha256':manifest['sha256'],
          'size_bytes':manifest['size_bytes'],'filename':manifest['name'],'source_file_id':'synthetic-core-file-'+str(index),
          'input_kind':kind,'media_group_index':index})
    return inputs,payloads


def _file_input_reserve(store,service,work,inputs,*,action='queue',key='synthetic-file-action',executor=None,digest=None):
    context={'version':1,'origin_ref':'synthetic-followup-origin','source_event_id':'synthetic-followup-event','source_revision':1,
             'surface':'web','output_contract':{'mode':'inherit'},'prompt_layers':service.worker_prompt_layer_trace(),'file_inputs':inputs}
    payload_digest=digest or hashlib.sha256(json.dumps({'action':action,'instruction':'Use exactly these selected inputs','file_inputs':inputs},sort_keys=True).encode()).hexdigest()
    return store.reserve_active_work_action(tenant_id='tenant-a',owner_id='owner-a',work_ref=work['work_ref'],
       idempotency_key=key,action=action,payload_digest=payload_digest,source_context=context,
       executor_id=executor or service.executor_id,lease_seconds=30)


def _file_input_manifest(store,worker,run_id):
    return store.get_run_file_manifest(run_id,worker_id=worker['worker_id'],tenant_id='tenant-a',owner_id='owner-a')


def _file_input_complete(store,run):
    assert store.finalize_run_if_state(run['run_id'],'running','completed',output_text='Synthetic original draft',
       **_exact_terminal_generation(store,run['run_id']))
    store.update_worker_state(run['worker_id'],'ready')


def _file_input_admit_next(store,worker):
    executor='file-input-next-admission'
    run=store.claim_next_queued_run(worker['worker_id'],executor_id=executor);assert run is not None
    lease=store.acquire_host_run_lease(runtime_family='openclaw',lane='mission',tenant_id='tenant-a',owner_id='owner-a',
       worker_id=worker['worker_id'],run_id=run['run_id'],executor_id=executor,conversation_limit=2,mission_limit=64,
       account_mission_limit=64,tenant_mission_limit=64,lease_ttl_s=300)
    admitted=store.admit_claimed_run(run['run_id'],lease_id=lease['lease_id'],executor_id=executor)
    assert admitted is not None
    return admitted


@pytest.mark.parametrize('action',['queue','message'])
@pytest.mark.parametrize('source_state',['running','completed'])
def test_active_work_file_real_next_boundary_and_restart(tmp_path,monkeypatch,action,source_state):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,payloads=_file_input_uploads(service)
    old_workspace=Path(worker['workspace_dir']);sentinel=old_workspace/'existing-draft.txt';sentinel.write_bytes(b'Original ongoing workspace')
    old_bundle=store.get_worker(worker['worker_id'])['bootstrap_bundle_json']
    if source_state=='completed':_file_input_complete(store,run)
    reservation=_file_input_reserve(store,service,work,inputs,action=action)
    try:
        result=service.execute_active_work_action(work,action=action,instruction='Use exactly these selected inputs',
          idempotency_key='synthetic-file-action',action_use_id=reservation['action_use_id'],start_processor=False)
        next_id=result['run_id'];assert next_id!=run['run_id']
        accepted=_file_input_manifest(store,worker,next_id)
        assert [(x['upload_id'],x['revision'],x['name'],x['sha256'],x['size_bytes'],x['source_file_id'],x['input_kind'],x['media_group_index']) for x in accepted]==[(x['upload_id'],x['revision'],x['filename'],x['sha256'],x['size_bytes'],x['source_file_id'],x['input_kind'],x['media_group_index']) for x in inputs]
        assert list(old_workspace.rglob('*'))==[sentinel]
        assert store.get_worker(worker['worker_id'])['bootstrap_bundle_json']==old_bundle
        assert sentinel.read_bytes()==b'Original ongoing workspace'
        assert store.finish_active_work_action(reservation['action_use_id'],response=result,current_run_id=next_id,executor_id=service.executor_id)
    finally:service.shutdown()
    # Actual SQLite reopen, not a copied dictionary or mock Store.
    restarted=Store(store.db_path);again=WorkersProjectsService(restarted,runtime,reconcile_on_startup=False,start_background_consumers=False)
    again._ensure_worker_processor=lambda _id:None
    try:
        assert _file_input_manifest(restarted,worker,next_id)==accepted
        replay=_file_input_reserve(restarted,again,work,inputs,action=action)
        assert replay['idempotent_replay'] and not replay['should_execute']
        assert len(restarted.list_runs_for_worker(worker['worker_id']))==2
        assert _file_input_manifest(restarted,worker,run['run_id'])==[]
        if source_state=='running':
            assert restarted.claim_next_queued_run(worker['worker_id']) is None
            _file_input_complete(restarted,run)
        admitted=_file_input_admit_next(restarted,worker);assert admitted['run_id']==next_id
        projected=again._run_local_worker(restarted.get_worker(worker['worker_id']),admitted)
        bundle=json.loads(projected['bootstrap_bundle_json'])
        selected=[x for x in bundle['files'] if x.get('source_path')]
        assert [(x['path'].split('/')[-1],x['sha256']) for x in selected]==[(name,hashlib.sha256(body).hexdigest()) for name,body,_ in payloads]
        assert list(old_workspace.rglob('*'))==[sentinel]
        for entry in selected:materialize_managed_file(old_workspace,entry,projected)
        for entry,(_,body,_) in zip(selected,payloads):assert (old_workspace/entry['path']).read_bytes()==body
        index=json.loads(next(x['content'] for x in bundle['files'] if x['path']==projected['_run_input_manifest_path']))
        assert [x['source_file_id'] for x in index['files']]==[x['source_file_id'] for x in inputs]
        assert [x['input_kind'] for x in index['files']]==['original','extracted_text']
        assert sentinel.read_bytes()==b'Original ongoing workspace'
        with pytest.raises(ValueError,match='owner'):
            restarted.get_run_file_manifest(next_id,worker_id=worker['worker_id'],tenant_id='tenant-a',owner_id='foreign-owner')
    finally:again.shutdown()


@pytest.mark.parametrize('drift',['not_ready','sha256','name','revision','foreign'])
def test_active_work_file_real_transaction_rechecks_ready_receipt(tmp_path,monkeypatch,drift):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,_=_file_input_uploads(service)
    manifest=service._active_work_action_file_manifest(worker, _file_input_reserve(store,service,work,inputs))
    uid=inputs[0]['upload_id']
    if drift=='revision':manifest[0]['revision']='changed-revision'
    else:
        field,value={'not_ready':('state','created'),'sha256':('sha256','0'*64),'name':('name','renamed.csv'),'foreign':('owner_id','foreign-owner')}[drift]
        with store._connect() as conn:conn.execute(f'UPDATE workspace_file_uploads SET {field}=? WHERE upload_id=?',(value,uid))
    try:
        with pytest.raises((FileAdmissionError,ValueError)):
            store.create_idempotent_run(run_id='run-receipt-drift',worker_id=worker['worker_id'],project_id=worker['project_id'],instruction='Selected followup',file_manifest=manifest)
        assert store.get_run('run-receipt-drift') is None
        with store._connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM workspace_file_run_inputs WHERE run_id=?',('run-receipt-drift',)).fetchone()[0]==0
    finally:service.shutdown()


def test_active_work_file_real_retry_identity_and_collision_rollback(tmp_path,monkeypatch):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,payloads=_file_input_uploads(service);reservation=_file_input_reserve(store,service,work,inputs)
    manifest=service._active_work_action_file_manifest(worker,reservation)
    args=dict(run_id='run-immutable-inputs',worker_id=worker['worker_id'],project_id=worker['project_id'],instruction='Selected followup')
    try:
        first,new=store.create_idempotent_run(**args,file_manifest=manifest);assert new
        second,new=Store(store.db_path).create_idempotent_run(**args,file_manifest=manifest);assert not new and first==second
        accepted=_file_input_manifest(store,worker,args['run_id'])
        with pytest.raises(ValueError,match='different run inputs'):store.create_idempotent_run(**args,file_manifest=list(reversed(manifest)))
        with pytest.raises(ValueError,match='different instruction'):store.create_idempotent_run(**{**args,'instruction':'Changed'},file_manifest=manifest)
        with pytest.raises(ActiveWorkActionConflictError):_file_input_reserve(store,service,work,list(reversed(inputs)))
        with pytest.raises(KeyError):store.reserve_active_work_action(tenant_id='tenant-a',owner_id='foreign-owner',work_ref=work['work_ref'],idempotency_key='synthetic-file-action',action='queue',payload_digest='different')
        assert _file_input_manifest(store,worker,args['run_id'])==accepted
        for index,(name,body,_) in enumerate(payloads):
            replay=service.files.create_upload('tenant-a','owner-a',draft_id='synthetic-action-draft',name=name,size_bytes=len(body),idempotency_key='synthetic-upload-'+str(index))
            assert replay['upload_id']==inputs[index]['upload_id'] and replay['state']=='ready'
        with pytest.raises(FileAdmissionError):service.files.create_upload('tenant-a','owner-a',draft_id='synthetic-action-draft',name='changed.csv',size_bytes=inputs[0]['size_bytes'],idempotency_key='synthetic-upload-0')
    finally:service.shutdown()


def test_active_work_file_real_message_running_race_does_not_publish_inputs(tmp_path,monkeypatch):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,_=_file_input_uploads(service);manifest=service._active_work_action_file_manifest(worker,_file_input_reserve(store,service,work,inputs,action='message'))
    try:
        result,created=store.replace_queued_run_idempotently(source_run_id=run['run_id'],replacement_run_id='run-message-race',worker_id=worker['worker_id'],project_id=worker['project_id'],instruction='New guidance',file_manifest=manifest)
        assert result is None and not created and store.get_run('run-message-race') is None
        assert _file_input_manifest(store,worker,run['run_id'])==[]
        assert list(Path(worker['workspace_dir']).rglob('*'))==[]
    finally:service.shutdown()


@pytest.fixture(autouse=True)
def _file_input_no_network(request, monkeypatch):
    if not request.node.name.startswith("test_active_work_file_real_"):
        return
    import httpx, socket
    def denied(*args,**kwargs):raise AssertionError('Private integration cannot use a network or API')
    monkeypatch.setattr(httpx.Client,'request',denied)
    monkeypatch.setattr(httpx.AsyncClient,'request',denied)
    monkeypatch.setattr(socket.socket,'connect',denied)


def test_active_work_file_real_concurrent_action_reservation_has_one_executor(tmp_path,monkeypatch):
    from threading import Barrier
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,_=_file_input_uploads(service);barrier=Barrier(2);results=[];errors=[]
    def reserve(executor):
        try:
            other=Store(store.db_path);barrier.wait(3)
            results.append(_file_input_reserve(other,service,work,inputs,executor=executor))
        except BaseException as exc:errors.append(exc)
    threads=[Thread(target=reserve,args=('synthetic-executor-'+str(i),)) for i in range(2)]
    try:
        for thread in threads:thread.start()
        for thread in threads:thread.join(5)
        assert not errors and all(not x.is_alive() for x in threads),errors
        assert len(results)==2 and len({x['action_use_id'] for x in results})==1
        assert sum(bool(x['should_execute']) for x in results)==1
        assert len(store.list_runs_for_worker(worker['worker_id']))==1
        with store._connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM active_work_action_uses').fetchone()[0]==1
            assert conn.execute('SELECT COUNT(*) FROM workspace_file_bindings').fetchone()[0]==0
    finally:service.shutdown()


def _file_input_owning_endpoint(store,service):
    import workers_projects_runtime.service as service_module
    from workers_projects_runtime.auth import AuthContext
    from workers_projects_runtime.models import ActiveWorkActionRequest
    from workers_projects_runtime.store import ActiveWorkActionConflictError
    from workers_projects_runtime.openclaw_runtime import HostCapacityError
    from fastapi import Request
    from hashlib import sha256
    tree=ast.parse(Path(service_module.__file__).with_name('api.py').read_text())
    names={'_auth_context','_account_scope','_canonical_digest','_active_work_state','active_work_action'}
    nodes=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes}==names
    for node in nodes:node.decorator_list=[]
    namespace={'store':store,'service':service,'AuthContext':AuthContext,'Request':Request,
               'ActiveWorkActionRequest':ActiveWorkActionRequest,'ActiveWorkActionConflictError':ActiveWorkActionConflictError,
               'FileAdmissionError':FileAdmissionError,'HostCapacityError':HostCapacityError,'HTTPException':HTTPException,'json':json,'sha256':sha256,'os':__import__('os')}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'actual-owning-active-work-endpoint','exec'),namespace)
    request=Request({'type':'http','method':'POST','path':'/private-pure-function','headers':[]})
    request.state.auth_context=AuthContext(tenant_id='tenant-a',user_id='owner-a',auth_mode='service_assertion')
    return namespace['active_work_action'],request,ActiveWorkActionRequest


def test_active_work_file_real_configured_file_count_policy_owns_admission(tmp_path,monkeypatch):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    try:
        with store._connect() as conn:assert service.files._policy(conn,'tenant-a','owner-a')['max_batch_files'] is None
        inputs=[]
        for i in range(65):
            upload=service.files.create_upload('tenant-a','owner-a',draft_id='synthetic-many-files',name='item-'+str(i)+'.txt',size_bytes=0,idempotency_key='many-file-'+str(i))
            async def empty():yield b''
            ready=asyncio.run(service.files.receive('tenant-a','owner-a',upload['upload_id'],empty()))
            assert ready['state']=='ready'
            item=service.files.manifest('tenant-a','owner-a',[ready['upload_id']])[0]
            inputs.append({'upload_id':item['upload_id'],'revision':item['revision'],'sha256':item['sha256'],'size_bytes':0,'filename':item['name'],
                           'source_file_id':'synthetic-many-source-'+str(i),'input_kind':'original'})
        from workers_projects_runtime.models import ActiveWorkActionRequest
        payload=ActiveWorkActionRequest(action='queue',instruction='Use the selected files',idempotency_key='synthetic-many-action',file_inputs=inputs,
            source_context={'version':1,'origin_ref':'synthetic-many-origin','source_event_id':'synthetic-many-event','source_revision':1,'surface':'web','output_contract':{'mode':'inherit'}})
        assert len(payload.file_inputs)==65
        with store._connect() as conn:conn.execute('INSERT INTO workspace_file_policies(tenant_id,owner_id,max_batch_files) VALUES(?,?,?)',('tenant-a','owner-a',2))
        for i in range(2):service.files.create_upload('tenant-a','owner-a',draft_id='synthetic-configured-batch',name='limited-'+str(i)+'.txt',size_bytes=0,idempotency_key='limited-file-'+str(i))
        with pytest.raises(FileAdmissionError) as limit:
            service.files.create_upload('tenant-a','owner-a',draft_id='synthetic-configured-batch',name='limited-2.txt',size_bytes=0,idempotency_key='limited-file-2')
        assert limit.value.status_code==413 and 'configured file count' in str(limit.value)
    finally:service.shutdown()


@pytest.mark.parametrize('invalid',['hash','not_ready','foreign_upload'])
def test_active_work_file_real_endpoint_denied_selection_has_no_control_or_file_changes(tmp_path,monkeypatch,invalid):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,_=_file_input_uploads(service)
    if invalid=='hash':inputs[0]['sha256']='0'*64
    elif invalid=='not_ready':service.files.cancel('tenant-a','owner-a',inputs[0]['upload_id'])
    else:
        with store._connect() as conn:conn.execute('UPDATE workspace_file_uploads SET owner_id=? WHERE upload_id=?',('synthetic-foreign-owner',inputs[0]['upload_id']))
    endpoint,request,Payload=_file_input_owning_endpoint(store,service)
    payload=Payload(action='steer',instruction='Use these files',idempotency_key='synthetic-invalid-action',file_inputs=inputs,
        source_context={'version':1,'origin_ref':'synthetic-origin','source_event_id':'synthetic-event','source_revision':1,'surface':'web','output_contract':{'mode':'inherit'}})
    try:
        with pytest.raises(HTTPException) as failure:endpoint(work['work_ref'],payload,request)
        assert failure.value.status_code==(404 if invalid=='foreign_upload' else 409)
        assert not runtime.entered.is_set()
        assert store.get_worker(worker['worker_id'])['compute_release_token']=='' and store.get_run(run['run_id'])['state']=='running'
        assert len(store.list_runs_for_worker(worker['worker_id']))==1
        with store._connect() as conn:assert conn.execute('SELECT COUNT(*) FROM workspace_file_bindings').fetchone()[0]==0
        assert list(Path(worker['workspace_dir']).rglob('*'))==[]
    finally:service.shutdown()


@pytest.mark.parametrize('drift',['revoked','revision_stale','scope_lost'])
def test_active_work_file_real_claim_pin_rejection_rolls_back_before_stop(tmp_path,monkeypatch,drift):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,_=_file_input_uploads(service);endpoint,request,Payload=_file_input_owning_endpoint(store,service)
    payload=Payload(action='steer',instruction='Use exactly these selected inputs',idempotency_key='synthetic-claim-pin-action',file_inputs=inputs,
        source_context={'version':1,'origin_ref':'synthetic-origin','source_event_id':'synthetic-event','source_revision':1,'surface':'web','output_contract':{'mode':'inherit'}})
    original=store.try_claim_worker_compute_release;entered=Event();release=Event();errors=[]
    def gate(*args,**kwargs):entered.set();assert release.wait(5);return original(*args,**kwargs)
    store.try_claim_worker_compute_release=gate
    def invoke():
        try:endpoint(work['work_ref'],payload,request)
        except BaseException as exc:errors.append(exc)
    thread=Thread(target=invoke);thread.start()
    try:
        assert entered.wait(3)
        if drift=='revoked':service.files.cancel('tenant-a','owner-a',inputs[0]['upload_id'])
        else:
            field,value=('metadata_revision',1) if drift=='revision_stale' else ('owner_id','synthetic-foreign-owner')
            with store._connect() as conn:conn.execute(f'UPDATE workspace_file_uploads SET {field}=? WHERE upload_id=?',(value,inputs[0]['upload_id']))
    finally:release.set();thread.join(5)
    try:
        assert not thread.is_alive() and len(errors)==1 and isinstance(errors[0],HTTPException)
        assert errors[0].status_code==(404 if drift=='scope_lost' else 409)
        assert errors[0].detail['code']=='file_operation_failed'
        assert not runtime.entered.is_set() and store.get_run(run['run_id'])['state']=='running'
        assert store.get_worker(worker['worker_id'])['compute_release_token']==''
        assert len(store.list_runs_for_worker(worker['worker_id']))==1
        with store._connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM workspace_file_run_inputs').fetchone()[0]==0
            assert conn.execute('SELECT COUNT(*) FROM workspace_file_pins').fetchone()[0]==0
            action=dict(conn.execute('SELECT * FROM active_work_action_uses').fetchone())
            assert action['status']=='failed'
            assert conn.execute('SELECT COUNT(*) FROM workspace_file_bindings').fetchone()[0]==0
        assert list(Path(worker['workspace_dir']).rglob('*'))==[]
    finally:service.shutdown()


@pytest.mark.parametrize('terminal_wins',[False,True])
def test_active_work_file_real_steer_queued_inputs_never_publish_before_stop(tmp_path,monkeypatch,terminal_wins):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch,block=True)
    inputs,payloads=_file_input_uploads(service);endpoint,request,Payload=_file_input_owning_endpoint(store,service)
    payload=Payload(action='steer',instruction='Use exactly these selected inputs',idempotency_key='synthetic-steer-queued-action',file_inputs=inputs,
        source_context={'version':1,'origin_ref':'synthetic-origin','source_event_id':'synthetic-event','source_revision':1,'surface':'web','output_contract':{'mode':'inherit'}})
    results=[];errors=[]
    def invoke():
        try:results.append(endpoint(work['work_ref'],payload,request))
        except BaseException as exc:errors.append(exc)
    thread=Thread(target=invoke);thread.start()
    try:
        assert runtime.entered.wait(3);claimed=store.get_worker(worker['worker_id']);replacement=claimed['compute_release_replacement_run_id'];assert replacement
        manifest=_file_input_manifest(store,worker,replacement)
        assert [(x['source_file_id'],x['input_kind'],x['media_group_index']) for x in manifest]==[(x['source_file_id'],x['input_kind'],x['media_group_index']) for x in inputs]
        assert store.claim_next_queued_run(worker['worker_id']) is None
        with pytest.raises(FileAdmissionError):service.files.cancel('tenant-a','owner-a',inputs[0]['upload_id'])
        with store._connect() as conn:assert conn.execute('SELECT COUNT(*) FROM workspace_file_bindings').fetchone()[0]==0
        assert list(Path(worker['workspace_dir']).rglob('*'))==[]
        if terminal_wins:_file_input_complete(Store(store.db_path),run)
    finally:runtime.release.set();thread.join(5)
    try:
        assert not thread.is_alive() and not errors,errors
        assert store.get_worker(worker['worker_id'])['compute_release_token']==''
        assert _file_input_manifest(store,worker,replacement)==manifest
        if terminal_wins:
            assert results[0]['controlOutcome']=='terminal_won' and results[0]['fileDisposition']=='not_bound_target_finished'
            assert store.get_run(replacement)['state']=='cancelled' and store.get_run(run['run_id'])['output_text']=='Synthetic original draft'
            assert store.claim_next_queued_run(worker['worker_id']) is None
        else:assert store.get_run(run['run_id'])['state']=='interrupted' and store.get_run(replacement)['state']=='queued'
        assert list(Path(worker['workspace_dir']).rglob('*'))==[]
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM workspace_file_pins WHERE kind='run' AND reference_id=?",(replacement,)).fetchone()[0]==2
            assert conn.execute('SELECT COUNT(*) FROM workspace_file_bindings').fetchone()[0]==0
        replay=endpoint(work['work_ref'],payload,request);assert replay['idempotentReplay']
        changed=payload.model_copy(update={'file_inputs':list(reversed(payload.file_inputs))})
        with pytest.raises(HTTPException) as conflict:endpoint(work['work_ref'],changed,request)
        assert conflict.value.status_code==409 and conflict.value.detail['code']=='active_work_idempotency_conflict'
        assert len(store.list_runs_for_worker(worker['worker_id']))==2
    finally:service.shutdown()
    reopened=Store(store.db_path);assert _file_input_manifest(reopened,worker,replacement)==manifest
    if not terminal_wins:
        again=WorkersProjectsService(reopened,runtime,reconcile_on_startup=False,start_background_consumers=False);again._ensure_worker_processor=lambda _id:None
        try:
            admitted=_file_input_admit_next(reopened,worker);assert admitted['run_id']==replacement
            selected=again._run_local_worker(reopened.get_worker(worker['worker_id']),admitted);bundle=json.loads(selected['bootstrap_bundle_json'])
            index=json.loads(next(x['content'] for x in bundle['files'] if x['path']==selected['_run_input_manifest_path']))['files']
            assert [(x['source_file_id'],x['input_kind'],x['media_group_index']) for x in index]==[(x['source_file_id'],x['input_kind'],x['media_group_index']) for x in inputs]
            assert [x['name'] for x in index]==[name for name,_,_ in payloads]
            assert selected['_run_input_manifest_path'] in again._runtime_instruction_for_run(selected,'Use the selected files')
            entries=[x for x in bundle['files'] if x.get('managed_upload_id')];assert len(entries)==2
            for entry,(_,body,_) in zip(entries,payloads):materialize_managed_file(Path(worker['workspace_dir']),entry,selected);assert (Path(worker['workspace_dir'])/entry['path']).read_bytes()==body
        finally:again.shutdown()


@pytest.mark.parametrize('recovery',['interrupted','expired'])
def test_active_work_file_real_steer_pinned_inputs_restart_exact_fence(tmp_path,monkeypatch,recovery):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch,crash=True)
    inputs,_=_file_input_uploads(service);reservation=_file_input_reserve(store,service,work,inputs,action='steer')
    try:
        with pytest.raises(RuntimeErrorBase,match='synthetic steer control crash'):
            service.execute_active_work_action(work,action='steer',instruction='Use exactly these selected inputs',idempotency_key='synthetic-file-action',action_use_id=reservation['action_use_id'],start_processor=False)
        claimed=store.get_worker(worker['worker_id']);replacement=claimed['compute_release_replacement_run_id'];manifest=_file_input_manifest(store,worker,replacement);assert len(manifest)==2
        assert store.claim_next_queued_run(worker['worker_id']) is None
        if recovery=='interrupted':
            assert store.confirm_worker_control_runtime_effect(worker['worker_id'],claimed['compute_release_token'],claimed['compute_release_epoch'],kind='steer_run',target_run_id=run['run_id'])
            assert store.finalize_run_if_state(run['run_id'],'running','interrupted',error_text='Exact provider stopped',**_exact_terminal_generation(store,run['run_id']))
        else:
            with store._connect() as conn:conn.execute('UPDATE workers SET compute_release_expires_at=? WHERE worker_id=?',('2000-01-01T00:00:00+00:00',worker['worker_id']))
    finally:service.shutdown()
    reopened=Store(store.db_path);again=WorkersProjectsService(reopened,_FileInputPrivateRuntime(tmp_path/'runtime'),reconcile_on_startup=False,start_background_consumers=False);again._ensure_worker_processor=lambda _id:None
    try:
        if recovery=='interrupted':assert again._settle_interrupted_steer_claim(worker['worker_id'],run['run_id']) is not None
        else:assert [x['kind'] for x in again.recover_expired_compute_release_claims_once()]==['steer_run']
        assert _file_input_manifest(reopened,worker,replacement)==manifest
        assert reopened.get_worker(worker['worker_id'])['compute_release_token']==''
        assert reopened.get_run(run['run_id'])['state']=='interrupted'
        admitted=_file_input_admit_next(reopened,worker);assert admitted['run_id']==replacement
        assert reopened.claim_next_queued_run(worker['worker_id']) is None
        selected=again._run_local_worker(reopened.get_worker(worker['worker_id']),admitted)
        index=json.loads(next(x['content'] for x in json.loads(selected['bootstrap_bundle_json'])['files'] if x['path']==selected['_run_input_manifest_path']))['files']
        assert [x['input_kind'] for x in index]==['original','extracted_text']
        assert [x['source_file_id'] for x in index]==[x['source_file_id'] for x in inputs]
    finally:again.shutdown()


@pytest.mark.parametrize('state',['claimed','admitted','running','settling','paused','needs_input'])
def test_active_work_file_real_replacement_scheduler_rejects_old_blocking_source(tmp_path,monkeypatch,state):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    try:
        store.create_run(worker['worker_id'],worker['project_id'],'Never run ahead')
        with store._connect() as conn:conn.execute('UPDATE runs SET state=? WHERE run_id=?',(state,run['run_id']))
        assert store.claim_next_queued_run(worker['worker_id']) is None
    finally:service.shutdown()


def _file_input_process_one(service,worker_id):
    with service._processors_lock:service._active_processors.add(worker_id);service._processor_generations[worker_id]=1
    service._process_worker_queue_parallel(worker_id,1)


def test_active_work_file_real_scope_loss_after_pin_settles_typed_failed_run(tmp_path,monkeypatch):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,_=_file_input_uploads(service);reservation=_file_input_reserve(store,service,work,inputs,action='steer')
    try:
        result=service.execute_active_work_action(work,action='steer',instruction='Use exactly these selected inputs',idempotency_key='synthetic-file-action',action_use_id=reservation['action_use_id'],start_processor=False)
        replacement=result['run_id'];assert len(_file_input_manifest(store,worker,replacement))==2
        with store._connect() as conn:conn.execute('UPDATE workspace_file_uploads SET owner_id=? WHERE upload_id=?',('synthetic-foreign-owner',inputs[0]['upload_id']))
        _file_input_process_one(service,worker['worker_id'])
        failed=store.get_run(replacement);assert failed['state']=='failed',failed
        assert failed['failure_class']=='file_operation_failed' and failed['failure_structured'] and not failed['failure_retryable']
        assert failed['failure_class']!='service_processor_unexpected'
        assert store.get_worker(worker['worker_id'])['compute_release_token']==''
        assert store.claim_next_queued_run(worker['worker_id']) is None
        assert list(Path(worker['workspace_dir']).rglob('*'))==[]
    finally:service.shutdown()
    reopened=Store(store.db_path);assert reopened.get_run(replacement)['state']=='failed'


@pytest.mark.parametrize('status',[429,503])
def test_active_work_file_real_transient_file_admission_requeues_then_recovers(tmp_path,monkeypatch,status):
    store,service,work,worker,run,runtime=_file_input_fixture(tmp_path,monkeypatch)
    inputs,_=_file_input_uploads(service);reservation=_file_input_reserve(store,service,work,inputs,action='steer')
    try:
        result=service.execute_active_work_action(work,action='steer',instruction='Use exactly these selected inputs',idempotency_key='synthetic-file-action',action_use_id=reservation['action_use_id'],start_processor=False);replacement=result['run_id']
        original=service.files.register_run_input_versions
        def unavailable(*args,**kwargs):raise FileAdmissionError('Synthetic file admission unavailable',status,code='synthetic_file_unavailable')
        service.files.register_run_input_versions=unavailable
        _file_input_process_one(service,worker['worker_id'])
        queued=store.get_run(replacement);assert queued['state']=='queued',queued
        assert queued['failure_class']=='synthetic_file_unavailable' and queued['failure_retryable'] and queued['failure_structured']
        assert queued['retry_attempts']==1
        assert store.get_worker(worker['worker_id'])['compute_release_token']==''
        service.files.register_run_input_versions=original
        with store._connect() as conn:conn.execute('UPDATE runs SET retry_after=NULL WHERE run_id=?',(replacement,))
        admitted=_file_input_admit_next(store,worker);assert admitted['run_id']==replacement
        selected=service._run_local_worker(store.get_worker(worker['worker_id']),admitted);assert selected['_run_input_manifest_path']
        assert store.get_run(replacement)['state']=='admitted'
    finally:service.shutdown()
