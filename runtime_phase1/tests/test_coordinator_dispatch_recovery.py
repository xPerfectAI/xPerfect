"""Explicit owner retry recovers rejected application dispatch without replaying new work."""
from datetime import UTC, datetime, timedelta
import copy
import json
from types import SimpleNamespace

import pytest

from test_coordinator_application import app_core, prepare_dispatch, delegation_values
from test_account_api import account_client, account_headers
from workers_projects_runtime.coordinator import ApplicationBinding, Control, CoordinatorConfig, CoordinatorConflict, CoordinatorScopeError, Dispatch, Goal, Route, canonical, digest
from workers_projects_runtime.coordinator_application import finish_notification, prepare_goal


def rejected_dispatch(core, cid, *, status=409):
    with core.store._connect() as conn:
        row = conn.execute("SELECT callback_id FROM callback_outbox WHERE event_type='coordinator.dispatch_due' AND json_extract(payload_json,'$.data.conversation_id')=?", (cid,)).fetchone()
    record = core.store.claim_pending_callback(row[0])
    assert record
    assert finish_notification(core.store, record, state='rejected', error='http_rejected', http_status=status)
    return core.store.get_callback_outbox(row[0])


def failed_preparation(core, cid, details, values):
    original = core.service.reserve_delegation
    def denied(**_):
        raise PermissionError('Synthetic current policy denies the launch')
    core.service.reserve_delegation = denied
    try:
        with pytest.raises(PermissionError):
            prepare_goal(core, 'local', 'owner', cid, 'goal', details['dispatch_digest'], values)
    finally:
        core.service.reserve_delegation = original


def test_explicit_redispatch_reuses_immutable_event_and_fresh_pre_reservation_context(app_core):
    core, cid, _ = app_core
    details = prepare_dispatch(core, cid)
    old = delegation_values()
    old['bootstrap_bundle']['env'] = {'BROKER_TOKEN': 'synthetic-old'}
    old['bootstrap_bundle']['developer_instructions'] = 'Current Feelings: old.'
    old['bootstrap_bundle']['launch_authority']['worker_profile'] = 'codex-cli'
    old['request_digest'] = digest(old['bootstrap_bundle'])
    failed_preparation(core, cid, details, old)
    before = rejected_dispatch(core, cid)
    assert core.goal_snapshot('local','owner',cid,'goal')['actions'] == ['retry','stop']
    assert core.recover_dispatches('local','owner',cid) == []
    assert core.store.get_callback_outbox(before['callback_id'])['status'] == 'rejected'
    fresh = copy.deepcopy(old)
    fresh['bootstrap_bundle']['env']['BROKER_TOKEN'] = 'synthetic-current'
    fresh['bootstrap_bundle']['developer_instructions'] = 'Current Feelings: current.'
    fresh['bootstrap_bundle']['launch_authority'].pop('worker_profile')
    fresh['bootstrap_bundle']['launch_authority']['worker_access'] = 'full'
    fresh['request_digest'] = digest(fresh['bootstrap_bundle'])
    with pytest.raises(CoordinatorConflict, match='unstarted prepared'):
        prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],fresh)
    core.dispatch('local','owner',cid,Dispatch(goal_id='goal',route_id='primary'))
    after = core.store.get_callback_outbox(before['callback_id'])
    assert after['status'] == 'pending'
    for key in ('callback_id','payload_json','created_at','expires_at','attempts','delivery_generation','http_status','last_error'):
        assert after[key] == before[key]
    admitted = []
    reserve = core.service.reserve_delegation
    def capture(**values):
        admitted.append(copy.deepcopy(values['bootstrap_bundle']))
        return reserve(**values)
    core.service.reserve_delegation = capture
    work = prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],fresh)
    assert core.service.started == [work['worker_id']]
    saved = json.loads(core._goal(cid,'goal')['preparation_json'])
    assert saved['preparation_generation'] == saved['explicit_redispatch_generation'] == 1
    assert saved['preparation_history'] == [{'request_digest':old['request_digest'],'generation':0}]
    assert saved['request_digest'] == fresh['request_digest']
    bundle = json.loads(core.store.get_worker(work['worker_id'])['bootstrap_bundle_json'])
    assert bundle['developer_instructions'] == 'Current Feelings: current.'
    assert admitted[0]['env']['BROKER_TOKEN'] == 'synthetic-current'
    assert 'BROKER_TOKEN' not in bundle['env']  # Existing storage projection retains no new credential carrier.
    assert 'worker_profile' not in bundle['launch_authority']
    # Any accepted reservation still belongs to its original exact request owner.
    newer = copy.deepcopy(fresh); newer['request_digest'] = 'another-digest'
    replay = prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],newer)
    assert replay['work_ref'] == work['work_ref']
    assert core.store.get_delegation(work['work_ref'],tenant_id='local',owner_id='owner')['request_digest'] == fresh['request_digest']


