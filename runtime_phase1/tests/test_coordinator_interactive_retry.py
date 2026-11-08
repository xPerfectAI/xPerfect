"""Explicit retry preserves one accepted source and the failed native attempt."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_account_api import account_client

from test_coordinator_application import app_core, accept, packet
from workers_projects_runtime.coordinator import CoordinatorConflict, CoordinatorScopeError, CoordinatorService, Goal
from workers_projects_runtime.coordinator import ApplicationBinding, CoordinatorConfig, canonical
from workers_projects_runtime.coordinator_application import prepare_turn, turn_status
from workers_projects_runtime.coordinator_api import install_coordinator_routes


def fail(core, cid, tid="source", state="failed"):
    request = packet(cid, mood="fresh-current-context")
    request.metadata.message_id = request.metadata.stream_id = tid
    request.metadata.idempotency_key = cid + ":" + tid
    result = prepare_turn(core, "local", "owner", cid, tid, request)
    core.store.update_provider_request(result["request_id"], state=state)
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET blocker='failed' WHERE conversation_id=? AND turn_id=?", (cid, tid))
    return result


def test_retry_is_one_durable_child_with_exact_original_and_fresh_context(app_core):
    core, cid, _ = app_core
    original = "Keep these original bytes.\nDo the selected task."
    accept(core, cid, original)
    failed = fail(core, cid)
    with core.store._connect() as conn:
        before = dict(conn.execute("SELECT * FROM coordinator_turns WHERE conversation_id=? AND turn_id='source'", (cid,)).fetchone())
    assert turn_status(core, "local", "owner", cid, "source")["actions"] == ["retry"]
    child = core.retry_interactive_turn("local", "owner", cid, "source")
    assert child["source_turn_id"] == "source" and child["retry_of_turn_id"] == "source"
    assert child["state"] == "queued" and child["actions"] == ["stop"]
    assert core.retry_interactive_turn("local", "owner", cid, "source")["turn_id"] == child["turn_id"]
    restarted = CoordinatorService(core.store, core.service, core.provider)
    assert restarted.retry_interactive_turn("local", "owner", cid, "source")["turn_id"] == child["turn_id"]
    request = packet(cid, mood="current-new-context", token="new-current-token")
    request.metadata.message_id = request.metadata.stream_id = child["turn_id"]
    request.metadata.idempotency_key = cid + ":" + child["turn_id"]
    prepared = prepare_turn(restarted, "local", "owner", cid, child["turn_id"], request)
    assert prepared["request_id"] != failed["request_id"]
    assert core.provider.received[-1].messages[-1].content == original
    assert core.provider.received[-1].messages[0].content == "Current application mood: current-new-context"
    core.store.update_provider_request(prepared["request_id"], state="completed")
    assert restarted.retry_interactive_turn("local", "owner", cid, "source")["request_id"] == prepared["request_id"]
    with core.store._connect() as conn:
        assert dict(conn.execute("SELECT * FROM coordinator_turns WHERE conversation_id=? AND turn_id='source'", (cid,)).fetchone()) == before
        assert conn.execute("SELECT COUNT(*) FROM coordinator_turns WHERE conversation_id=?", (cid,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM callback_outbox WHERE event_type='coordinator.turn_due'", ()).fetchone()[0] == 1


def test_failed_retry_chain_has_no_lifetime_ceiling_and_always_resolves_original(app_core):
    core, cid, _ = app_core
    accept(core, cid)
    tid = "source"
    for _ in range(4):
        fail(core, cid, tid)
        child = core.retry_interactive_turn("local", "owner", cid, tid)
        assert child["source_turn_id"] == "source"
        tid = child["turn_id"]
    core.accept_goals("local", "owner", cid, tid, [Goal(id="one", text="Make a tiny local page.", source_turn_id=tid)])
    assert core._goal(cid, "one")["source_turn_id"] == "source"
    fail(core, cid, tid)
    with pytest.raises(CoordinatorConflict):
        core.retry_interactive_turn("local", "owner", cid, tid)


@pytest.mark.parametrize("fence", ["foreign", "stop", "turn_hold", "conversation_hold", "partial_goal", "response", "active", "missing", "destination"])
def test_retry_preserves_owner_stop_restore_settlement_and_partial_work_fences(app_core, fence):
    core, cid, _ = app_core
    accept(core, cid)
    old = fail(core, cid)
    with core.store._connect() as conn:
        if fence == "stop":conn.execute("UPDATE coordinator_turns SET blocker='cancelled' WHERE conversation_id=?", (cid,))
        if fence == "turn_hold":conn.execute("UPDATE coordinator_turns SET restore_hold=1 WHERE conversation_id=?", (cid,))
        if fence == "conversation_hold":conn.execute("UPDATE coordinator_conversations SET restore_hold=1 WHERE conversation_id=?", (cid,))
        if fence == "response":conn.execute("UPDATE coordinator_turns SET response_json='{}' WHERE conversation_id=?", (cid,))
    if fence == "active":core.store.update_provider_request(old["request_id"], state="running")
    if fence == "partial_goal":core.accept_goals("local", "owner", cid, "source", [Goal(id="accepted", text="Make a tiny local page.")])
    if fence == "destination":core.service.webhook_destinations.update("local","owner",app_core[2].application.destination_id,enabled=False)
    with pytest.raises((CoordinatorConflict, CoordinatorScopeError)):
        core.retry_interactive_turn("local", "foreign" if fence == "foreign" else "owner", cid,
                                    "missing" if fence == "missing" else "source")
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM coordinator_turns WHERE conversation_id=?", (cid,)).fetchone()[0] == 1


def test_retry_router_uses_same_owner_and_duplicate_receipt_without_provider_replay(app_core):
    core, cid, _ = app_core
    accept(core, cid);fail(core, cid, state="cancelled")
    app = FastAPI()
    install_coordinator_routes(app, core, lambda request:("local", request.headers.get("owner", "owner")), lambda *args:None)
    client = TestClient(app);url=f"/v1/coordinator/conversations/{cid}/turns/source/retry"
    assert client.post(url, headers={"owner":"foreign"}).status_code == 403
    first=client.post(url);assert first.status_code == 200, first.text
    assert client.post(url).json()["turn_id"] == first.json()["turn_id"]
    parent=client.get(f"/v1/coordinator/conversations/{cid}/turns/source").json()
    assert parent["retry_turn_id"] == first.json()["turn_id"] and parent["actions"] == []
    assert len(core.provider.received) == 1


def test_standalone_retry_uses_existing_provider_owner_and_copies_retained_library_selection(app_core):
    core,cid,config=app_core
    accept(core,cid);fail(core,cid)
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_conversations SET config_json=? WHERE conversation_id=?",
                     (canonical(config.model_copy(update={"application":None}).model_dump(mode="json")),cid))
        conn.execute("UPDATE coordinator_turns SET library_json=? WHERE conversation_id=? AND turn_id='source'",
                     ('{"refs":[],"version":1}',cid))
    child=core.retry_interactive_turn("local","owner",cid,"source")
    assert child["request_id"] and child["source_turn_id"]=="source"
    assert core.retry_interactive_turn("local","owner",cid,"source")["request_id"]==child["request_id"]
    assert len(core.provider.received)==2
    with core.store._connect() as conn:
        assert conn.execute("SELECT library_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",(cid,child["turn_id"])).fetchone()[0]=='{"refs":[],"version":1}'


def test_child_preparation_rejects_changed_authority_and_accepts_only_current_source_frame(app_core):
    core,cid,_=app_core;accept(core,cid);fail(core,cid)
    child=core.retry_interactive_turn("local","owner",cid,"source")
    p=packet(cid);p.metadata.message_id=p.metadata.stream_id=child["turn_id"];p.metadata.idempotency_key=cid+":"+child["turn_id"]
    p.metadata.owner_id="foreign"
    with pytest.raises(CoordinatorScopeError):prepare_turn(core,"local","owner",cid,child["turn_id"],p)
    assert len(core.provider.received)==1
    p.metadata.owner_id="owner"
    prepare_turn(core,"local","owner",cid,child["turn_id"],p)
    assert len(core.provider.received)==2


def test_real_account_retry_route_rejects_foreign_owner_without_creating_an_attempt(account_client):
    from types import SimpleNamespace
    from test_account_api import account_headers
    core=account_client.app.state.coordinator;store=core.store
    core.provider._model=lambda *args,**kwargs:SimpleNamespace(effort_choices=("high",))
    destination=core.service.webhook_destinations.create("tenant-a","owner-a",name="Synthetic retry",url="http://127.0.0.1:12345/retry",payload_mode="envelope")
    cid=core.create("tenant-a","owner-a",CoordinatorConfig(model="synthetic-manager",effort="high",application=ApplicationBinding(destination_id=destination["destination_id"],request_ref="synthetic-retry")))["conversation_id"]
    core.accept_turn("tenant-a","owner-a",cid,"source","Keep the accepted original.")
    scope=core.snapshot("tenant-a","owner-a",cid)["scope"]
    worker=store.create_worker(scope["project_id"],"owner-a","Manager","coordinator","codex-cli","synthetic","stub","synthetic-manager",tenant_id="tenant-a")
    session=store.upsert_provider_session(tenant_id="tenant-a",owner_id="owner-a",conversation_id=cid,agent_id="coordinator",model_id="synthetic-manager",project_id=scope["project_id"],worker_id=worker["worker_id"],workspace_dir="/synthetic",access_mode="workspace")
    request,_=store.create_provider_request(tenant_id="tenant-a",owner_id="owner-a",session_id=session["session_id"],idempotency_key=cid+":source",message_id="source",stream_id="source",requested_history_count=1)
    store.update_provider_request(request["request_id"],state="failed")
    with store._connect() as conn:conn.execute("UPDATE coordinator_turns SET request_id=?,blocker='failed' WHERE conversation_id=? AND turn_id='source'",(request["request_id"],cid))
    url=f"/v1/coordinator/conversations/{cid}/turns/source/retry"
    denied=account_client.post(url,headers=account_headers(owner_id="owner-b"))
    assert denied.status_code==403,denied.text
    first=account_client.post(url,headers=account_headers());assert first.status_code==200,first.text
    replay=account_client.post(url,headers=account_headers());assert replay.json()["turn_id"]==first.json()["turn_id"]
