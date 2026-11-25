"""A whole-conversation Stop is durable: nothing in the conversation starts again, also after a
restart. Ported by meaning from an earlier internal patch (without Worker claims or closing)."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_coordinator_existing_worker import existing, dispatch  # noqa: F401 (fixture)
from workers_projects_runtime.coordinator import (
    CoordinatorConflict, CoordinatorService, Goal,
)
from workers_projects_runtime.coordinator_api import install_coordinator_routes


def released(*_args, **_kwargs):
    return {"capacity_released": True}


def schedule(core, worker_id):
    worker = core.store.get_worker(worker_id)
    return core.store.create_scheduled_run(worker_id=worker_id, project_id=worker["project_id"],
        owner_id="owner", instruction="Unrelated wording", run_at="2026-01-01T00:00:00+00:00")


def recurring(core, worker_id):
    worker = core.store.get_worker(worker_id)
    return core.store.create_recurring_schedule_definition(worker_id=worker_id, project_id=worker["project_id"],
        tenant_id="local", owner_id="owner", scheduler_owner="glasshive_native", instruction="Unrelated wording",
        schedule_text="", recurrence_type="interval", interval_seconds=60, local_time="", timezone_name="UTC",
        dst_policy="skip", next_run_at="2026-01-01T00:00:00+00:00")


@pytest.mark.parametrize("initial", ["queued", "paused", "needs_input", "completed", "cancelled", "failed"])
def test_stop_fences_every_continuation_and_keeps_receipts(existing, initial):
    core, cid, worker_id, command = existing
    result = dispatch(existing)
    core.store.update_run(result["run_id"], state=initial, output_text="Retained receipt")
    core.accept_turn("local", "owner", cid, "next", "Unrelated wording")
    cancelled = []
    core.provider.cancel_by_idempotency = lambda key, *_a, **_k: cancelled.append(key) or {"capacity_released": True}
    snapshot = core.stop_conversation("local", "owner", cid)
    assert snapshot["state"] == "stopped" and not snapshot["confirmation_pending"]
    assert snapshot["stop_scope"] == "stopped"
    if initial != "queued":  # a queued run has produced nothing yet
        assert core.store.get_run(result["run_id"])["output_text"] == "Retained receipt"
    if initial in {"queued", "paused", "needs_input"}:
        assert core.store.get_run(result["run_id"])["state"] == "cancelled"
    assert set(cancelled) == {cid + ":turn", cid + ":next"}
    with pytest.raises(CoordinatorConflict, match="stopped"):
        core.accept_turn("local", "owner", cid, "late", "Unrelated wording")
    with pytest.raises(CoordinatorConflict, match="stopped"):
        core.accept_goals("local", "owner", cid, "turn", [Goal(id="late", text="Unrelated wording")])
    with pytest.raises(CoordinatorConflict, match="stopped"):
        core.dispatch("local", "owner", cid, command)
    assert core.start_turn("local", "owner", cid, "next")["state"] == "cancelled"
    assert core.queue_result_turn("local", "owner", cid) is None
    with core.store._connect() as conn:
        assert not conn.execute("SELECT 1 FROM coordinator_events WHERE conversation_id=? AND delivered_turn_id=''",
                                (cid,)).fetchone()
    restarted = CoordinatorService(core.store, core.service, core.provider)
    assert restarted.stop_conversation("local", "owner", cid)["state"] == "stopped"


def test_a_late_result_after_stop_is_kept_and_wakes_nothing(existing):
    core, cid, worker_id, _ = existing
    result = dispatch(existing)
    with core.store._connect() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (result["run_id"],))
    core.provider.cancel_by_idempotency = released
    core.service.cancel_run = lambda *_args: None  # the run keeps going for a while
    assert core.stop_conversation("local", "owner", cid)["state"] == "stopping"
    core.store.update_run(result["run_id"], state="completed", output_text="Late output")
    assert core.record_run_result(worker_id, result["run_id"]) is False
    with core.store._connect() as conn:
        delivered = conn.execute("SELECT delivered_turn_id FROM coordinator_events WHERE conversation_id=? "
                                 "AND identity=?", (cid, result["run_id"])).fetchone()[0]
    assert delivered == "stopped"
    core.reconcile_once()  # the re-driven Stop confirms and starts nothing
    snapshot = core.snapshot("local", "owner", cid)
    assert snapshot["stop_scope"] == "stopped"
    assert not [turn for turn in snapshot["turns"] if turn.get("origin") == "worker_results"]
    assert core.store.get_run(result["run_id"])["output_text"] == "Late output"


def test_reconcile_re_drives_a_stop_after_a_restart(existing):
    core, cid, _, _ = existing
    result = dispatch(existing)
    with core.store._connect() as conn:  # the fence committed, then the process ended
        conn.execute("UPDATE coordinator_conversations SET stop_scope='goals' WHERE conversation_id=?", (cid,))
    restarted = CoordinatorService(core.store, core.service, core.provider)
    restarted.provider.cancel_by_idempotency = released
    restarted.reconcile_once()
    assert core.store.get_run(result["run_id"])["state"] == "cancelled"
    assert restarted.snapshot("local", "owner", cid)["stop_scope"] == "stopped"


def test_stop_keeps_other_work_on_a_shared_worker(existing):
    core, cid, worker_id, _ = existing
    result = dispatch(existing)
    other = core.store.create_run(worker_id, core.store.get_worker(worker_id)["project_id"], "Other work")
    pending = schedule(core, worker_id)
    core.provider.cancel_by_idempotency = released
    assert core.stop_conversation("local", "owner", cid)["state"] == "stopped"
    assert core.store.get_run(result["run_id"])["state"] == "cancelled"
    assert core.store.get_run(other["run_id"])["state"] == "queued"
    assert core.store.get_schedule(pending["schedule_id"])["state"] == "pending"
    assert core.store.get_worker(worker_id)["state"] != "terminated"


@pytest.mark.parametrize("state", ["queued", "running", "paused", "needs_input"])
def test_stop_cancels_accepted_follow_up_runs(existing, state):
    core, cid, worker_id, _ = existing
    result = dispatch(existing)
    core.store.update_run(result["run_id"], state="completed", output_text="Earlier output")
    followup = core.store.create_run(worker_id, core.store.get_worker(worker_id)["project_id"], "Follow up")
    with core.store._connect() as conn:
        conn.execute("UPDATE runs SET state=? WHERE run_id=?", (state, followup["run_id"]))
        conn.execute("INSERT INTO coordinator_actions (conversation_id,goal_id,idempotency_key,payload_json,response_json) "
                     "VALUES(?,?,?,?,?)", (cid, "followup", "accepted-followup",
                     json.dumps({"action": "queue", "run_id": result["run_id"]}), json.dumps({"run_id": followup["run_id"]})))
    unrelated = core.store.create_run(worker_id, core.store.get_worker(worker_id)["project_id"], "Other goal")
    core.provider.cancel_by_idempotency = released
    assert core.stop_conversation("local", "owner", cid)["state"] == "stopped"
    assert core.store.get_run(followup["run_id"])["state"] == "cancelled"
    assert core.store.get_run(unrelated["run_id"])["state"] == "queued"
    assert core.store.get_run(result["run_id"])["output_text"] == "Earlier output"


def test_stop_cancels_only_schedules_its_own_runs_created(existing):
    core, cid, worker_id, _ = existing
    result = dispatch(existing)
    owner_clock = recurring(core, worker_id)
    with core.store._connect() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (result["run_id"],))
    values = dict(tenant_id="local", owner_id="owner", target={"kind": "worker", "worker_id": worker_id},
                  instruction="Identical task", recurrence_type="interval", interval_seconds=60,
                  source_run_id=result["run_id"], first_run_at="2099-01-01T00:00:00Z")
    clock = core.service.create_target_schedule(idempotency_key="native-owned-clock", **values)
    occurrence = core.service.run_recurring_schedule_now(clock["definition_id"], tenant_id="local", owner_id="owner",
                                                         idempotency_token="native-owned-occurrence")
    pending = core.store.get_schedule(occurrence["schedule_id"])
    core.store.update_run(result["run_id"], state="completed")
    core.store.claim_schedule(pending["schedule_id"])
    descendant, _ = core.store.create_or_get_run_for_schedule(pending["schedule_id"])
    with core.store._connect() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (descendant["run_id"],))
    nested = core.service.create_target_schedule(idempotency_key="nested-native-clock",
                                                 **{**values, "source_run_id": descendant["run_id"]})
    core.provider.cancel_by_idempotency = released
    assert core.stop_conversation("local", "owner", cid)["state"] == "stopped"
    definition = lambda item: core.store.get_recurring_schedule_definition(  # noqa: E731
        item["definition_id"], tenant_id="local", owner_id="owner")
    assert core.store.get_schedule(pending["schedule_id"])["state"] == "cancelled"
    assert not definition(clock)["active"] and not definition(nested)["active"]
    assert core.store.get_run(descendant["run_id"])["state"] == "cancelled"
    assert definition(owner_clock)["active"]
    with pytest.raises(ValueError):  # a finished or stopped source run cannot create a schedule
        core.service.create_target_schedule(idempotency_key="late-native-clock", **values)


def test_http_stop_is_authenticated_and_idempotent(existing):
    core, cid, _, _ = existing
    dispatch(existing)
    core.provider.cancel_by_idempotency = released
    app = FastAPI()
    install_coordinator_routes(app, core, lambda request: ("local", request.headers.get("x-owner", "owner")),
                               lambda *_: None)
    client = TestClient(app)
    url = f"/v1/coordinator/conversations/{cid}/stop"
    assert client.post(url, headers={"x-owner": "foreign"}).status_code in {403, 404}
    assert core.snapshot("local", "owner", cid)["stop_scope"] == ""
    for _ in range(2):
        response = client.post(url)
        assert response.status_code == 200 and response.json()["state"] == "stopped"
