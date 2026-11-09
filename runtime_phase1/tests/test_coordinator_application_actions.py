"""Application goal controls reuse ordinary Native action execution and receipts."""
from types import SimpleNamespace
import json
import base64
import hashlib
import hmac
import time

import pytest

from test_account_api import account_client, account_headers, delegation_payload_with_origin
from workers_projects_runtime.coordinator import ApplicationBinding, CoordinatorConfig, Goal


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv('GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_SECRET','synthetic-action-context-key')


def signed_action(body):
    bundle={'application_context_format':'opaque_v1','env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'synthetic-current-action-token'}}
    encoded=base64.b64encode(json.dumps(bundle).encode()).decode()
    stamp=str(int(time.time()))
    signature=hmac.new(b'synthetic-action-context-key',f'v1\n{stamp}\n{encoded}'.encode(),hashlib.sha256).hexdigest()
    return {**body,'application_bootstrap_bundle_b64':encoded},{**account_headers(),
        'X-GlassHive-Bootstrap-Timestamp':stamp,'X-GlassHive-Bootstrap-Signature':signature}


def bind_goal(client):
    service, store, core = client.app.state.service, client.app.state.store, client.app.state.coordinator
    service.executor.submit = lambda *args, **kwargs: None
    core.provider._model = lambda *args, **kwargs: SimpleNamespace(effort_choices=("high",))
    destination = service.webhook_destinations.create("tenant-a", "owner-a", name="Synthetic adapter",
        url="http://127.0.0.1:12345/adapter", payload_mode="envelope")
    cid = core.create("tenant-a", "owner-a", CoordinatorConfig(model="synthetic-manager",effort="high",
        application=ApplicationBinding(destination_id=destination["destination_id"],request_ref="synthetic-goal-actions")))["conversation_id"]
    core.accept_turn("tenant-a","owner-a",cid,"original","Prepare a synthetic report.",
                     [Goal(id="goal",text="Prepare a synthetic report.")])
    result = client.post("/v1/delegations",headers=account_headers(idempotency_key="synthetic-app-action-mission"),
                         json=delegation_payload_with_origin())
    assert result.status_code == 202,result.text
    work = store.get_delegation(result.json()["workRef"],tenant_id="tenant-a",owner_id="owner-a")
    with store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET work_ref=?,worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='goal'",
                     (work["work_ref"],work["worker_id"],work["current_run_id"],cid))
    return core, cid, work


def test_rich_goal_action_binds_before_start_and_recovers_exact_receipt(account_client,monkeypatch):
    core,cid,work = bind_goal(account_client)
    body={"action":"message","instruction":"Include a synthetic appendix.","idempotencyKey":"app-message-one",
          "sourceContext":{"version":1,"originRef":"ghi_synthetic_origin_0001","sourceEventId":"synthetic-followup-source", "sourceRevision":1,"surface":"web","outputContract":{"mode":"inherit"}},
          "inheritedConversation":{"version":1,"conversation_id":"original-app-chat","sources":[
              {"id":"original-request","role":"user","text":"Prepare a synthetic report.","assigned_input":True}]}}
    starts=[]
    def start(worker):
        goal=core._goal(cid,"goal")
        assert goal["run_id"] != work["current_run_id"]
        assert goal["worker_id"] == worker
        starts.append(goal["run_id"])
    monkeypatch.setattr(core.service,"start_assigned_run",start)
    url=f"/v1/coordinator/conversations/{cid}/goals/goal/actions"
    prepared,headers=signed_action(body)
    response=account_client.post(url,params={"run_id":work["current_run_id"]},headers=headers,json=prepared)
    assert response.status_code==202,response.text
    effect=response.json()["goal"]["run_id"]
    assert starts==[effect]
    with core.store._connect() as conn:
        row=dict(conn.execute("SELECT * FROM active_work_action_uses WHERE idempotency_key=?",(body["idempotencyKey"],)).fetchone())
        action=dict(conn.execute("SELECT * FROM coordinator_actions WHERE idempotency_key=?",(body["idempotencyKey"],)).fetchone())
    assert json.loads(row["source_context_json"])["inherited_conversation"] == body["inheritedConversation"]
    assert json.loads(action["application_json"])["source_run_id"]==work["current_run_id"]
    with core.store._connect() as conn:
        count=conn.execute("SELECT COUNT(*) FROM runs WHERE worker_id=?",(work["worker_id"],)).fetchone()[0]
    def forbid(*args,**kwargs):raise AssertionError("Receipt read must not execute another action")
    monkeypatch.setattr(core.service,"execute_active_work_action",forbid)
    monkeypatch.setattr(core.store,"reserve_active_work_action",forbid)
    recovered=account_client.post(url+"/receipt",params={"run_id":effect},headers=account_headers(),json=body)
    assert recovered.status_code==202,recovered.text
    assert recovered.json()["runId"]==effect
    assert starts==[effect]
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE worker_id=?",(work["worker_id"],)).fetchone()[0]==count
    changed={**body,"instruction":"Changed action."}
    assert account_client.post(url+"/receipt",params={"run_id":effect},headers=account_headers(),json=changed).status_code==409


