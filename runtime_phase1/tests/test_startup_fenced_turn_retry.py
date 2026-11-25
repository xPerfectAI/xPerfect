"""A turn that a restart interrupted is requeued by the startup fence; it must start again soon, not wait out its deadline,
and starting again must repeat nothing it already did."""
from types import SimpleNamespace

from workers_projects_runtime.coordinator import CoordinatorConfig, CoordinatorService, Dispatch, Goal, Route
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
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


class _Provider:
    def _model(self, model, *, tenant_id="local", owner_id=""):
        if model != "exact-native-model":
            raise ValueError("unsupported model")
        return SimpleNamespace(effort_choices=("high",))

    def start(self, payload, **kwargs):
        raise ConnectionError("synthetic provider")


_BRIEF = "Create three files at once, one per helper: ALPHA.md on mutexes, BETA.md on semaphores, GAMMA.md on monitors."
_GOALS = [Goal(id="alpha", text="ALPHA.md on mutexes"), Goal(id="beta", text="BETA.md on semaphores"),
          Goal(id="gamma", text="GAMMA.md on monitors")]


def _brief_turn_tool_calls(core, cid):
    """What the job-brief turn does with its tools: accept its goals, then dispatch each one."""
    core.accept_goals("local", "owner", cid, "job-brief", _GOALS)
    return [core.dispatch("local", "owner", cid, Dispatch(goal_id=goal.id, route_id="route")) for goal in _GOALS]


def test_a_job_brief_run_again_after_a_restart_starts_no_second_helper(tmp_path, monkeypatch):
    """The fenced job-brief turn runs again on its saved session and replays its goals and dispatches. Its
    helpers already exist (one finished, one running, one still queued): no second run, delegation or start of
    any of them, and each keeps its state."""
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.6-sol")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(tmp_path / "restart.sqlite3")

    class ReadyRuntime(StubRuntime):
        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(store, ReadyRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _: None
    monkeypatch.setattr(service, "_storage_pressure_v1", lambda: {"healthy": True})
    try:
        core = CoordinatorService(store, service, _Provider())
        cid = core.create("local", "owner", CoordinatorConfig(model="exact-native-model", effort="high", routes=[
            Route(id="route", profile="codex-cli", model="codex-cli:gpt-5.6-sol", effort="high",
                  execution_mode="docker")]))["conversation_id"]
        core.accept_turn("local", "owner", cid, "job-brief", _BRIEF)
        first = _brief_turn_tool_calls(core, cid)
        assert [goal["state"] for goal in first] == ["queued"] * 3, [goal["blocker"] for goal in first]
        runs = [goal["run_id"] for goal in first]
        with store._connect() as conn:
            conn.execute("UPDATE runs SET state='completed',output_text='ALPHA.md written.' WHERE run_id=?", (runs[0],))
            conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (runs[1],))
        states = {run_id: store.get_run(run_id)["state"] for run_id in runs}
        assert list(states.values()) == ["completed", "running", "queued"]

        # The restart: a new coordinator recovers its dispatches, then the fenced job-brief turn runs again.
        restarted = CoordinatorService(store, service, _Provider())
        restarted.recover_dispatches("local", "owner", cid)
        starts = []
        service.start_assigned_run = starts.append
        again = _brief_turn_tool_calls(restarted, cid)

        assert [goal["run_id"] for goal in again] == runs
        assert {run_id: store.get_run(run_id)["state"] for run_id in runs} == states
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM coordinator_goals WHERE conversation_id=?",
                                (cid,)).fetchone()[0] == 3
        # A replayed dispatch only wakes its helper's worker, which runs a queued run and nothing else:
        # the finished and the running helper have none, the queued one gets its first start.
        assert sorted(starts) == sorted(store.get_run(run_id)["worker_id"] for run_id in runs)
        next_runs = {worker_id: (store.peek_next_queued_run(worker_id) or {}).get("run_id") for worker_id in starts}
        assert sorted(filter(None, next_runs.values())) == [runs[2]]
    finally:
        service.shutdown()
        store.close()
