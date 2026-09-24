"""Preserve durable operator Pause while interrupted dispatch retries recover."""
from threading import Event, Thread
from workers_projects_runtime.store import Store
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.openclaw_runtime import StubRuntime, RuntimeInfo
from test_api import create_truthfully_invoked_test_run, wait_until

def test_startup_reconcile_restarts_crash_requeued_run_but_preserves_operator_pause(
    tmp_path,
):
    class MissingReconcileProcessRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Crash Restart Recovery",
        "Restart work whose prior dispatch ownership was lost.",
        "codex-cli",
    )

    def crashed_running_worker(name: str, suffix: str) -> tuple[dict, dict]:
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name=name,
            role="host worker",
            profile="codex-cli",
            backend="codex-cli",
            runtime="codex-cli",
            model="stub/codex-cli",
        )
        run = create_truthfully_invoked_test_run(
            store,
            worker,
            project["project_id"],
            f"Finish {suffix} after process restart",
            suffix=suffix,
            executor_id=f"crashed-executor-{suffix}",
        )
        lease = store.get_active_host_run_lease_for_run(run["run_id"])
        assert lease is not None
        released = store.release_host_run_lease(
            lease["lease_id"],
            executor_id=f"crashed-executor-{suffix}",
            reason="synthetic_process_crash",
        )
        assert released is not None
        return worker, run

    _, automatic_run = crashed_running_worker(
        "Automatically Recovered Worker", "automatic"
    )
    paused_worker, paused_run = crashed_running_worker(
        "Operator Paused Worker", "operator-paused"
    )
    failed_paused_worker, failed_paused_run = crashed_running_worker(
        "Failed State Operator Paused Worker", "failed-operator-paused"
    )
    store.add_event(
        project["project_id"],
        paused_worker["worker_id"],
        paused_run["run_id"],
        "worker.paused",
        "Worker paused",
    )
    # Reproduce a crash split where durable operator intent landed before the
    # worker/run projection. Startup must honor that event from any worker state.
    assert (store.get_worker(paused_worker["worker_id"]) or {})["state"] == "running"
    store.add_event(
        project["project_id"],
        failed_paused_worker["worker_id"],
        failed_paused_run["run_id"],
        "worker.paused",
        "Worker paused",
    )
    store.update_worker_state(failed_paused_worker["worker_id"], "failed")

    service = WorkersProjectsService(
        store,
        MissingReconcileProcessRuntime(),
        reconcile_on_startup=True,
    )
    try:
        wait_until(
            lambda: (store.get_run(automatic_run["run_id"]) or {}).get("state")
            == "completed",
            timeout=3,
        )

        recovered = store.get_run(automatic_run["run_id"])
        assert recovered is not None
        assert recovered["output_text"].startswith("STUB_OK:")
        recovered_attempts = store.list_run_attempts(automatic_run["run_id"])
        assert [attempt["state"] for attempt in recovered_attempts] == [
            "retry_queued",
            "completed",
        ]
        assert recovered_attempts[0]["terminal_reason"] == (
            "running_invariant_reconciled"
        )

        preserved = store.get_run(paused_run["run_id"])
        assert preserved is not None
        assert preserved["state"] == "queued"
        assert preserved["last_retry_class"] == "running_invariant_reconciled"
        assert (store.get_worker(paused_worker["worker_id"]) or {})["state"] == (
            "paused"
        )
        assert len(store.list_run_attempts(paused_run["run_id"])) == 1
        failed_preserved = store.get_run(failed_paused_run["run_id"])
        assert failed_preserved is not None
        assert failed_preserved["state"] == "queued"
        assert (store.get_worker(failed_paused_worker["worker_id"]) or {})[
            "state"
        ] == "paused"
        assert len(store.list_run_attempts(failed_paused_run["run_id"])) == 1
    finally:
        service.shutdown()


def test_restart_retry_reconciliation_cannot_overwrite_concurrent_operator_pause(
    tmp_path,
    monkeypatch,
):
    class NoSchedulerWorkersProjectsService(WorkersProjectsService):
        def _process_scheduler_cycle(self) -> None:
            return

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Atomic Restart Recovery",
        "Preserve operator control while restart recovery races.",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Concurrently Paused Worker",
        role="host worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    run = create_truthfully_invoked_test_run(
        store,
        worker,
        project["project_id"],
        "Do not resume after the operator pause wins.",
        suffix="concurrent-pause",
        executor_id="crashed-executor-concurrent-pause",
    )
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    assert lease is not None
    assert store.release_host_run_lease(
        lease["lease_id"],
        executor_id="crashed-executor-concurrent-pause",
        reason="synthetic_process_crash",
    )

    service = NoSchedulerWorkersProjectsService(
        store,
        StubRuntime(),
        reconcile_on_startup=False,
    )
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    entered_retry_projection = Event()
    allow_retry_projection = Event()
    original_reconcile = store.reconcile_automatic_retry_worker

    def reconcile_after_concurrent_pause(worker_id: str):
        entered_retry_projection.set()
        assert allow_retry_projection.wait(2)
        return original_reconcile(worker_id)

    monkeypatch.setattr(
        store,
        "reconcile_automatic_retry_worker",
        reconcile_after_concurrent_pause,
    )
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            service.reconcile_all_workers()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = Thread(target=reconcile)
    thread.start()
    try:
        assert entered_retry_projection.wait(2)
        store.add_event(
            project["project_id"],
            worker["worker_id"],
            run["run_id"],
            "worker.paused",
            "Worker paused",
        )
        store.update_worker_state(worker["worker_id"], "paused")
        allow_retry_projection.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert errors == []
        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "paused"
        durable_run = store.get_run(run["run_id"])
        assert durable_run is not None
        assert durable_run["state"] == "queued"
        assert durable_run["last_retry_class"] == "running_invariant_reconciled"
        assert len(store.list_run_attempts(run["run_id"])) == 1
    finally:
        allow_retry_projection.set()
        thread.join(timeout=2)
        service.shutdown()




def test_retry_projection_preserves_concurrently_admitted_generation(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Synthetic retry", "Preserve dispatch.", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner", name="Synthetic worker",
        role="host worker", profile="codex-cli", backend="codex-cli",
        runtime="codex-cli", model="stub/codex-cli",
    )
    retry = store.create_run(worker["worker_id"], project["project_id"], "Retry this later.", state="queued")
    store.update_run(
        retry["run_id"], retry_after="2099-01-01T00:00:00+00:00",
        failure_retryable=1, failure_structured=1,
        failure_class="running_invariant_reconciled",
        last_retry_class="running_invariant_reconciled",
        queue_blocker_class="running_invariant_reconciled",
    )
    active = create_truthfully_invoked_test_run(
        store, worker, project["project_id"], "Keep this dispatch active.",
        suffix="concurrent-active", executor_id="synthetic-active-owner",
    )
    projected = store.reconcile_automatic_retry_worker(worker["worker_id"])
    assert projected["state"] == "running"
    assert store.get_run(active["run_id"])["state"] == "running"
    assert store.get_active_host_run_lease_for_run(active["run_id"])["status"] == "active"