def test_goal_action_stale_run_cannot_reserve_effect(account_client):
    core,cid,work=bind_goal(account_client)
    response=account_client.post(f"/v1/coordinator/conversations/{cid}/goals/goal/actions",params={"run_id":"unrelated-run"},
        headers=account_headers(),json={"action":"stop","idempotencyKey":"stale-stop"})
    assert response.status_code==409,response.text
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM active_work_action_uses").fetchone()[0]==0
    assert core._goal(cid,"goal")["intent_state"]=="accepted"


def test_pending_goal_stop_and_exact_receipt_need_no_fake_worker(account_client):
    core,cid,work=bind_goal(account_client)
    core.accept_goals("tenant-a","owner-a",cid,"original",[Goal(id="pending",text="Prepare a synthetic report.")])
    body={"action":"stop","idempotencyKey":"pending-goal-stop"}
    url=f"/v1/coordinator/conversations/{cid}/goals/pending/actions"
    response=account_client.post(url,params={"run_id":""},headers=account_headers(),json=body)
    assert response.status_code==202,response.text
    assert response.json()["goal"]["state"]=="cancelled"
    preflight=account_client.post(url+"/preflight",params={"run_id":""},headers=account_headers(),json=body)
    assert preflight.status_code==200 and preflight.json()["state"]=="completed"
    recovered=account_client.post(url+"/receipt",params={"run_id":""},headers=account_headers(),json=body)
    assert recovered.status_code==202,recovered.text
    assert recovered.json()["goal"]["worker_id"]==""
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM active_work_action_uses").fetchone()[0]==0


def test_ordinary_delegation_projects_owner_scoped_typed_uploads(account_client):
    body=delegation_payload_with_origin()
    body['bootstrapBundle']['glasshive_upload_context']={'tool_uploaded_files':[
        {'file_id':'synthetic-selected-file','filename':'selected.txt','text':'Exact synthetic attachment text.'}]}
    response=account_client.post('/v1/delegations',headers=account_headers(idempotency_key='typed-upload-native-owner'),json=body)
    assert response.status_code==202,response.text
    record=account_client.app.state.store.get_delegation(response.json()['workRef'],tenant_id='tenant-a',owner_id='owner-a')
    worker=account_client.app.state.store.get_worker(record['worker_id'])
    bundle=json.loads(worker['bootstrap_bundle_json'])
    assert any(entry.get('content')=='Exact synthetic attachment text.' for entry in bundle['files'])


def test_native_manager_control_uses_same_outbox_and_replays_missing_notification(account_client,monkeypatch):
    from workers_projects_runtime.coordinator import Control
    from workers_projects_runtime import coordinator_application as application
    core,cid,work=bind_goal(account_client)
    control=Control(action='message',run_id=work['current_run_id'],idempotency_key='manager-source-once',message='Preserve the original and add an appendix.')
    original=application.notify
    monkeypatch.setattr(application,'notify',lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('synthetic crash after control intent')))
    with pytest.raises(RuntimeError):core.control('tenant-a','owner-a',cid,'goal',control)
    monkeypatch.setattr(application,'notify',original)
    core.recover_retry_actions('tenant-a','owner-a',cid)
    details=application.action_input(core,'tenant-a','owner-a',cid,'goal',control.idempotency_key)
    assert details['control']==control.model_dump() and details['source_turn_id']=='original'
    assert core._goal(cid,'goal')['run_id']==work['current_run_id']
    with core.store._connect() as conn:
        events=conn.execute("SELECT * FROM callback_outbox WHERE event_type='coordinator.action_due'").fetchall()
        assert len(events)==1
        assert conn.execute('SELECT COUNT(*) FROM active_work_action_uses').fetchone()[0]==0
    body={'action':'message','instruction':control.message,'idempotencyKey':details['action_key']}
    preflight=account_client.post(f'/v1/coordinator/conversations/{cid}/goals/goal/actions/preflight',params={'run_id':work['current_run_id']},headers=account_headers(),json=body)
    assert preflight.status_code==200 and preflight.json()['state']=='unprepared'
    prepared,headers=signed_action(body)
    result=account_client.post(f'/v1/coordinator/conversations/{cid}/goals/goal/actions',params={'run_id':work['current_run_id']},headers=headers,json=prepared)
    assert result.status_code==202,result.text
    assert result.json()['goal']['run_id']!=work['current_run_id']


