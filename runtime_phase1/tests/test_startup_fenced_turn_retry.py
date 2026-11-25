"""A turn that a restart interrupted is requeued by the startup fence; it must start again soon, not wait out its deadline."""
from workers_projects_runtime.store import Store


def _fenced_turn(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Synthetic conversation", "Restart recovery.", "claude-code")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner", name="Synthetic coordinator",
        role="conversation-agent", profile="claude-code", backend="claude-code",
        runtime="claude-code", model="stub/claude-code",
    )
    # Compute released across the restart: the worker reads "paused", with no pause event and no paused run.
    store.update_worker(worker["worker_id"], state="paused")
    run = store.create_run(worker["worker_id"], project["project_id"], "Resume this turn.", state="queued")
    # The fields the startup fence writes on the interrupted turn (store.requeue_unconfirmed_host_run_start).
    store.update_run(
        run["run_id"], retry_after="2000-01-01T00:00:00+00:00",
        failure_retryable=1, failure_structured=1,
        failure_class="service_startup_fenced", last_retry_class="service_startup_fenced",
        queue_blocker_class="running_invariant_reconciled",
    )
    return store, worker, run


def test_a_turn_requeued_by_the_startup_fence_is_an_automatic_retry(tmp_path):
    store, worker, _ = _fenced_turn(tmp_path)
    assert store.has_queued_running_invariant_retry(worker["worker_id"])


def test_its_worker_is_made_ready_so_the_scheduler_starts_it(tmp_path):
    store, worker, run = _fenced_turn(tmp_path)
    projected = store.reconcile_automatic_retry_worker(worker["worker_id"])
    assert projected is not None and projected["state"] == "ready"
    assert store.get_run(run["run_id"])["state"] == "queued"
