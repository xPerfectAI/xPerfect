from __future__ import annotations

import copy
import hashlib
import json

import pytest

from test_account_api import account_client, account_headers, delegation_payload_with_origin
from workers_projects_runtime.models import ActiveWorkActionRequest


def canonical(body):
    payload = ActiveWorkActionRequest.model_validate(body)
    result = {'action': payload.action, 'instruction': str(payload.instruction or '').strip(),
        'capabilityReauthorization': payload.capability_reauthorization.model_dump() if payload.capability_reauthorization else None}
    if payload.file_inputs: result['fileInputs'] = [item.model_dump() for item in payload.file_inputs]
    if payload.native_input: result['nativeInput'] = payload.native_input.model_dump()
    if payload.native_human_input: result['nativeHumanInput'] = payload.native_human_input.model_dump(by_alias=True)
    if payload.source_context: result['sourceContext'] = payload.source_context.model_dump()
    return hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def fixture(client, body=None, state='completed'):
    service, store = client.app.state.service, client.app.state.store
    service.executor.submit = lambda *args, **kwargs: None
    response = client.post('/v1/delegations', headers=account_headers(idempotency_key='receipt-fixture'), json=delegation_payload_with_origin())
    assert response.status_code == 202, response.text
    work_ref = response.json()['workRef']
    body = body or {'action': 'message', 'instruction': 'Preserve the existing report. Café.', 'idempotencyKey': 'receipt-operation-one'}
    reservation = store.reserve_active_work_action(tenant_id='tenant-a', owner_id='owner-a', work_ref=work_ref,
        idempotency_key=body['idempotencyKey'], action=body['action'], payload_digest=canonical(body), executor_id=service.executor_id)
    if state == 'completed':
        saved = {'workRef': work_ref, 'action': body['action'], 'status': 'queued', 'state': 'queued', 'idempotentReplay': False}
        store.finish_active_work_action(reservation['action_use_id'], response=saved, executor_id=service.executor_id)
    elif state == 'failed':
        saved = {'statusCode': 503, 'detail': {'code': 'fixture_unavailable', 'message': 'Synthetic unavailable source.'}}
        store.fail_active_work_action(reservation['action_use_id'], failure_response=saved, error='Synthetic failure', executor_id=service.executor_id)
    else: saved = None
    if state in {'completed', 'failed'}:
        saved = json.loads(store.get_active_work_action(reservation['action_use_id'])['response_json'])
    return service, store, work_ref, body, saved


def snapshot(store):
    with store._connect() as conn:
        return {table: [tuple(row) for row in conn.execute(query)] for table, query in {
            'actions': 'SELECT * FROM active_work_action_uses ORDER BY action_use_id',
            'runs': 'SELECT run_id,state,instruction,queued_at,started_at,ended_at FROM runs ORDER BY run_id',
            'delegations': 'SELECT work_ref,current_run_id,updated_at FROM delegations ORDER BY work_ref',
        }.items()}


def forbid_effects(service, store, monkeypatch):
    def unexpected(*args, **kwargs): raise AssertionError('Receipt read must not execute, reserve or recover')
    for owner, method in [(store, 'reserve_active_work_action'), (service, 'execute_active_work_action'), (service, 'reconcile_active_work_action')]:
        monkeypatch.setattr(owner, method, unexpected)


def test_exact_completed_receipt_is_read_only_after_current_run_moves(account_client, monkeypatch):
    service, store, work, body, saved = fixture(account_client)
    record = store.get_delegation(work, tenant_id='tenant-a', owner_id='owner-a')
    store.update_run(record['current_run_id'], state='needs_input')
    before = snapshot(store); forbid_effects(service, store, monkeypatch)
    for _ in range(2):
        result = account_client.post(f'/v1/work/{work}/actions/receipt', headers=account_headers(), json=body)
        assert result.status_code == 202, result.text
        assert result.json() == saved
        assert snapshot(store) == before


def test_failed_receipt_returns_original_status_without_retry(account_client, monkeypatch):
    service, store, work, body, saved = fixture(account_client, state='failed')
    before = snapshot(store); forbid_effects(service, store, monkeypatch)
    result = account_client.post(f'/v1/work/{work}/actions/receipt', headers=account_headers(), json=body)
    assert result.status_code == saved['statusCode'], result.text
    assert result.json() == {'detail': saved['detail']}
    assert result.headers.get('x-xperfect-action-receipt')=='failed-v1'
    assert snapshot(store) == before


@pytest.mark.parametrize('change', ['missing', 'action', 'instruction', 'pending', 'owner', 'tenant'])
def test_receipt_rejects_mismatch_and_unfinished_without_effects(account_client, monkeypatch, change):
    service, store, work, body, _ = fixture(account_client, state='pending' if change == 'pending' else 'completed')
    changed = dict(body);headers=account_headers()
    if change == 'missing': changed['idempotencyKey'] = 'receipt-other-operation'
    if change == 'action': changed['action'] = 'queue'
    if change == 'instruction': changed['instruction'] += ' Alter scope.'
    if change == 'owner': headers=account_headers(owner_id='owner-b')
    if change == 'tenant': headers=account_headers(tenant_id='tenant-b')
    before = snapshot(store);forbid_effects(service, store, monkeypatch)
    result = account_client.post(f'/v1/work/{work}/actions/receipt', headers=headers, json=changed)
    assert result.status_code == (404 if change in {'owner', 'tenant'} else 409), result.text
    assert result.headers.get('x-xperfect-action-receipt') is None
    if result.status_code == 409: assert result.json()['detail']['code'] == 'active_work_idempotency_conflict'
    assert snapshot(store) == before


