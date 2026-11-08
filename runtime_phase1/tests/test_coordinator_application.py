import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workers_projects_runtime.coordinator import (
    ApplicationBinding, CoordinatorConfig, CoordinatorConflict, CoordinatorScopeError,
    CoordinatorService, Dispatch, Goal, Route, canonical, digest, guard_coordinator_run,
)
from workers_projects_runtime.coordinator_application import (
    dispatch_input, finish_notification, notification_fence, prepare_goal, prepare_turn, turn_status,
)
from workers_projects_runtime.coordinator_api import install_coordinator_routes
from workers_projects_runtime.conversation_provider import ChatCompletionRequest
from workers_projects_runtime.schedule_targets import Destinations, dispatch_pending
from workers_projects_runtime.store import Store


class Provider:
    def __init__(self, store):
        self.store, self.received = store, []

    def _model(self, model, **_):
        return SimpleNamespace(effort_choices=("high",), harness_profile="codex-cli", native_model="synthetic-manager")

    def start(self, payload, **_):
        self.received.append(payload)
        tenant=_.get('tenant_id','local')
        m = payload.metadata
        project = m.allowed_ai_origin_scope["project_id"]
        worker = self.store.create_worker(project, m.owner_id, "Background manager", "coordinator", "codex-cli",
                                           "synthetic", "stub", "synthetic-manager",tenant_id=tenant)
        session = self.store.upsert_provider_session(tenant_id=tenant, owner_id=m.owner_id,
            conversation_id=m.conversation_id, agent_id=m.agent_id, model_id=payload.model,
            project_id=project, worker_id=worker["worker_id"], workspace_dir="/synthetic", access_mode="workspace")
        record, _ = self.store.create_provider_request(tenant_id=tenant, owner_id=m.owner_id,
            session_id=session["session_id"], idempotency_key=m.idempotency_key,
            message_id=m.message_id, stream_id=m.stream_id, requested_history_count=len(payload.messages))
        return record


@pytest.fixture
def app_core(tmp_path):
    store = Store(tmp_path / "application.sqlite3")
    service = SimpleNamespace(store=store, webhook_destinations=Destinations(tmp_path / "destinations"),
                              _scheduler_wake_event=Event(), _shutdown_event=Event(),
                              _schedule_transport_lock=Lock(), _schedule_transport_active=set(),
                              _schedule_transport_capacity=4)
    service.schedule_transport_executor = ThreadPoolExecutor(max_workers=2)
    destination = service.webhook_destinations.create("local", "owner", name="Synthetic app",
        url="http://127.0.0.1:12345/application", payload_mode="envelope")
    core = CoordinatorService(store, service, Provider(store))
    service.coordinator = core
    config = CoordinatorConfig(model="codex-cli:synthetic-manager", effort="high",
        routes=[Route(id="primary", profile="codex-cli", model="codex-cli:synthetic-worker", effort="high", execution_mode="host")],
        application=ApplicationBinding(destination_id=destination["destination_id"], request_ref="opaque-app-binding"))
    cid = core.create("local", "owner", config)["conversation_id"]
    service.started = []
    def reserve(**values):
        assert values.pop("start_run") is False
        values.pop("file_upload_ids", None)
        authority = values["bootstrap_bundle"]["launch_authority"]
        return store.reserve_delegation(**values, backend="synthetic", runtime="stub", model=authority["worker_model"])
    service.reserve_delegation = reserve
    def start(worker_id):
        goal = core._goal(cid, "goal")
        assert goal["worker_id"] == worker_id
        with store._connect() as conn:
            guard_coordinator_run(conn, goal["run_id"])
        service.started.append(worker_id)
    service.start_assigned_run = start
    service.cancel_run = lambda worker_id, run_id: store.update_run(run_id, state="cancelled")
    yield core, cid, config
    service.schedule_transport_executor.shutdown(wait=True)
    store.close()


def accept(core, cid, text="Make a tiny local page. Keep the source exactly."):
    return core.accept_turn("local", "owner", cid, "source", text)


