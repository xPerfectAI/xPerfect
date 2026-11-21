"""A provider route that keeps failing is called less and less often, until it works again."""
from datetime import datetime, timedelta, timezone

from workers_projects_runtime.store import Store

ROUTE = dict(tenant_id="local", owner_id="owner", profile="claude-code", runtime="claude-code",
             model="synthetic-model")
START = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _fail(store, at, n, **extra):
    return store.record_provider_route_failure(
        **ROUTE, failure_class="provider_rate_limited", failure_structured=True,
        default_cooldown_s=300, run_id=f"run-{n}", evidence_id=f"evidence-{n}", now=at, **extra)


def _wait_s(row, at):
    return (datetime.fromisoformat(row["cooldown_until"]) - at).total_seconds()


def test_each_failure_in_a_row_doubles_the_wait_up_to_an_hour(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    at, waits = START, []
    for n in range(7):
        row = _fail(store, at, n)
        waits.append(_wait_s(row, at))
        at = at + timedelta(seconds=waits[-1] + 1)
        assert store.get_provider_route_health(**ROUTE, now=at) is None  # the circuit reopens
    assert waits == [300, 600, 1200, 2400, 3600, 3600, 3600]


def test_a_completed_run_on_the_route_resets_the_wait(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    at = START
    for n in range(3):
        row = _fail(store, at, n)
        at = at + timedelta(seconds=_wait_s(row, at) + 1)
    project = store.create_project("owner", "p", "Route subject.", "claude-code")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner", name="w", role="conversation-agent",
        profile="claude-code", backend="", runtime="claude-code", model="synthetic-model",
        execution_mode="host", trusted_run_lane="conversation", bootstrap_bundle={})
    run = store.create_run(worker["worker_id"], project["project_id"], "work", state="queued")
    store.update_run(run["run_id"], state="completed", ended_at=at.isoformat())
    later = at + timedelta(seconds=30)
    assert _wait_s(_fail(store, later, 99), later) == 300


def test_a_time_given_by_the_provider_is_used_as_is(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    at = START
    for n in range(3):
        row = _fail(store, at, n)
        at = at + timedelta(seconds=_wait_s(row, at) + 1)
    row = _fail(store, at, 50, retry_after_s=42)
    assert _wait_s(row, at) == 42 and row["cooldown_source"] == "provider_exact"


def test_another_route_keeps_its_own_count(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    at = START
    for n in range(3):
        row = _fail(store, at, n)
        at = at + timedelta(seconds=_wait_s(row, at) + 1)
    other = store.record_provider_route_failure(
        **{**ROUTE, "profile": "codex-cli", "runtime": "codex-cli"},
        failure_class="provider_rate_limited", failure_structured=True, default_cooldown_s=300,
        run_id="run-other", evidence_id="evidence-other", now=at)
    assert _wait_s(other, at) == 300
