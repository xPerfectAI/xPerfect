from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

import pytest

from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


@pytest.fixture
def inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        owner_id="synthetic-owner", title="Recovery inventory", goal="Preserve work",
        default_worker_profile="codex-cli",
    )
    workers = {}
    for index, state in enumerate(
        ["ready", "needs_input", "paused", "terminated", "failed", "running", "starting", "stopping"]
    ):
        worker = store.create_worker(
            project_id=project["project_id"], owner_id="synthetic-owner",
            name=f"Synthetic {state}", role="Inventory fixture", profile="codex-cli",
            backend="stub", runtime="stub", model="test-model",
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET state=?,created_at=?,bootstrap_bundle_json=? WHERE worker_id=?",
                (state, f"2026-01-01T00:00:{index:02d}+00:00", '{"fixture":"' + "x" * 65536 + '"}', worker["worker_id"]),
            )
        workers[state] = worker["worker_id"]
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        yield store, service, workers
    finally:
        service.shutdown()
        store.close()


def test_inventory_query_projects_only_ids_in_original_order(inventory, monkeypatch):
    store, _service, _workers = inventory
    expected = [worker["worker_id"] for worker in store.list_all_workers()]
    statements = []
    connect = store._connect

    @contextmanager
    def traced_connect():
        with connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    assert store.list_worker_ids() == expected
    inventory_query = next(statement for statement in statements if "FROM workers" in statement)
    assert "SELECT worker_id" in inventory_query
    assert "bootstrap_bundle_json" not in inventory_query
    assert "SELECT *" not in inventory_query


@pytest.mark.parametrize(
    "filters,states",
    [
        ({"states": {"needs_input"}}, {"needs_input"}),
        ({"states": {"terminated", "failed"}}, {"terminated", "failed"}),
        ({"states": {"paused"}, "compute_released": False}, {"paused"}),
        ({"states": {"paused"}, "compute_released": True}, set()),
        ({"exclude_states": {"running", "starting", "paused", "terminated"}}, {"ready", "needs_input", "failed", "stopping"}),
    ],
)
def test_inventory_filters_match_existing_early_checks(inventory, filters, states):
    store, _service, workers = inventory
    assert set(store.list_worker_ids(**filters)) == {workers[state] for state in states}


@pytest.mark.parametrize("released", [None, "", "2026-01-01T01:00:00+00:00"])
def test_inventory_release_filter_matches_truthiness(inventory, released):
    store, _service, workers = inventory
    with store._connect() as conn:
        conn.execute("UPDATE workers SET compute_released_at=? WHERE worker_id=?", (released, workers["paused"]))
    assert bool(store.list_worker_ids(states={"paused"}, compute_released=False)) is (not bool(released))
    assert bool(store.list_worker_ids(states={"paused"}, compute_released=True)) is bool(released)


@pytest.mark.parametrize(
    "consumer,expected",
    [
        ("reconcile_all_workers", {"ready", "needs_input", "paused", "terminated", "failed", "running", "starting", "stopping"}),
        ("reap_needs_input_workers_once", {"needs_input"}),
        ("reap_terminated_workers_once", {"terminated", "failed"}),
        ("reap_paused_workers_once", {"paused"}),
        ("_release_idle_workers", {"ready", "needs_input", "failed", "stopping"}),
    ],
)
def test_recovery_consumers_hydrate_only_candidate_rows(inventory, monkeypatch, consumer, expected):
    store, service, workers = inventory
    hydrated = []
    get_worker = store.get_worker

    def read_current(worker_id, *args, **kwargs):
        hydrated.append(worker_id)
        return get_worker(worker_id, *args, **kwargs)

    def forbid_bulk():
        raise AssertionError("Recovery fetched full historical worker bundles")

    monkeypatch.setattr(store, "get_worker", read_current)
    monkeypatch.setattr(store, "list_all_workers", forbid_bulk)
    monkeypatch.setattr(store, "reconcile_invalid_running_runs", lambda: None)
    monkeypatch.setattr(service, "reconcile_terminal_artifact_observations", lambda: None)
    monkeypatch.setattr(service, "_reconcile_worker_row", lambda worker: None)
    monkeypatch.setattr(service, "_reconcile_terminated_worker_compute", lambda worker: None)
    monkeypatch.setattr(service, "_paused_terminate_after_s", lambda: 1)
    monkeypatch.setattr(service, "_worker_idle_seconds", lambda worker: 100)
    monkeypatch.setattr(service, "_release_worker_compute", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_release_needs_input_compute", lambda *args, **kwargs: None)
    monkeypatch.setattr(store, "list_nonterminal_runs_for_worker", lambda worker_id: [{"run_id": "synthetic-run", "state": "needs_input"}] if worker_id == workers["needs_input"] else [])
    if consumer == "_release_idle_workers":
        service._release_idle_workers(1)
    else:
        getattr(service, consumer)()
    assert set(hydrated) == {workers[state] for state in expected}


def test_candidate_read_sees_new_state_and_keeps_existing_gate(inventory, monkeypatch):
    store, service, workers = inventory
    candidate = workers["paused"]
    list_ids = store.list_worker_ids

    def changed_after_selection(**kwargs):
        ids = list_ids(**kwargs)
        with store._connect() as conn:
            conn.execute("UPDATE workers SET state='running' WHERE worker_id=?", (candidate,))
        return ids

    monkeypatch.setattr(store, "list_worker_ids", changed_after_selection)
    monkeypatch.setattr(service, "_paused_terminate_after_s", lambda: 1)
    monkeypatch.setattr(service, "_release_worker_compute", lambda *args, **kwargs: pytest.fail("A changed candidate was released"))
    assert service.reap_paused_workers_once() == []


def test_recovery_skips_deleted_candidate_and_stops_before_next_hydration(inventory, monkeypatch):
    store, service, workers = inventory
    seen = []
    reads = []
    get_worker = store.get_worker
    monkeypatch.setattr(store, "list_worker_ids", lambda: ["deleted-synthetic-worker", workers["ready"], workers["paused"]])
    monkeypatch.setattr(store, "reconcile_invalid_running_runs", lambda: None)
    monkeypatch.setattr(service, "reconcile_terminal_artifact_observations", lambda: None)

    def read_current(worker_id):
        reads.append(worker_id)
        return get_worker(worker_id)

    def reconcile(worker):
        seen.append(worker["worker_id"])
        service._shutdown_event.set()

    monkeypatch.setattr(store, "get_worker", read_current)
    monkeypatch.setattr(service, "_reconcile_worker_row", reconcile)
    service.reconcile_all_workers()
    assert seen == [workers["ready"]]
    assert reads == ["deleted-synthetic-worker", workers["ready"]]