def test_restart_refreshes_same_queued_effect_without_repeating_action(account_client,monkeypatch):
    core,cid,work=bind_goal(account_client)
    body={'action':'message','instruction':'Continue the retained original.','idempotencyKey':'restart-exact-action'}
    prepared,headers=signed_action(body)
    url=f'/v1/coordinator/conversations/{cid}/goals/goal/actions'
    response=account_client.post(url,params={'run_id':work['current_run_id']},headers=headers,json=prepared)
    assert response.status_code==202,response.text
    run_id=response.json()['runId']
    assert core.service.has_run_local_bundle(run_id)
    run=core.store.get_run(run_id)
    projected=core.service._runtime_worker_for_run(core.store.get_worker(work['worker_id']),run)
    assert projected['_requires_application_invocation_context'] is True
    assert core.service._requires_conversation_invocation_bearer(projected)
    assert 'synthetic-current-action-token' not in run['runtime_bundle_json']
    core.service.discard_run_local_bundle(run_id)
    preflight=account_client.post(url+'/preflight',params={'run_id':run_id},headers=account_headers(),json=body)
    assert preflight.status_code==200 and preflight.json()['state']=='unprepared'
    core.recover_retry_actions('tenant-a','owner-a',cid)
    from workers_projects_runtime.coordinator_application import action_input
    details=action_input(core,'tenant-a','owner-a',cid,'goal',body['idempotencyKey'])
    assert details['action_request']['instruction']==body['instruction']
    assert details['state']=='preparing'
    def forbid(*args,**kwargs):raise AssertionError('Recovered action must use its immutable native receipt')
    monkeypatch.setattr(core.service,'execute_active_work_action',forbid)
    prepared,headers=signed_action(body)
    recovered=account_client.post(url,params={'run_id':run_id},headers=headers,json=prepared)
    assert recovered.status_code==202,recovered.text
    assert recovered.json()['runId']==run_id and core.service.has_run_local_bundle(run_id)
    with core.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM active_work_action_uses WHERE idempotency_key=?',(body['idempotencyKey'],)).fetchone()[0]==1


def test_invocation_uses_fresh_context_before_admission_and_never_falls_back_after_restart(account_client,monkeypatch):
    from workers_projects_runtime.broker_admission import BrokerAdmissionError
    core,cid,work=bind_goal(account_client)
    body={'action':'message','instruction':'Keep working.','idempotencyKey':'runtime-fresh-context'}
    prepared,headers=signed_action(body)
    result=account_client.post(f'/v1/coordinator/conversations/{cid}/goals/goal/actions',params={'run_id':work['current_run_id']},headers=headers,json=prepared)
    assert result.status_code==202,result.text
    run_id=result.json()['runId'];run=core.store.get_run(run_id);worker=core.store.get_worker(work['worker_id'])
    captured=[]
    class AdmissionObserved(Exception):pass
    def observe(current,*args,**kwargs):
        captured.append(json.loads(current['bootstrap_bundle_json']))
        assert current['_requires_application_invocation_context'] is True
        raise AdmissionObserved()
    monkeypatch.setattr(core.service,'_run_local_admitted_worker',observe)
    with pytest.raises(AdmissionObserved):core.service._run_local_worker(worker,run)
    assert captured[0]['env']['GLASSHIVE_CAPABILITY_BROKER_TOKEN']=='synthetic-current-action-token'
    assert 'synthetic-current-action-token' not in core.store.get_worker(work['worker_id'])['bootstrap_bundle_json']
    core.service.discard_run_local_bundle(run_id)
    monkeypatch.setattr(core.service,'_run_local_admitted_worker',lambda current,*args,**kwargs:current)
    with pytest.raises(BrokerAdmissionError,match='refreshed'):
        core.service._run_local_worker(worker,run)


def test_application_context_and_invocation_executor_share_existing_run_marker(account_client):
    from workers_projects_runtime.coordinator_application import stage_action_context
    core,cid,work=bind_goal(account_client)
    body={'action':'message','instruction':'Keep the original context.','idempotencyKey':'coexisting-native-markers'}
    prepared,headers=signed_action(body)
    response=account_client.post(f'/v1/coordinator/conversations/{cid}/goals/goal/actions',
        params={'run_id':work['current_run_id']},headers=headers,json=prepared)
    assert response.status_code==202,response.text
    run_id=response.json()['runId']
    assert core.store.bind_invocation_executor(run_id,core.service._executor_id)
    bundle={'application_context_format':'opaque_v1','env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'synthetic-current-action-token'}}
    assert stage_action_context(core,'tenant-a','owner-a',cid,'goal',body['idempotencyKey'],bundle,{'runId':run_id})
    run=core.store.get_run(run_id)
    assert json.loads(run['runtime_bundle_json'])=={
        'application_invocation_context_required':True,'invocation_executor_id':core.service._executor_id}
    projected=core.service._runtime_worker_for_run(core.store.get_worker(work['worker_id']),run)
    assert projected['_requires_application_invocation_context'] is True
    assert 'invocation_executor_id' not in json.loads(projected['bootstrap_bundle_json'])
    core.store.update_run(work['current_run_id'],state='completed',output_text='Earlier synthetic effect finished.')
    assert core.store.peek_next_queued_run(work['worker_id'])['run_id']==run_id
    assert core.store.claim_next_queued_run(work['worker_id'],executor_id='synthetic-foreign-executor',expected_run_id=run_id) is None
    assert core.store.get_run(run_id)['state']=='queued'
    claimed=core.store.claim_next_queued_run(work['worker_id'],executor_id=core.service._executor_id,expected_run_id=run_id)
    assert claimed is not None and claimed['run_id']==run_id