def packet(cid, *, mood="calm", token="synthetic-turn-only-token"):
    return ChatCompletionRequest.model_validate({"model":"codex-cli:synthetic-manager", "reasoning_effort":"high",
        "messages":[{"role":"developer","content":"Current application mood: " + mood}],
        "metadata":{"owner_id":"owner","conversation_id":cid,"agent_id":"coordinator", "message_id":"source",
            "stream_id":"source","idempotency_key":cid+":source","surface":"coordinator", "origin":"system", "actor_kind":"system",
            "bootstrap_bundle":{"env":{"APP_BROKER_TOKEN":token}}}})


def delegation_values():
    return dict(tenant_id="local", owner_id="owner", idempotency_key="opaque-client-key", request_digest="opaque-native-digest",
        origin_ref="opaque-client-origin", title="Original work", goal="Make a tiny local page.",
        instruction="Make a tiny local page.", origin_surface="voice", worker_name="Helper", worker_role="general",
        profile="codex-cli", execution_mode="host", resource_class="standard",
        bootstrap_bundle={"launch_authority":{"version":1,"kind":"conversation_orchestrator", "execution_mode":"host",
            "worker_model":"synthetic-worker","worker_reasoning_effort":"high"}})


def prepare_dispatch(core, cid, *, guidance=None):
    accept(core, cid)
    core.accept_goals("local", "owner", cid, "source", [Goal(id="goal", text="Make a tiny local page.")])
    core.dispatch("local", "owner", cid, Dispatch(goal_id="goal", route_id="primary", guidance=guidance))
    return dispatch_input(core, "local", "owner", cid, "goal")


def test_application_create_is_idempotent_and_no_extra_project(app_core):
    core, cid, config = app_core
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(pool.map(lambda _: core.create("local", "owner", config), range(3)))
    assert {r["conversation_id"] for r in replies} == {cid}
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM coordinator_conversations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    with pytest.raises(CoordinatorConflict):
        core.create("local", "owner", config.model_copy(update={"effort":"high", "max_goals":101}))


def test_config_rejects_reusable_bootstrap_authority(app_core):
    _, _, config = app_core
    value = config.model_dump()
    value["bootstrap_bundle"] = {"env":{"TOKEN":"must-not-be-saved"}}
    with pytest.raises(ValueError):
        CoordinatorConfig.model_validate(value)


