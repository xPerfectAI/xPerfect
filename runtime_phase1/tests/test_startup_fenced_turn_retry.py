"""A turn that a restart interrupted is requeued; it must start again soon, not wait out its deadline, and starting
again must repeat nothing it already did: a coordinator turn run again adds no goals once it has made some."""
import uuid
from types import SimpleNamespace

import pytest

from workers_projects_runtime.coordinator import (
    CoordinatorConfig, CoordinatorConflict, CoordinatorService, Dispatch, Goal, Route,
)
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
    """A brief run again replays its own goals and dispatches with the same ids: its helpers already exist (one
    finished, one running, one still queued): no second run, delegation or start of any of them."""
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


class _Job:
    """A job conversation on the real store, service and coordinator. Its `job-brief` turn runs as `brief_run` on its
    own conversation worker, and calls the coordinator's tools as that run's live attempt (the native principal)."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
        monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.6-sol")
        monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
        self.store = store = Store(tmp_path / "job.sqlite3")

        class ReadyRuntime(StubRuntime):
            def isolated_parallel_readiness(self):
                return {"ready": True, "reason": ""}

        self.service = WorkersProjectsService(store, ReadyRuntime(), reconcile_on_startup=False)
        self.service.start_assigned_run = lambda _: None
        monkeypatch.setattr(self.service, "_storage_pressure_v1", lambda: {"healthy": True})
        self.core = CoordinatorService(store, self.service, _Provider())
        self.cid = self.core.create("local", "owner", CoordinatorConfig(
            model="exact-native-model", effort="high", routes=[Route(
                id="route", profile="codex-cli", model="codex-cli:gpt-5.6-sol", effort="high",
                execution_mode="docker")]))["conversation_id"]
        self.core.accept_turn("local", "owner", self.cid, "job-brief", _BRIEF)
        project = store.create_project("owner", "Synthetic conversation", "Restart recovery.", "claude-code")
        self.worker = store.create_worker(
            project_id=project["project_id"], owner_id="owner", name="Synthetic coordinator",
            role="conversation-agent", profile="claude-code", backend="claude-code",
            runtime="claude-code", model="stub/claude-code")
        self.brief_run = store.create_run(self.worker["worker_id"], project["project_id"], "The brief.", state="queued")
        session = store.upsert_provider_session(
            tenant_id="local", owner_id="owner", conversation_id=self.cid, agent_id="coordinator",
            model_id="exact-native-model", project_id=project["project_id"], worker_id=self.worker["worker_id"],
            workspace_dir=str(tmp_path), access_mode="workspace")
        request, _ = store.create_provider_request(
            tenant_id="local", owner_id="owner", session_id=session["session_id"],
            idempotency_key=f"{self.cid}:job-brief", message_id="job-brief", stream_id="job-brief",
            requested_history_count=1)
        with store._connect() as conn:  # the links the provider writes when the turn starts
            conn.execute("UPDATE provider_requests SET run_id=?,state='running' WHERE request_id=?",
                         (self.brief_run["run_id"], request["request_id"]))
            conn.execute("UPDATE coordinator_turns SET request_id=? WHERE conversation_id=? AND turn_id='job-brief'",
                         (request["request_id"], self.cid))

    def attempt(self, number):
        """The brief's run begins its attempt `number` (a claim writes this row); returns the tools' principal."""
        attempt_id = "att_" + uuid.uuid4().hex
        with self.store._connect() as conn:
            conn.execute("INSERT INTO run_attempts(attempt_id,run_id,attempt_number,state,claimed_at,lease_id,"
                         "capacity_available_json,capacity_required_json,capacity_shortage_json,"
                         "capacity_reservation_json) VALUES(?,?,?,'running',?,'','{}','{}','{}','{}')",
                         (attempt_id, self.brief_run["run_id"], number,
                          __import__("workers_projects_runtime.models", fromlist=["utc_now"]).utc_now()))
            conn.execute("UPDATE runs SET active_attempt_id=? WHERE run_id=?", (attempt_id, self.brief_run["run_id"]))
        return {"tenant_id": "local", "owner_id": "owner", "worker_id": self.worker["worker_id"],
                "run_id": self.brief_run["run_id"], "attempt_id": attempt_id}

    def tool_calls(self, principal, goals):
        """What the brief does with its tools: accept goals, then dispatch each one it named. A dispatch of a goal
        that does not exist is refused like any other unknown goal."""
        self.core.accept_native_goals(principal, goals)
        for goal in goals:
            try:
                self.core.dispatch("local", "owner", self.cid, Dispatch(goal_id=goal.id, route_id="route"))
            except CoordinatorConflict:
                pass

    def restart(self, path):
        """The two restart paths seen live: the startup fence with the worker read paused, and the running-invariant
        repair with the worker ready. Either way the brief's run is queued again and runs as a new attempt."""
        failure = {"fenced_paused": "service_startup_fenced", "reconciled_ready": "running_invariant_reconciled"}[path]
        self.store.update_worker(self.worker["worker_id"], state="paused" if path == "fenced_paused" else "ready")
        self.store.update_run(
            self.brief_run["run_id"], retry_after="2000-01-01T00:00:00+00:00", failure_retryable=1,
            failure_structured=1, failure_class=failure, last_retry_class=failure,
            queue_blocker_class="running_invariant_reconciled")
        if path == "fenced_paused":
            assert self.store.reconcile_automatic_retry_worker(self.worker["worker_id"])["state"] == "ready"
        assert self.store.get_worker(self.worker["worker_id"])["state"] == "ready"

    def counts(self):
        with self.store._connect() as conn:
            goals = conn.execute("SELECT COUNT(*) FROM coordinator_goals WHERE conversation_id=?", (self.cid,)).fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id IN (SELECT run_id FROM coordinator_goals "
                                "WHERE conversation_id=? AND run_id<>'')", (self.cid,)).fetchone()[0]
            delegations = conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0]
        return goals, runs, delegations

    def close(self):
        self.service.shutdown()
        self.store.close()