def test_http_attempts_do_not_authorize_context_refresh_and_permissions_still_apply(app_core):
    core, cid, _ = app_core
    details = prepare_dispatch(core,cid)
    old = delegation_values();failed_preparation(core,cid,details,old)
    rejected = rejected_dispatch(core,cid,status=403)
    for _ in range(3):
        core.recover_dispatches('local','owner',cid)
        assert core.store.get_callback_outbox(rejected['callback_id'])['status'] == 'rejected'
    retry = Control(action='retry',idempotency_key='explicit-current-retry')
    core.control('local','owner',cid,'goal',retry)
    fresh = delegation_values();fresh['request_digest'] = 'fresh-policy-recheck'
    failed_preparation(core,cid,details,fresh)
    rejected_dispatch(core,cid,status=403)
    for _ in range(3):core.recover_dispatches('local','owner',cid)
    assert core.store.get_callback_outbox(rejected['callback_id'])['attempts'] == 2
    assert core.service.started == []
    # Same owner key never raises a second retry after that retry was itself rejected.
    core.control('local','owner',cid,'goal',retry)
    assert core.store.get_callback_outbox(rejected['callback_id'])['status'] == 'rejected'
    with core.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0] == 0


@pytest.mark.parametrize('fence',['stop','goal_hold','conversation_hold','destination_disabled','foreign'])
def test_rejected_dispatch_retry_cannot_cross_authority_or_expiry(app_core,fence):
    core,cid,_ = app_core
    prepare_dispatch(core,cid);row = rejected_dispatch(core,cid)
    if fence == 'stop':core.control('local','owner',cid,'goal',Control(action='stop',idempotency_key='stop-before-retry'))
    elif fence in {'goal_hold','conversation_hold'}:
        with core.store._connect() as conn:
            table='coordinator_goals' if fence=='goal_hold' else 'coordinator_conversations'
            conn.execute(f'UPDATE {table} SET restore_hold=1 WHERE conversation_id=?',(cid,))
    elif fence == 'expired':
        # Snapshot immutability prevents changing its expiry. Advance the module clock instead.
        import workers_projects_runtime.coordinator_application as application
        original = application.datetime
        class FutureDateTime(original):
            @classmethod
            def now(cls,tz=None):return original.now(UTC)+timedelta(days=1)
        patch=pytest.MonkeyPatch();patch.setattr(application,'datetime',FutureDateTime)
    elif fence == 'destination_disabled':
        destination=core.service.webhook_destinations.get('local','owner',json.loads(core._conversation('local','owner',cid)['config_json'])['application']['destination_id'])
        core.service.webhook_destinations.update('local','owner',destination['destination_id'],enabled=False)
    try:
        with pytest.raises((CoordinatorConflict,CoordinatorScopeError)):
            core.control('local','other-owner' if fence=='foreign' else 'owner',cid,'goal',Control(action='retry',idempotency_key='forbidden-retry'))
        assert 'retry' not in core.goal_snapshot('local','owner',cid,'goal')['actions'] if fence!='foreign' else True
        assert core.store.get_callback_outbox(row['callback_id'])['status']=='rejected'
    finally:
        if fence=='expired':patch.undo()


def test_reservation_prevents_rearming_or_replacing_accepted_digest(app_core):
    core,cid,_=app_core
    details=prepare_dispatch(core,cid)
    values=delegation_values();failed_preparation(core,cid,details,values)
    rejected_dispatch(core,cid)
    original=core.service.reserve_delegation
    reserved=original(**values,start_run=False)
    assert core.store.get_delegation(reserved['work_ref'],tenant_id='local',owner_id='owner')
    assert core.goal_snapshot('local','owner',cid,'goal')['actions']==['stop']
    with pytest.raises(CoordinatorConflict,match='existing reservation'):
        core.dispatch('local','owner',cid,Dispatch(goal_id='goal',route_id='primary'))