def test_acceptance_precedes_preparation_and_uses_same_outbox(app_core):
    core, cid, _ = app_core
    accept(core, cid)
    reply = core.start_turn("local", "owner", cid, "source")
    assert reply["state"] == "queued" and core.provider.received == []
    core.start_turn("local", "owner", cid, "source")
    with core.store._connect() as conn:
        rows = conn.execute("SELECT * FROM callback_outbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["worker_id"] is rows[0]["run_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM recurring_schedule_definitions").fetchone()[0] == 0
        body = json.loads(rows[0]["payload_json"])
        assert set(body["data"]) == {"conversation_id","turn_id","request_ref"}
        assert "Make a tiny" not in rows[0]["payload_json"]
    assert core.service._scheduler_wake_event.is_set()


def test_prepared_turn_preserves_source_and_never_saves_bearer(app_core):
    core, cid, _ = app_core
    accept(core, cid)
    result = prepare_turn(core, "local", "owner", cid, "source", packet(cid))
    assert result["request_id"] and len(core.provider.received) == 1
    native = core.provider.received[0]
    assert native.messages[0].content == "Current application mood: calm"
    assert "background coordination" in native.messages[-2].content
    assert native.messages[-1].content == "Make a tiny local page. Keep the source exactly."
    assert native.metadata.bootstrap_bundle["env"]["APP_BROKER_TOKEN"] == "synthetic-turn-only-token"
    with core.store._connect() as conn:
        assert "synthetic-turn-only-token" not in conn.execute("SELECT config_json FROM coordinator_conversations").fetchone()[0]
        assert "synthetic-turn-only-token" not in conn.execute("SELECT payload_json FROM coordinator_turns").fetchone()[0]
    replay = prepare_turn(CoordinatorService(core.store, core.service, core.provider), "local", "owner", cid, "source", packet(cid, mood="changed"))
    assert replay["request_id"] == result["request_id"] and len(core.provider.received) == 1
    status = turn_status(core, "local", "owner", cid, "source")
    assert status["message"] == "Make a tiny local page. Keep the source exactly."
    assert "payload_json" not in status


def test_crash_after_provider_admission_recovers_without_second_inference(app_core):
    core, cid, _ = app_core
    accept(core, cid)
    original = core.provider.start
    def crash(payload, **kwargs):
        original(payload, **kwargs)
        raise RuntimeError("synthetic crash after native admission")
    core.provider.start = crash
    with pytest.raises(RuntimeError):
        prepare_turn(core, "local", "owner", cid, "source", packet(cid))
    core.provider.start = original
    result = prepare_turn(core, "local", "owner", cid, "source", packet(cid))
    assert result["request_id"] and len(core.provider.received) == 1


def test_app_goals_select_originals_and_separate_guidance(app_core):
    core, cid, _ = app_core
    accept(core, cid)
    with pytest.raises(CoordinatorConflict):
        core.accept_goals("local", "owner", cid, "source", [Goal(id="goal", text="A rewritten page assignment")])
    details = prepare_dispatch(core, cid, guidance="Use accessible colors.")
    assert details["instruction"] == "Make a tiny local page."
    assert details["guidance"] == "Use accessible colors."
    values = delegation_values()
    values["bootstrap_bundle"]["inherited_conversation"] = {"version":1,"conversation_id":cid,"sources":[
        {"id":"source-original","role":"user","text":values["goal"],"assigned_input":True},
        {"id":"separate-guidance","role":"context","kind":"coordinator_guidance","text":details["guidance"]}]}
    result = prepare_goal(core, "local", "owner", cid, "goal", details["dispatch_digest"], values)
    run = core.store.get_run(result["initial_run_id"])
    assert run["instruction"] == "Make a tiny local page."
    bundle = json.loads(core.store.get_worker(result["worker_id"])["bootstrap_bundle_json"])
    assert bundle["inherited_conversation"]["sources"][-1]["role"] == "context"
    assert bundle["inherited_conversation"]["sources"][-1]["text"] == "Use accessible colors."


def test_native_reservation_binds_before_start_and_crash_retry_is_one_run(app_core):
    core, cid, _ = app_core
    details = prepare_dispatch(core, cid)
    reserve = core.service.reserve_delegation
    def crash(**values):
        work = reserve(**values)
        with core.store._connect() as conn, pytest.raises(CoordinatorConflict):
            guard_coordinator_run(conn, work["initial_run_id"])
        raise RuntimeError("synthetic crash before binding")
    core.service.reserve_delegation = crash
    with pytest.raises(RuntimeError):
        prepare_goal(core, "local", "owner", cid, "goal", details["dispatch_digest"], delegation_values())
    core.service.reserve_delegation = reserve
    work = prepare_goal(core, "local", "owner", cid, "goal", details["dispatch_digest"], delegation_values())
    replay = prepare_goal(core, "local", "owner", cid, "goal", details["dispatch_digest"], delegation_values())
    assert replay["initial_run_id"] == work["initial_run_id"]
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1


def test_pending_goal_stop_is_authoritative_and_fences_late_prepare(app_core):
    core, cid, _ = app_core
    details = prepare_dispatch(core, cid)
    assert core.goal_snapshot("local", "owner", cid, "goal")["actions"] == ["stop"]
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET intent_state='cancelled' WHERE conversation_id=?", (cid,))
    with pytest.raises(CoordinatorConflict):
        prepare_goal(core, "local", "owner", cid, "goal", details["dispatch_digest"], delegation_values())
    with core.store._connect() as conn:
        record = dict(conn.execute("SELECT * FROM callback_outbox WHERE event_type='coordinator.dispatch_due'").fetchone())
        assert conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0
    assert notification_fence(core.service, record) == "cancelled"


def test_outbox_uses_existing_transport_and_rejects_destination_revocation(app_core):
    core, cid, config = app_core
    accept(core, cid)
    core.start_turn("local", "owner", cid, "source")
    sent = []
    def send(**values):
        sent.append(values)
        return SimpleNamespace(state="accepted",error_code="",retryable=False,http_status=202,timings_ms={})
    core.service.schedule_webhook_transport = SimpleNamespace(send=send)
    ids = dispatch_pending(core.service)
    core.service.schedule_transport_executor.shutdown(wait=True)
    assert len(ids) == len(sent) == 1
    record = core.store.get_callback_outbox(ids[0])
    assert record["status"] == "http_accepted"
    assert sent[0]["event_id"] == ids[0]
    assert not core.service._schedule_transport_active


def test_uncertain_delivery_remains_uncertain_after_later_rejection(app_core):
    core, cid, _ = app_core
    accept(core, cid)
    core.start_turn("local", "owner", cid, "source")
    with core.store._connect() as conn:
        identity = conn.execute("SELECT callback_id FROM callback_outbox").fetchone()[0]
    record = core.store.claim_pending_callback(identity)
    assert finish_notification(core.store, record, state="unacknowledged", retryable=True)
    with core.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET next_attempt_at=NULL WHERE callback_id=?", (identity,))
    record = core.store.claim_pending_callback(identity)
    assert finish_notification(core.store, record, state="rejected", error="destination_revoked")
    assert core.store.get_callback_outbox(identity)["status"] == "unacknowledged"


def test_owner_api_requires_exact_signed_application_config(app_core, monkeypatch):
    core, cid, config = app_core
    monkeypatch.setenv("GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_SECRET", "synthetic-config-signing-key")
    app = FastAPI()
    install_coordinator_routes(app, core, lambda request:("local","owner"), lambda *args:None)
    client = TestClient(app)
    body = {"application_config":config.model_dump(mode="json")}
    assert client.post("/v1/coordinator/conversations",json=body).status_code == 403
    raw = base64.b64encode(json.dumps({"coordinator_application_config":body["application_config"]}).encode()).decode()
    stamp = str(int(datetime.now(UTC).timestamp()))
    signature = hmac.new(b"synthetic-config-signing-key", f"v1\n{stamp}\n{raw}".encode(), hashlib.sha256).hexdigest()
    headers = {"X-GlassHive-Bootstrap-Bundle-B64":raw,"X-GlassHive-Bootstrap-Timestamp":stamp,"X-GlassHive-Bootstrap-Signature":signature}
    response = client.post("/v1/coordinator/conversations",json=body,headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] == cid
    for selection in ({"account_id": "other-connection"}, {"model_id": "other-model"},
                      {"scope": {"project_id": "other-project"}}):
        assert client.post("/v1/coordinator/conversations", json={**body, **selection},
                           headers=headers).status_code == 403
    changed = {"application_config":{**body["application_config"],"max_goals":101}}
    assert client.post("/v1/coordinator/conversations",json=changed,headers=headers).status_code == 403


@pytest.mark.parametrize("change", ["model", "effort", "owner", "scope", "origin", "actor", "tenant"])
def test_preparation_rejects_changed_native_authority(app_core, change):
    core, cid, _ = app_core
    accept(core, cid)
    request = packet(cid)
    if change == "model": request.model = "codex-cli:other-manager"
    elif change == "effort": request.reasoning_effort = "low"
    elif change == "owner": request.metadata.owner_id = "unrelated-owner"
    elif change == "tenant": request.metadata.tenant_id = "unrelated-tenant"
    elif change == "scope": request.metadata.allowed_ai_origin_scope = {"project_id":"unrelated-project"}
    elif change == "origin": request.metadata.origin = "callback"
    elif change == "actor": request.metadata.actor_kind = "external_user"
    with pytest.raises(CoordinatorScopeError):
        prepare_turn(core, "local", "owner", cid, "source", request)
    assert core.provider.received == []


def test_concurrent_first_creation_uses_one_project(app_core):
    core, _, config = app_core
    fresh = config.model_copy(update={"application":config.application.model_copy(update={"request_ref":"another-opaque-app-binding"})})
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(pool.map(lambda _: core.create("local", "owner", fresh), range(3)))
    assert len({reply["conversation_id"] for reply in replies}) == 1
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM coordinator_conversations").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2


def test_dispatch_identity_and_admission_ordinal_survive_replay(app_core):
    core, cid, _ = app_core
    admissions = []
    core.service.local_qa_coordinator_admission = lambda **values: admissions.append(values["ordinal"])
    details = prepare_dispatch(core, cid)
    with core.store._connect() as conn:
        saved = dict(conn.execute("SELECT * FROM coordinator_goals").fetchone())
    core.dispatch("local", "owner", cid, Dispatch(goal_id="goal", route_id="primary"))
    assert details["dispatch_digest"] == saved["application_dispatch_digest"]
    assert admissions == [1]
    assert core._goal(cid,"goal")["application_dispatch_ordinal"] == 1


def test_outbox_source_hold_and_expiry_and_immutable_snapshot(app_core):
    import sqlite3
    from workers_projects_runtime.schedule_targets import fence_callback
    core, cid, _ = app_core
    accept(core, cid)
    core.start_turn("local", "owner", cid, "source")
    with core.store._connect() as conn:
        identity = conn.execute("SELECT callback_id FROM callback_outbox").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE callback_outbox SET payload_json='{}' WHERE callback_id=?", (identity,))
    record = core.store.claim_pending_callback(identity)
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET restore_hold=1 WHERE conversation_id=?", (cid,))
    assert notification_fence(core.service, record) == "continuity_restore_hold"
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET restore_hold=0 WHERE conversation_id=?", (cid,))
    record["expires_at"] = (datetime.now(UTC)-timedelta(seconds=1)).isoformat()
    assert finish_notification(core.store, record, state="expired", attempt_started=False)
    assert core.store.get_callback_outbox(identity)["status"] == "expired"


def test_stopped_unbound_reservation_recovers_opaque_client_identity(app_core):
    from workers_projects_runtime.coordinator import Control
    core, cid, _ = app_core
    details = prepare_dispatch(core, cid)
    original = core.service.reserve_delegation
    def crash(**values):
        result = original(**values)
        raise RuntimeError("synthetic crash after native reservation")
    core.service.reserve_delegation = crash
    with pytest.raises(RuntimeError): prepare_goal(core,"local","owner",cid,"goal",details["dispatch_digest"],delegation_values())
    core.control("local","owner",cid,"goal",Control(action="stop",run_id="",idempotency_key="stop-original"))
    core.service.reserve_delegation = original
    core.recover_dispatches("local","owner",cid)
    goal = core._goal(cid,"goal")
    assert goal["run_id"] and core.store.get_run(goal["run_id"])["state"] == "cancelled"
    assert core.service.started == []


def test_self_result_waits_for_canonical_files_and_keeps_exact_publisher(app_core):
    from workers_projects_runtime.coordinator_application import queue_self_results
    core,cid,_=app_core
    accept(core,cid)
    prepared=prepare_turn(core,"local","owner",cid,"source",packet(cid))
    request=core.store.get_provider_request(prepared["request_id"])
    session=core.store.get_provider_session_by_id(request["session_id"])
    run=core.store.create_run(session["worker_id"],session["project_id"],"Self-handled original",state="running")
    core.store.update_provider_request(request["request_id"],run_id=run["run_id"],state="running")
    core.accept_goals("local","owner",cid,"source",[Goal(id="goal",text="Make a tiny local page.")])
    # This fixture exercises canonical result projection, not native runtime invocation.
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='goal'",
                     (session["worker_id"],run["run_id"],cid))
    assert core.goal_snapshot("local","owner",cid,"goal")["actions"] == ["stop"]
    core.store.update_run(run["run_id"],state="completed",output_text="A complete result.",terminal_result_revision=2)
    queue_self_results(core,core._conversation("local","owner",cid))
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM callback_outbox WHERE event_type='coordinator.result_ready'").fetchone()[0]==0
    publisher={"owner_id":"owner","conversation_id":cid,"agent_id":"coordinator", "request_id":request["request_id"],
        "message_id":"source","stream_id":"source","run_id":run["run_id"],"attempt_id":""}
    envelope={"version":1,**publisher,"files":[{"name":"result.txt","sha256":"a"*64}]}
    core.store.update_provider_request(request["request_id"],state="completed",response_json=json.dumps({"glasshive":{"output_files":envelope}}))
    for _ in range(2):queue_self_results(core,core._conversation("local","owner",cid))
    page=core.read_result("local","owner",cid,"goal")
    assert page["output_files"]==envelope and page["publisher"]==publisher
    with core.store._connect() as conn:
        callbacks=conn.execute("SELECT * FROM callback_outbox WHERE event_type='coordinator.result_ready'").fetchall()
    assert len(callbacks)==1 and notification_fence(core.service,dict(callbacks[0]))==""
    changed={**envelope,"message_id":"unrelated-source"}
    core.store.update_provider_request(request["request_id"],response_json=json.dumps({"glasshive":{"output_files":changed}}))
    with pytest.raises(CoordinatorScopeError):core.read_result("local","owner",cid,"goal")