@pytest.mark.parametrize('field', ['capabilityReauthorization', 'fileInputs', 'nativeInput', 'nativeHumanInput', 'sourceContext'])
def test_receipt_digest_covers_every_optional_authority_field(account_client, monkeypatch, field):
    body={'action':'message','instruction':'Keep original constraints.','idempotencyKey':'receipt-all-authority-fields'}
    if field == 'capabilityReauthorization':
        body['action']='resume';body['instruction']=''
        body[field]={'version':1,'authorizationRef':'gha_synthetic_receipt','maxExpiresAt':'2026-12-01T00:00:00.000Z','scopeFingerprint':'synthetic-fingerprint'}
    if field in {'sourceContext', 'fileInputs'}:
        body['sourceContext']={'version':1,'originRef':'ghi_synthetic_origin_0001','sourceEventId':'event-receipt','sourceRevision':1,'surface':'web','outputContract':{'mode':'inherit'}}
    if field == 'fileInputs':
        body[field]=[{'uploadId':'fil_syntheticreceipt','revision':'revision-one','sha256':'a'*64,'sizeBytes':3,'filename':'fixture.txt','sourceFileId':'file-receipt','inputKind':'original','mediaGroupIndex':0}]
    if field in {'nativeInput', 'nativeHumanInput'}:
        body['action']='resume';body['instruction']='';body['nativeInput']={'version':1,'requestId':'request-receipt','requestFingerprint':'a'*64,'action':'accept','content':{'optionId':'allow_once'}}
    if field == 'nativeHumanInput':
        body[field]={'version':1,'actorKind':'external_user','origin':'interactive','sourceEventId':'event-owner','sourceIndex':0,'sourceMessageId':'message-owner','authoringRevision':1,'logicalTurnId':'turn-owner','conversationId':'conversation-owner','receivedAt':1900000000000}
    service,store,work,_,saved=fixture(account_client,body)
    before=snapshot(store);forbid_effects(service,store,monkeypatch)
    result=account_client.post(f'/v1/work/{work}/actions/receipt',headers=account_headers(),json=body)
    assert result.status_code==202,result.text
    assert result.json()==saved
    changed=copy.deepcopy(body)
    if field == 'capabilityReauthorization':changed[field]['authorizationRef']='gha_different_receipt'
    elif field == 'fileInputs':changed[field][0]['revision']='revision-two'
    elif field == 'nativeInput':changed[field]['content']['optionId']='reject_once'
    elif field == 'nativeHumanInput':changed[field]['sourceMessageId']='message-different'
    else:changed[field]['sourceRevision']=2
    result=account_client.post(f'/v1/work/{work}/actions/receipt',headers=account_headers(),json=changed)
    assert result.status_code==409,result.text
    assert snapshot(store)==before


def test_receipt_reuses_real_action_digest_and_saved_response(account_client, monkeypatch):
    service, store = account_client.app.state.service, account_client.app.state.store
    service.executor.submit=lambda *args,**kwargs:None
    accepted=account_client.post('/v1/delegations',headers=account_headers(idempotency_key='real-receipt-delegation'),json=delegation_payload_with_origin()).json()
    body={'action':'message','instruction':'Add a synthetic appendix.','idempotencyKey':'real-receipt-operation'}
    first=account_client.post(f"/v1/work/{accepted['workRef']}/actions",headers=account_headers(),json=body)
    assert first.status_code==202,first.text
    with store._connect() as conn: row=dict(conn.execute('SELECT * FROM active_work_action_uses WHERE work_ref=? AND idempotency_key=?',(accepted['workRef'],body['idempotencyKey'])).fetchone())
    assert row['status']=='completed'
    before=snapshot(store);forbid_effects(service,store,monkeypatch)
    result=account_client.post(f"/v1/work/{accepted['workRef']}/actions/receipt",headers=account_headers(),json=body)
    assert result.status_code==202,result.text
    assert result.json()==json.loads(row['response_json'])
    assert snapshot(store)==before


@pytest.mark.parametrize("executor_current", [True, False])
def test_exact_bound_control_receipt_can_report_pending_without_any_effect(account_client, monkeypatch, executor_current):
    body={'action':'steer','instruction':'Keep the same synthetic planner.','idempotencyKey':'pending-bound-control'}
    service,store,work,body,_=fixture(account_client,body,state='pending')
    record=store.get_delegation(work,tenant_id='tenant-a',owner_id='owner-a')
    worker=store.get_worker(record['worker_id']);run=store.get_run(record['current_run_id'])
    with store._connect() as conn:
        action=dict(conn.execute('SELECT * FROM active_work_action_uses WHERE work_ref=?',(work,)).fetchone())
    claim=service._claim_exact_run_control(worker,run,kind='steer_run',
        replacement_run={**run,'run_id':'run_receipt_replacement','instruction':body['instruction']},
        action_use_id=action['action_use_id'])
    assert claim and service.active_work_action_claim_is_pending(action)
    if not executor_current: service._executor_id='different-synthetic-executor'
    before=snapshot(store);forbid_effects(service,store,monkeypatch)
    result=account_client.post(f'/v1/work/{work}/actions/receipt',headers=account_headers(),json=body)
    if not executor_current:
        assert result.status_code==409,result.text
        assert snapshot(store)==before
        return
    assert result.status_code==202,result.text
    assert result.json()=={'workRef':work,'action':'steer','status':'pending','state':'accepted',
                          'confirmationPending':True,'idempotentReplay':True}
    assert snapshot(store)==before
    changed={**body,'instruction':'A different goal.'}
    assert account_client.post(f'/v1/work/{work}/actions/receipt',headers=account_headers(),json=changed).status_code==409
    assert snapshot(store)==before
