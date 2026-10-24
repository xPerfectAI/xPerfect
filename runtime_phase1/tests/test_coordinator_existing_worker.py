"""Existing Chat workers retain scope, native identity and exact assignment receipts."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_coordinator import Provider
from workers_projects_runtime.coordinator import (
    CoordinatorConfig, CoordinatorConflict, CoordinatorScope, CoordinatorScopeError,
    CoordinatorService, Dispatch, Goal, canonical, digest, guard_coordinator_run,
)
from workers_projects_runtime.coordinator_api import install_coordinator_routes
from workers_projects_runtime.coordinator_mcp import native_coordinator_server
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


@pytest.fixture
def existing(tmp_path):
    store = Store(tmp_path/'existing.db')
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False,
                                     start_background_consumers=False)
    service._ensure_worker_processor = lambda worker_id: None
    core = CoordinatorService(store, service, Provider())
    project = store.create_project('owner', 'Research', 'Retain useful work', 'openclaw-general')
    config = CoordinatorConfig(model='exact-native-model', effort='high',
                               scope=CoordinatorScope(project_id=project['project_id']))
    cid = core.create('local', 'owner', config)['conversation_id']
    bundle = {'provider_model': 'stub-model', 'project_definition': 'Original expertise',
              'env': {'WPR_CODEX_CLI_REASONING_EFFORT': 'high'},
              'files': [{'path': 'original.txt', 'content': 'Original files'}]}
    worker = store.create_worker(project['project_id'], 'owner', 'Morgan', 'Research lead',
                                 'openclaw-general', 'openclaw', 'openclaw-stub', 'stub-model',
                                 bootstrap_bundle=bundle)
    store.update_worker(worker['worker_id'], state='idle', session_key='retained-session')
    core.accept_turn('local', 'owner', cid, 'turn', 'Exact user goal.\nKeep  two spaces.',
                     [Goal(id='followup', text='Use the existing expert')])
    command = Dispatch(goal_id='followup', worker_id=worker['worker_id'], instruction='Continue exact work')
    yield core, cid, worker['worker_id'], command
    service.shutdown()
    store.close()


def dispatch(existing):
    core, cid, _, command = existing
    return core.dispatch('local', 'owner', cid, command)


def test_dispatch_target_schema_and_legacy_receipt_are_exact():
    original = {'goal_id': 'goal', 'route_id': 'route', 'instruction': 'Original exact request'}
    assert Dispatch(**original).request_payload() == original
    assert canonical(Dispatch(**original).request_payload()) == canonical(original)
    assert digest({'dispatch': Dispatch(**original).request_payload()}) == digest({'dispatch': original})
    for target in ({}, {'route_id': 'route', 'worker_id': 'worker'}):
        with pytest.raises(ValueError):
            Dispatch(goal_id='goal', instruction='Exact', **target)


def test_identity_context_result_and_idempotency(existing):
    core, cid, worker_id, command = existing
    before = core.store.get_worker(worker_id)
    starts = []
    def start(target):
        goal = core._goal(cid, command.goal_id)
        assert goal['run_id'] and goal['worker_id'] == target == worker_id and not goal['work_ref']
        with core.store._connect() as conn: guard_coordinator_run(conn, goal['run_id'])
        starts.append(target)
    core.service.start_assigned_run = start
    result = dispatch(existing)
    assert result['state'] == 'queued', result
    after = core.store.get_worker(worker_id)
    for key in ('project_id', 'name', 'profile', 'model', 'bootstrap_bundle_json', 'session_key'):
        assert after[key] == before[key]
    assert dispatch(existing)['run_id'] == result['run_id']
    assert starts == [worker_id]
    with core.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM workers').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0] == 0
        conn.execute("UPDATE coordinator_turns SET response_json=?,payload_json=? WHERE conversation_id=?",
                     (json.dumps({'choices':[{'message':{'content':'Exact earlier reply'}}]}),
                      json.dumps({'secret':'PRIVATE_REQUEST_RECORD'}), cid))
    from workers_projects_runtime.worker_context_sources import authorized_sources
    from workers_projects_runtime.control_plane import ControlPlaneStore
    ControlPlaneStore(core.store.db_path)
    sources = authorized_sources(core.store, None, {**after, '_context_run_id':result['run_id']})
    assert 'Exact user goal.\nKeep  two spaces.' in [source['text'] for source in sources]
    assert 'Exact earlier reply' in [source['text'] for source in sources]
    assert 'PRIVATE_REQUEST_RECORD' not in json.dumps(sources)
    core.store.update_run(result['run_id'], state='completed', output_text='Exact complete output')
    assert core.read_result('local','owner',cid,'followup')['output_text'] == 'Exact complete output'
    core.refresh('local','owner',cid)
    core.refresh('local','owner',cid)
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM coordinator_events WHERE identity=?", (result['run_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['owner', 'tenant', 'project', 'closed', 'conversation', 'unknown'])
def test_scope_rejections_never_assign(existing, fault):
    core, cid, worker_id, command = existing
    if fault == 'owner': core.store.update_worker(worker_id, owner_id='other')
    elif fault == 'tenant': core.store.update_worker(worker_id, tenant_id='other')
    elif fault == 'project':
        other = core.store.create_project('owner','Other','Other','openclaw-general')
        core.store.update_worker(worker_id, project_id=other['project_id'])
    elif fault == 'closed': core.store.update_worker(worker_id, state='terminated')
    elif fault == 'conversation': core.store.update_worker(worker_id, trusted_run_lane='conversation')
    else: command = command.model_copy(update={'worker_id':'unknown'})
    with pytest.raises((CoordinatorScopeError, CoordinatorConflict)): core.dispatch('local','owner',cid,command)
    with core.store._connect() as conn: assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    if fault != 'unknown': assert core.snapshot('local','owner',cid)['workers'] == []


@pytest.mark.parametrize('account_problem', ['unavailable', 'mismatch', 'not_ready'])
def test_account_unavailable_cannot_fall_back(existing, account_problem):
    core, cid, worker_id, _ = existing
    bundle = json.loads(core.store.get_worker(worker_id)['bootstrap_bundle_json'])
    bundle['provider_account'] = {'policy':'personal_required','account_id':'retained-account'}
    core.store.update_worker(worker_id, bootstrap_bundle_json=json.dumps(bundle))
    core.service._retained_provider_account_problem = lambda **kw: account_problem
    result = dispatch(existing)
    assert result['state'] == 'blocked' and result['blocker'] == 'worker_account_reconnect_required'
    with core.store._connect() as conn: assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    assert json.loads(core.store.get_worker(worker_id)['bootstrap_bundle_json']) == bundle


def test_concurrent_replay_and_changed_payload_never_duplicate(existing):
    core,cid,worker_id,command=existing
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: dispatch(existing), range(4)))
    assert len({result['run_id'] for result in results}) == 1
    with pytest.raises(CoordinatorConflict): core.dispatch('local','owner',cid,command.model_copy(update={'instruction':'Different'}))
    second = core.store.create_worker(core.store.get_worker(worker_id)['project_id'], 'owner', 'Other',
                                     'lead', 'openclaw-general', 'openclaw', 'openclaw-stub', 'stub-model')
    with pytest.raises(CoordinatorConflict): core.dispatch('local','owner',cid,command.model_copy(update={'worker_id':second['worker_id']}))
    with core.store._connect() as conn: assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


@pytest.mark.parametrize('claim', ['legacy', 'parallel'])
def test_busy_processor_cannot_claim_before_binding(existing, claim):
    core,cid,worker_id,_=existing
    assign=core.service.assign_run
    observed=[]
    def reserve(*args, **kwargs):
        assert kwargs['start_processor'] is False and kwargs['resume_paused_worker'] is False
        run=assign(*args, **kwargs)
        with core.store._connect() as conn,pytest.raises(CoordinatorConflict): guard_coordinator_run(conn,run['run_id'])
        assert getattr(core.store,'_claim_next_queued_run_'+claim)(worker_id) is None
        assert core.store.get_run(run['run_id'])['state']=='queued'
        observed.append(run['run_id'])
        return run
    core.service.assign_run=reserve
    result=dispatch(existing)
    assert observed==[result['run_id']]
    assert getattr(core.store,'_claim_next_queued_run_'+claim)(worker_id)['run_id']==result['run_id']


@pytest.mark.parametrize('state', ['paused','needs_input','running'])
def test_existing_current_work_is_preserved(existing,state):
    core,cid,worker_id,_=existing
    current=core.store.create_run(worker_id,core.store.get_worker(worker_id)['project_id'],'Unrelated current work')
    with core.store._connect() as conn:
        conn.execute('UPDATE runs SET state=? WHERE run_id=?',(state,current['run_id']))
    core.store.update_worker(worker_id,state=state)
    result=dispatch(existing)
    assert result['state']=='queued',result
    assert core.store.get_run(current['run_id'])['state']==state
    assert core.store.get_worker(worker_id)['state']==state
    assert core.store._claim_next_queued_run_parallel(worker_id) is None


@pytest.mark.parametrize('race', ['crash','cancel','goal_hold','conversation_hold'])
def test_unattached_receipt_recovery_and_hold_fences(existing,race):
    core,cid,worker_id,_=existing
    assign=core.service.assign_run
    saved=[]
    class Crash(BaseException): pass
    def reserve(*args,**kwargs):
        run=assign(*args,**kwargs);saved.append(run)
        with core.store._connect() as conn:
            if race=='cancel': conn.execute("UPDATE coordinator_goals SET intent_state='cancelled' WHERE conversation_id=?",(cid,))
            elif race=='goal_hold': conn.execute("UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?",(cid,))
            elif race=='conversation_hold': conn.execute("UPDATE coordinator_conversations SET restore_hold=1 WHERE conversation_id=?",(cid,))
        if race in {'crash','cancel'}: raise Crash()
        return run
    core.service.assign_run=reserve
    if race in {'crash','cancel'}:
        with pytest.raises(Crash): dispatch(existing)
    else: assert dispatch(existing)['state']=='restore_held'
    with core.store._connect() as conn,pytest.raises(CoordinatorConflict): guard_coordinator_run(conn,saved[0]['run_id'])
    assert core.store._claim_next_queued_run_parallel(worker_id) is None
    core.service.assign_run=assign
    if race in {'crash','cancel'}:
        recovered=core.recover_dispatches('local','owner',cid)
        assert recovered[0]['run_id']==saved[0]['run_id']
        assert recovered[0]['state']==('cancelled' if race=='cancel' else 'queued')
    else: assert core.recover_dispatches('local','owner',cid)==[]
    with core.store._connect() as conn: assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1


def test_exact_library_version_bind_precedes_start(existing):
    core,cid,worker_id,_=existing
    selection={'refs':[{'item_id':'item','version_id':'version-old'}],'upload_ids':['upload'],
               'upload_revisions':[{'upload_id':'upload','revision':'exact-revision'}]}
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET library_json=? WHERE conversation_id=?',(json.dumps(selection),cid))
    calls=[]
    core.service.files=SimpleNamespace(bind=lambda *args,**kwargs:calls.append((args,kwargs)))
    core.service.start_assigned_run=lambda target:calls.append(('start',target))
    before=core.store.get_worker(worker_id)['bootstrap_bundle_json']
    result=dispatch(existing)
    assert result['state']=='queued'
    assert calls[0][0][:4]==(worker_id,'local','owner',['upload'])
    assert calls[0][1]=={'revisions':selection['upload_revisions']}
    assert calls[1]==('start',worker_id)
    assert core.store.get_worker(worker_id)['bootstrap_bundle_json']==before


def test_http_and_mcp_share_exact_branch(existing):
    core,cid,worker_id,command=existing
    app=FastAPI()
    install_coordinator_routes(app,core,lambda request:('local',request.headers.get('x-owner','owner')),lambda *args:None)
    client=TestClient(app)
    url=f'/v1/coordinator/conversations/{cid}'
    roster=client.get(url).json()['workers']
    assert roster==[{key:core.store.get_worker(worker_id)[key] for key in ('worker_id','name','role','profile','model','state')}]
    assert client.post(url+'/dispatch',json=command.request_payload(),headers={'x-owner':'other'}).status_code==403
    assert client.post(url+'/dispatch',json={**command.request_payload(),'route_id':'route'}).status_code==422
    result=client.post(url+'/dispatch',json=command.request_payload())
    assert result.status_code==200,result.text
    core.native_identity=lambda principal:('local','owner',cid)
    peers=SimpleNamespace(native_principal=lambda token:{'worker_id':'foreground'})
    server=native_coordinator_server(core,peers,'http://127.0.0.1:8766/v1/native/coordinator/')
    schema=next(tool for tool in asyncio.run(server.list_tools()) if tool.name=='coordinator_dispatch').inputSchema
    assert set(schema['required'])=={'goal_id','instruction'}
    assert {'worker_id','route_id'}<=schema['properties'].keys()
    tool=server._tool_manager.get_tool('coordinator_dispatch')
    context=SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={'authorization':'Bearer exact'})))
    replay=tool.fn(goal_id=command.goal_id,instruction=command.instruction,worker_id=worker_id,ctx=context)
    assert replay['run_id']==result.json()['run_id']


def test_prepared_idle_worker_can_start_without_resuming_operator_pause(existing):
    core,_,worker_id,_=existing
    core.store.update_worker(worker_id,state='paused',compute_released_at='2026-10-24T00:00:00+00:00')
    result=dispatch(existing)
    assert result['state']=='queued'
    assert core.store.get_worker(worker_id)['state']=='starting'


def test_pending_ability_review_is_not_bypassed(existing):
    core,_,worker_id,_=existing
    core.store.update_worker(worker_id,duplication_report_json=json.dumps({
        'reapproval_items':[{'action_id':'review-one','kind':'library','resolution':'library_grant',
                            'reference':'unreviewed','label':'Review tool','route':'library','scopes':['tools']}]}))
    result=dispatch(existing)
    assert result['state']=='blocked'
    with core.store._connect() as conn: assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==0


def test_bound_restore_hold_fences_claim_and_final_admission(existing):
    core,cid,worker_id,_=existing
    result=dispatch(existing)
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_conversations SET restore_hold=1 WHERE conversation_id=?',(cid,))
    assert core.store._claim_next_queued_run_parallel(worker_id) is None
    with core.store._connect() as conn,pytest.raises(CoordinatorConflict): guard_coordinator_run(conn,result['run_id'])


def test_stopping_reused_goal_preserves_unrelated_worker_queue(existing):
    from workers_projects_runtime.coordinator import Control
    core,cid,worker_id,_=existing
    result=dispatch(existing)
    unrelated=core.store.create_run(worker_id,core.store.get_worker(worker_id)['project_id'],'Keep other work')
    receipt=core.control('local','owner',cid,'followup',Control(action='stop',run_id=result['run_id'],idempotency_key='stop-one'))
    assert receipt['state']=='cancelled'
    assert core.store.get_run(unrelated['run_id'])['state']=='queued'
    assert core.read_result('local','owner',cid,'followup')['run_id']==result['run_id']
    for action in ('queue','message','dismiss'):
        with pytest.raises(CoordinatorConflict):
            core.control('local','owner',cid,'followup',Control(action=action,run_id=result['run_id'],message='Extra',idempotency_key=action))


def test_failed_file_binding_never_admits_unattached_run(existing):
    core,cid,worker_id,_=existing
    selection={'refs':[{'item_id':'item','version_id':'version'}],'upload_ids':['missing'],'upload_revisions':[]}
    with core.store._connect() as conn: conn.execute('UPDATE coordinator_turns SET library_json=? WHERE conversation_id=?',(json.dumps(selection),cid))
    result=dispatch(existing)
    assert result['state']=='blocked' and not result['run_id']
    assert core.store._claim_next_queued_run_parallel(worker_id) is None


def test_status_roster_is_bounded_and_never_exposes_bootstrap(existing):
    core,cid,worker_id,_=existing
    project=core.store.get_worker(worker_id)['project_id']
    for i in range(101): core.store.create_worker(project,'owner',f'Member {i}','Research','openclaw-general','openclaw','openclaw-stub','stub-model')
    snapshot=core.snapshot('local','owner',cid)
    assert len(snapshot['workers'])==100 and snapshot['workers_truncated']
    assert all(set(row)=={'worker_id','name','role','profile','model','state'} for row in snapshot['workers'])


@pytest.mark.parametrize('state',['paused','needs_input','running'])
def test_stop_current_reused_run_preserves_other_pending_goals(existing,state):
    from workers_projects_runtime.coordinator import Control
    core,cid,worker_id,_=existing
    result=dispatch(existing)
    with core.store._connect() as conn:
        conn.execute('UPDATE runs SET state=? WHERE run_id=?',(state,result['run_id']))
        conn.execute('UPDATE workers SET state=? WHERE worker_id=?',(state,worker_id))
    other=core.store.create_run(worker_id,core.store.get_worker(worker_id)['project_id'],'Other accepted goal')
    receipt=core.control('local','owner',cid,'followup',Control(action='stop',run_id=result['run_id'],idempotency_key='stop-current'))
    assert receipt['state']=='cancelled' and not receipt.get('confirmation_pending')
    assert core.store.get_run(other['run_id'])['state']=='queued'
    assert not core.store.get_worker(worker_id)['work_stop_id']


def test_unknown_paused_native_stop_proof_stays_pending_then_recovers(existing):
    from workers_projects_runtime.coordinator import Control
    from workers_projects_runtime.openclaw_runtime import RuntimeInfo
    core,cid,worker_id,_=existing
    result=dispatch(existing)
    core.store.update_run(result['run_id'],state='paused')
    core.store.update_worker(worker_id,state='paused')
    other=core.store.create_run(worker_id,core.store.get_worker(worker_id)['project_id'],'Other goal')
    original=core.service.runtime.interrupt_worker
    core.service.runtime.interrupt_worker=lambda *args,**kw:RuntimeInfo(runtime='openclaw-stub',model='stub-model',gateway_url='',gateway_port=None,gateway_token=None,session_key='retained-session',state_dir=None,workspace_dir=None,pid=42)
    command=Control(action='stop',run_id=result['run_id'],idempotency_key='stop-paused')
    response=core.control('local','owner',cid,'followup',command)
    assert response['confirmation_pending'] and response['state']=='paused'
    assert core.store.get_run(other['run_id'])['state']=='queued'
    assert core.store.get_worker(worker_id)['compute_release_kind']=='cancel_run'
    core.service.runtime.interrupt_worker=original
    assert core.control('local','owner',cid,'followup',command)['state']=='cancelled'
    assert core.store.get_run(other['run_id'])['state']=='queued'


def test_two_chats_and_watch_keep_run_scoped_original_history(existing):
    from workers_projects_runtime.control_plane import ControlPlaneStore
    from workers_projects_runtime.worker_configuration import WorkerConfiguration
    from workers_projects_runtime.worker_context_sources import authorized_sources
    core,cid,worker_id,command=existing
    ControlPlaneStore(core.store.db_path)
    first=dispatch(existing)
    project=core.store.get_worker(worker_id)['project_id']
    second_cid=core.create('local','owner',CoordinatorConfig(model='exact-native-model',effort='high',scope=CoordinatorScope(project_id=project)))['conversation_id']
    core.accept_turn('local','owner',second_cid,'other-turn','Only second Chat evidence',[Goal(id='second',text='Second request')])
    second=core.dispatch('local','owner',second_cid,Dispatch(goal_id='second',worker_id=worker_id,instruction='Different job'))
    watch=core.store.create_run(worker_id,project,'Watch-only request')
    principal={}
    config=WorkerConfiguration(core.store,SimpleNamespace(native_principal=lambda *args,**kw:principal),
                               source_resolver=lambda worker:authorized_sources(core.store,None,worker))
    for run_id,wanted,unwanted in [(first['run_id'],'Exact user goal.','Only second Chat evidence'),
                                   (second['run_id'],'Only second Chat evidence','Exact user goal.'),
                                   (watch['run_id'],'','Only second Chat evidence')]:
        worker=core.store.get_worker(worker_id);run=core.store.get_run(run_id)
        projected=config.prepare_run(worker,{**run,'active_attempt_id':'context-attempt'})
        text=json.dumps(projected['_context_projection'])
        assert unwanted not in text
        if wanted:
            assert wanted in text
            principal.update(worker_id=worker_id,tenant_id='local',owner_id='owner',run_id=run_id,attempt_id='context-attempt')
            source=next(item for item in projected['_context_projection']['inline'] if wanted in item['text'])
            assert wanted in config.native_read('exact-run-token',source['id'])['text']
        else: assert 'Exact user goal.' not in text


@pytest.mark.parametrize('claim',['legacy','parallel'])
def test_held_front_assignment_does_not_block_independent_work(existing,claim):
    core,cid,worker_id,_=existing
    held=dispatch(existing)
    with core.store._connect() as conn: conn.execute('UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?',(cid,))
    other=core.store.create_run(worker_id,core.store.get_worker(worker_id)['project_id'],'Independent Watch work')
    assert getattr(core.store,'_claim_next_queued_run_'+claim)(worker_id)['run_id']==other['run_id']
    assert core.store.get_run(held['run_id'])['state']=='queued'


@pytest.mark.parametrize('kind',['ephemeral','delegated'])
def test_one_off_helpers_are_not_reusable_experts(existing,kind):
    core,cid,worker_id,command=existing
    if kind=='ephemeral': core.store.update_worker(worker_id,workspace_kind='ephemeral')
    else:
        work=core.store.reserve_delegation(tenant_id='local',owner_id='owner',idempotency_key='helper',request_digest='helper',
            origin_ref='helper',title='Helper',goal='Other job',instruction='Do job',origin_surface='coordinator',worker_name='g1',
            worker_role='worker',profile='openclaw-general',backend='openclaw',runtime='openclaw-stub',model='stub-model',execution_mode='docker')
        worker_id=work['worker_id']
        core.store.update_worker(worker_id,project_id=core.store.get_worker(command.worker_id)['project_id'])
        command=command.model_copy(update={'worker_id':worker_id})
    assert worker_id not in {worker['worker_id'] for worker in core.snapshot('local','owner',cid)['workers']}
    with pytest.raises(CoordinatorScopeError): core.dispatch('local','owner',cid,command)


@pytest.mark.parametrize('proof',['confirmed','never_started','unknown'])
def test_exact_cancel_host_pause_uses_same_generation_proof_and_keeps_queue(tmp_path,proof):
    from test_host_run_leases import _active_worker_and_run
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    store=Store(tmp_path/'host-cancel.db')
    _,worker,run=_active_worker_and_run(store,'host-cancel',execution_mode='host',
                                       run_state='queued' if proof=='never_started' else 'running')
    lease=store.get_active_host_run_lease_for_run(run['run_id'])
    store.transition_run_if_state(run['run_id'],run['state'],'paused')
    store.update_worker_state(worker['worker_id'],'paused')
    if lease:store.release_host_run_lease(lease['lease_id'],executor_id=lease['executor_id'],
                                        reason='pause_run_confirmed' if proof=='confirmed' else 'unknown')
    other=store.create_run(worker['worker_id'],worker['project_id'],'Unrelated queued work')
    class UnknownNative(StubRuntime):
        def interrupt_worker(self,worker,*,run_id=None): raise RuntimeErrorBase('Exact native absence is unproved')
    service=WorkersProjectsService(store,UnknownNative(),reconcile_on_startup=False,start_background_consumers=False)
    service._ensure_worker_processor=lambda worker_id:None
    try:
        if proof=='unknown':
            with pytest.raises(RuntimeErrorBase):service.cancel_run(worker['worker_id'],run['run_id'])
            assert store.get_run(run['run_id'])['state']=='paused'
            assert store.get_worker(worker['worker_id'])['compute_release_kind']=='cancel_run'
        else:
            service.cancel_run(worker['worker_id'],run['run_id'])
            assert store.get_run(run['run_id'])['state']=='cancelled'
        assert store.get_run(other['run_id'])['state']=='queued'
        assert not store.get_worker(worker['worker_id'])['work_stop_id']
    finally:
        service.shutdown();store.close()