def test_real_account_router_retry_and_receipt_need_no_fake_worker(account_client):
    client=account_client;core=client.app.state.coordinator;service=client.app.state.service
    core.provider._model=lambda *_,**__:SimpleNamespace(effort_choices=('high',))
    destination=service.webhook_destinations.create('tenant-a','owner-a',name='Synthetic current adapter',url='http://127.0.0.1:12345/adapter',payload_mode='envelope')
    cid=core.create('tenant-a','owner-a',CoordinatorConfig(model='synthetic-manager',effort='high',
        routes=[Route(id='primary',profile='codex-cli',model='codex-cli:synthetic-worker',effort='high',execution_mode='host')],
        application=ApplicationBinding(destination_id=destination['destination_id'],request_ref='opaque-retry-router')))['conversation_id']
    core.accept_turn('tenant-a','owner-a',cid,'original','Prepare the original report.',[Goal(id='goal',text='Prepare the original report.')])
    core.dispatch('tenant-a','owner-a',cid,Dispatch(goal_id='goal',route_id='primary'))
    before=rejected_dispatch(core,cid)
    url=f'/v1/coordinator/conversations/{cid}/goals/goal/actions'
    body={'action':'retry','idempotencyKey':'actual-router-retry'}
    response=client.post(url,params={'run_id':''},headers=account_headers(),json=body)
    assert response.status_code==202,response.text
    assert response.json()['action']=='retry'
    assert response.json()['goal']['state']=='accepted'
    for path in [url,url+'/receipt']:
        replay=client.post(path,params={'run_id':''},headers=account_headers(),json=body)
        assert replay.status_code==202,replay.text
        assert replay.json()['idempotentReplay'] is True
    replayed=core.store.get_callback_outbox(before['callback_id'])
    assert replayed['attempts']>=before['attempts']  # The real maintenance transport may already deliver the accepted retry.
    assert replayed['payload_json']==before['payload_json'] and replayed['expires_at']==before['expires_at']
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM coordinator_actions WHERE conversation_id=?",(cid,)).fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM workers').fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM active_work_action_uses').fetchone()[0]==0
    foreign=client.post(url,params={'run_id':''},headers=account_headers(owner_id='other-owner'),json={'action':'retry','idempotencyKey':'foreign-router-retry'})
    assert foreign.status_code in {403,404}


@pytest.mark.parametrize('changed',['goal','instruction','profile','model','effort','owner','idempotency','origin'])
def test_explicit_freshness_never_authorizes_different_accepted_work(app_core,changed):
    core,cid,_=app_core
    details=prepare_dispatch(core,cid);old=delegation_values()
    failed_preparation(core,cid,details,old);rejected_dispatch(core,cid)
    core.control('local','owner',cid,'goal',Control(action='retry',idempotency_key='same-original-retry'))
    fresh=copy.deepcopy(old);fresh['request_digest']='fresh-current-context'
    if changed in {'goal','instruction','profile'}:fresh[changed]='different'
    elif changed=='model':fresh['bootstrap_bundle']['launch_authority']['worker_model']='different'
    elif changed=='effort':fresh['bootstrap_bundle']['launch_authority']['worker_reasoning_effort']='low'
    elif changed=='owner':fresh['owner_id']='foreign'
    elif changed=='idempotency':fresh['idempotency_key']='different'
    elif changed=='origin':fresh['origin_ref']='different'
    with pytest.raises((CoordinatorConflict,CoordinatorScopeError)):
        prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],fresh)
    assert core.service.started==[]
    with core.store._connect() as conn:assert conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0]==0


def test_retry_receipt_survives_restart_and_does_not_rearm_after_later_rejection(app_core):
    from workers_projects_runtime.coordinator import CoordinatorService
    core,cid,_=app_core;prepare_dispatch(core,cid);before=rejected_dispatch(core,cid)
    control=Control(action='retry',idempotency_key='restart-exact-retry')
    first=core.control('local','owner',cid,'goal',control)
    rejected_dispatch(core,cid,status=403)
    restarted=CoordinatorService(core.store,core.service,core.provider)
    assert restarted.control('local','owner',cid,'goal',control)==first
    restarted.recover_retry_actions('local','owner',cid)
    assert core.store.get_callback_outbox(before['callback_id'])['status']=='rejected'
    with core.store._connect() as conn:assert conn.execute('SELECT COUNT(*) FROM callback_outbox WHERE event_type=?',('coordinator.dispatch_due',)).fetchone()[0]==1


def test_original_expiry_still_fences_fresh_preparation_after_explicit_retry(app_core,monkeypatch):
    import workers_projects_runtime.coordinator_application as application
    core,cid,_=app_core;details=prepare_dispatch(core,cid);values=delegation_values()
    failed_preparation(core,cid,details,values);rejected_dispatch(core,cid)
    core.control('local','owner',cid,'goal',Control(action='retry',idempotency_key='expires-after-retry'))
    original=application.datetime
    class FutureDateTime(original):
        @classmethod
        def now(cls,tz=None):return original.now(UTC)+timedelta(days=1)
    monkeypatch.setattr(application,'datetime',FutureDateTime)
    fresh=copy.deepcopy(values);fresh['request_digest']='new-after-expiry'
    with pytest.raises(CoordinatorConflict,match='no longer current'):
        prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],fresh)
    assert core.service.started==[]