def test_generated_result_wake_keeps_authoritative_original_context_reference_after_restart(app_core):
    core,cid,_=app_core
    accept(core,cid)
    core.accept_goals("local","owner",cid,"source",[Goal(id="result-source-goal",text="Make a tiny local page.")])
    with core.store._connect() as conn:
        conn.execute("INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,created_at) VALUES(?,?,?,?,?)",
            (cid,"results-synthetic",json.dumps({"kind":"worker_result_notifications","items":[{"goal_id":"result-source-goal"}]}),"worker_results",datetime.now(UTC).isoformat()))
    reopened=CoordinatorService(core.store,core.service,core.provider)
    status=turn_status(reopened,"local","owner",cid,"results-synthetic")
    assert status["origin"]=="worker_results" and status["source_turn_id"]=="source"
    accept(reopened,cid,"Make a tiny local page. Keep the source exactly.")
    assert turn_status(reopened,"local","owner",cid,"results-synthetic")["source_turn_id"]=="source"
    assert "message" not in status


@pytest.mark.parametrize('withdrawal',['destination','source'])
def test_inflight_withdrawal_stops_retries_without_losing_uncertain_outcome(app_core,withdrawal):
    from workers_projects_runtime.schedule_targets import deliver
    core,cid,config=app_core
    accept(core,cid)
    core.start_turn('local','owner',cid,'source')
    with core.store._connect() as conn:
        identity=conn.execute('SELECT callback_id FROM callback_outbox').fetchone()[0]
    record=core.store.claim_pending_callback(identity)
    class Transport:
        def send(self,**values):
            if withdrawal=='destination':core.service.webhook_destinations.update('local','owner',config.application.destination_id,enabled=False)
            else:
                with core.store._connect() as conn:conn.execute("UPDATE coordinator_turns SET blocker='cancelled' WHERE conversation_id=?",(cid,))
            return SimpleNamespace(state='unacknowledged',error_code='delivery_timeout',retryable=True,http_status=None,timings_ms={})
    core.service.schedule_webhook_transport=Transport()
    deliver(core.service,record)
    saved=core.store.get_callback_outbox(identity)
    assert saved['status']=='unacknowledged' and saved['next_attempt_at'] is None