def _again(goals):
    """The same work named anew."""
    return [Goal(id=goal.id + "-again", text=goal.text) for goal in goals]


@pytest.mark.parametrize("path", ["fenced_paused", "reconciled_ready"])
def test_a_brief_run_again_under_new_goal_ids_starts_no_second_helper(tmp_path, monkeypatch, path):
    """The brief made and dispatched its three goals, then the restart came. Run again, it names the same work
    under new ids: it gets back the three goals it already has, and nothing new is made or started."""
    job = _Job(tmp_path, monkeypatch)
    try:
        job.tool_calls(job.attempt(1), _GOALS)
        assert job.counts() == (3, 3, 3)
        job.restart(path)
        job.tool_calls(job.attempt(2), _again(_GOALS))
        assert job.counts() == (3, 3, 3)
    finally:
        job.close()


def test_a_brief_with_no_goals_yet_is_run_again_and_makes_them(tmp_path, monkeypatch):
    """The restart came before the brief made any goal: run again, it makes and dispatches them."""
    job = _Job(tmp_path, monkeypatch)
    try:
        job.attempt(1)
        job.restart("fenced_paused")
        job.tool_calls(job.attempt(2), _GOALS)
        assert job.counts() == (3, 3, 3)
    finally:
        job.close()


def test_one_attempt_may_add_goals_in_several_calls(tmp_path, monkeypatch):
    """Within one attempt nothing changes: a second call of the same run adds its goals."""
    job = _Job(tmp_path, monkeypatch)
    try:
        principal = job.attempt(1)
        job.tool_calls(principal, _GOALS[:2])
        job.tool_calls(principal, _GOALS[2:])
        assert job.counts() == (3, 3, 3)
    finally:
        job.close()


def test_a_brief_that_had_made_two_of_its_three_goals_keeps_two(tmp_path, monkeypatch):
    """What the guard costs: a brief stopped after making two of its three goals cannot add the third when it is run
    again. It gets back its two; a later turn of the job (its results) can still start more work."""
    job = _Job(tmp_path, monkeypatch)
    try:
        job.tool_calls(job.attempt(1), _GOALS[:2])
        job.restart("reconciled_ready")
        job.tool_calls(job.attempt(2), _GOALS)
        assert job.counts() == (2, 2, 2)
    finally:
        job.close()


def _given_with_the_turn(job):
    """A client gave one goal with the turn (POST .../turns with goals, or POST .../goals), well before its run."""
    job.core.accept_goals("local", "owner", job.cid, "job-brief", [Goal(id="given", text="Given with the turn")])
    with job.store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET created_at='2000-01-01T00:00:00+00:00' WHERE goal_id='given'")


def test_goals_given_with_the_turn_leave_its_first_attempt_free_to_make_its_own(tmp_path, monkeypatch):
    """Goals that came with the turn were not made by an earlier attempt: the first attempt makes and starts its
    three. (Before, it got back the given goal only and its own were silently dropped.)"""
    job = _Job(tmp_path, monkeypatch)
    try:
        _given_with_the_turn(job)
        job.tool_calls(job.attempt(1), _GOALS)
        assert job.counts() == (4, 3, 3)
    finally:
        job.close()


@pytest.mark.parametrize("path", ["fenced_paused", "reconciled_ready"])
def test_with_given_goals_a_restart_before_its_own_still_makes_them_and_one_after_adds_none(tmp_path, monkeypatch, path):
    job = _Job(tmp_path, monkeypatch)
    try:
        _given_with_the_turn(job)
        job.attempt(1)  # the restart came before the brief made any goal of its own
        job.restart(path)
        job.tool_calls(job.attempt(2), _GOALS)
        assert job.counts() == (4, 3, 3)
        job.restart(path)  # and again, now after it made them: named anew, nothing new is made or started
        job.tool_calls(job.attempt(3), _again(_GOALS))
        assert job.counts() == (4, 3, 3)
    finally:
        job.close()