def test_completed_action_history_does_not_hide_later_explicit_retry_recovery(app_core):
    from workers_projects_runtime.coordinator_application import recover_goal_actions
    core,cid,_=app_core;prepare_dispatch(core,cid);before=rejected_dispatch(core,cid)
    with core.store._connect() as conn:
        for i in range(80):
            conn.execute("INSERT INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json,application_json,response_json) VALUES(?,?,?,?,?,?)",
                (cid,'goal',f'historical-completed-{i}',canonical(Control(action='retry',idempotency_key=f'historical-completed-{i}').model_dump()),
                 canonical({'dispatch_retry':True}),canonical({'state':'accepted'})))
    control=Control(action='retry',idempotency_key='current-after-history')
    core.control('local','owner',cid,'goal',control)
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_actions SET response_json='' WHERE conversation_id=? AND idempotency_key=?",(cid,control.idempotency_key))
    recover_goal_actions(core,'local','owner',cid)
    with core.store._connect() as conn:
        row=conn.execute("SELECT response_json FROM coordinator_actions WHERE conversation_id=? AND idempotency_key=?",(cid,control.idempotency_key)).fetchone()
        assert row['response_json']
        assert conn.execute("SELECT COUNT(*) FROM coordinator_actions WHERE conversation_id=?",(cid,)).fetchone()[0]==81
    assert core.store.get_callback_outbox(before['callback_id'])['attempts']==1
    assert core.service.started==[]


def test_lost_retry_response_recovers_exact_receipt_after_worker_binds_without_new_effect(app_core):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from workers_projects_runtime.coordinator_api import install_coordinator_routes
    core,cid,_=app_core;details=prepare_dispatch(core,cid);old=delegation_values()
    failed_preparation(core,cid,details,old);rejected_dispatch(core,cid)
    key='lost-response-original-operation'
    accepted=core.control('local','owner',cid,'goal',Control(action='retry',idempotency_key=key))
    fresh=copy.deepcopy(old);fresh['request_digest']='fresh-after-explicit-retry'
    work=prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],fresh)
    app=FastAPI();install_coordinator_routes(app,core,lambda request:('local','owner'),lambda *_:None)
    client=TestClient(app);url=f'/v1/coordinator/conversations/{cid}/goals/goal/actions'
    automatic={'action':'retry','idempotencyKey':key,
        'runtimeBundle':{'schedule_access':{'destinations':[],'manage_destination_ids':[]}},
        'inheritedConversation':{'version':1,'conversation_id':'current-app-conversation','sources':[]},
        'sourceContext':{'version':1,'originRef':'synthetic-current-origin','sourceEventId':'synthetic-current-source',
            'sourceRevision':1,'surface':'web','outputContract':{'mode':'inherit'}}}
    for suffix in ['/preflight','/receipt','']:
        response=client.post(url+suffix,params={'run_id':work['initial_run_id']},json=automatic)
        assert response.status_code in {200,202},response.text
        receipt=response.json().get('receipt',response.json())
        assert receipt['goal']==accepted
    assert core.service.started==[work['worker_id']]
    assert core._goal(cid,'goal')['source_turn_id']=='source'
    with core.store._connect() as conn:assert conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0]==1
    for changed in [{**automatic,'instruction':'Replace the original goal'}, {'action':'stop','idempotencyKey':key},
                    {**automatic,'nativeInput':{'version':1,'requestId':'new-permission','requestFingerprint':'a'*64,'action':'accept'}}]:
        response=client.post(url+'/receipt',params={'run_id':work['initial_run_id']},json=changed)
        assert response.status_code in {409,422},response.text
    assert core.service.started==[work['worker_id']]


def test_first_dispatch_retry_cannot_discard_automatic_context_before_receipt_exists(app_core):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from workers_projects_runtime.coordinator_api import install_coordinator_routes
    core,cid,_=app_core;prepare_dispatch(core,cid);before=rejected_dispatch(core,cid)
    app=FastAPI();install_coordinator_routes(app,core,lambda request:('local','owner'),lambda *_:None)
    response=TestClient(app).post(f'/v1/coordinator/conversations/{cid}/goals/goal/actions',params={'run_id':''},
        json={'action':'retry','idempotencyKey':'no-existing-retry-receipt',
              'inheritedConversation':{'version':1,'conversation_id':'other-current-input','sources':[]},
              'sourceContext':{'version':1,'originRef':'synthetic-current-origin','sourceEventId':'synthetic-current-source',
                'sourceRevision':1,'surface':'web','outputContract':{'mode':'inherit'}}})
    assert response.status_code==409,response.text
    assert core.store.get_callback_outbox(before['callback_id'])['status']=='rejected'