def test_application_acceptance_and_roster_do_not_copy_full_old_history(app_core,monkeypatch):
    core,cid,_=app_core
    def forbidden(*args,**kwargs):raise AssertionError('Foreground acceptance must not build the conversation history')
    original=core.snapshot
    monkeypatch.setattr(core,'snapshot',forbidden)
    result=accept(core,cid)
    assert result['turn_id']=='source' and result['state']=='queued'
    monkeypatch.setattr(core,'snapshot',original)
    compact=core.snapshot('local','owner',cid,include_history=False)
    assert compact['turns'][0]['source_turn_id']=='source' and compact['turns'][0]['actions']==['stop']
    assert compact['turns'][0]['message'] == 'Make a tiny local page. Keep the source exactly.'
    assert 'response_json' not in compact['turns'][0] and 'payload_json' not in compact['turns'][0]
    assert core.snapshot('local','owner',cid)['turns'][0]['message']=='Make a tiny local page. Keep the source exactly.'


def test_app_has_no_lifetime_goal_budget_when_unlimited_is_explicit(app_core):
    core,_,config=app_core
    config=config.model_copy(update={'max_goals':None,'application':config.application.model_copy(update={'request_ref':'unlimited-goals'})})
    cid=core.create('local','owner',config)['conversation_id']
    for index in range(105):
        message=f'Original objective {index}.'
        core.accept_turn('local','owner',cid,f'turn-{index}',message,[Goal(id=f'goal-{index}',text=message)])
    core.accept_turn('local','owner',cid,'new-source','The next original objective.',[Goal(id='next',text='The next original objective.')])
    assert len(core.snapshot('local','owner',cid,include_history=False)['goals'])==106


def test_result_turn_sources_follow_goal_fks_not_later_interactive_input(app_core):
    core,cid,_=app_core
    for tid,gid,text in [('one','a','Original task alpha.'),('two','b','Original task beta.')]:
        core.accept_turn('local','owner',cid,tid,text,[Goal(id=gid,text=text)])
        with core.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET response_json='{}' WHERE conversation_id=? AND turn_id=?",(cid,tid))
    with core.store._connect() as conn:
        for gid in ['a','b']:
            conn.execute("INSERT INTO coordinator_events(conversation_id,kind,identity,payload_json,created_at) VALUES(?,?,?,?,?)",
                (cid,'result_available','synthetic-run-'+gid,json.dumps({'goal_id':gid,'run_id':'synthetic-run-'+gid}),datetime.now(UTC).isoformat()))
    first=core.queue_result_turn('local','owner',cid)
    assert turn_status(core,'local','owner',cid,first)['source_turn_id']=='one'
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM coordinator_events WHERE delivered_turn_id=''").fetchone()[0]==1
        conn.execute("UPDATE coordinator_turns SET response_json='{}' WHERE conversation_id=? AND turn_id=?",(cid,first))
    second=core.queue_result_turn('local','owner',cid)
    reopened=CoordinatorService(core.store,core.service,core.provider)
    assert turn_status(reopened,'local','owner',cid,first)['source_turn_id']=='one'
    assert turn_status(reopened,'local','owner',cid,second)['source_turn_id']=='two'
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET message=? WHERE conversation_id=? AND turn_id=?',
            (json.dumps({'items':[{'goal_id':'a'},{'goal_id':'b'}]}),cid,second))
    with pytest.raises(CoordinatorConflict):turn_status(reopened,'local','owner',cid,second)


def test_existing_maintenance_recovers_committed_turn_and_dispatch_before_notification(app_core,monkeypatch):
    from workers_projects_runtime import coordinator_application as application
    core,cid,_=app_core
    accept(core,cid)
    core.accept_goals('local','owner',cid,'source',[Goal(id='goal',text='Make a tiny local page.')])
    original=application.notify
    def crash(*args,**kwargs):raise RuntimeError('synthetic crash after durable intent')
    monkeypatch.setattr(application,'notify',crash)
    with pytest.raises(RuntimeError):core.start_turn('local','owner',cid,'source')
    with pytest.raises(RuntimeError):core.dispatch('local','owner',cid,Dispatch(goal_id='goal',route_id='primary'))
    monkeypatch.setattr(application,'notify',original)
    core.reconcile_once()
    with core.store._connect() as conn:
        rows=conn.execute('SELECT event_type FROM callback_outbox').fetchall()
    assert {row['event_type'] for row in rows}=={'coordinator.turn_due','coordinator.dispatch_due'}
    assert core.provider.received==[]


def test_app_roster_does_not_offer_unprepared_external_expert_attachment(app_core):
    core,cid,_=app_core
    accept(core,cid)
    core.accept_goals('local','owner',cid,'source',[Goal(id='goal',text='Make a tiny local page.')])
    status=core.snapshot('local','owner',cid)
    assert status['workers']==[] and status['worker_reuse_supported'] is False
    with pytest.raises(CoordinatorScopeError):
        core.dispatch('local','owner',cid,Dispatch(goal_id='goal',worker_id='external-expert'))


def test_prepared_nonlocal_tenant_is_not_relabelled_as_local(app_core):
    core,_,config=app_core
    destination=core.service.webhook_destinations.create('tenant-synthetic','owner',name='Tenant adapter',url='http://127.0.0.1:12345/adapter',payload_mode='envelope')
    config=config.model_copy(update={'application':ApplicationBinding(destination_id=destination['destination_id'],request_ref='tenant-specific-binding')})
    cid=core.create('tenant-synthetic','owner',config)['conversation_id']
    core.accept_turn('tenant-synthetic','owner',cid,'source','Make a tiny local page.')
    incoming=packet(cid)
    with pytest.raises(CoordinatorScopeError):prepare_turn(core,'tenant-synthetic','owner',cid,'source',incoming)
    assert core.provider.received==[]
    incoming.metadata.tenant_id='tenant-synthetic'
    result=prepare_turn(core,'tenant-synthetic','owner',cid,'source',incoming)
    request=core.store.get_provider_request(result['request_id'])
    assert request['tenant_id']=='tenant-synthetic'
    assert core.provider.received[0].metadata.tenant_id=='tenant-synthetic'
