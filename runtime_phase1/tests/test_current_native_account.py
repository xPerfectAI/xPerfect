from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import subprocess
import time

import pytest
from workers_projects_runtime import current_native_account as native
from workers_projects_runtime import current_native_binding
from workers_projects_runtime.current_native_binding import recover_retained_leases
from workers_projects_runtime.control_plane import ControlPlaneStore, ControlPlaneError, ControlPlaneConflict
from workers_projects_runtime.mission_provider_accounts import MissionProviderAccountBinder, apply_bound_provider_account_environment, native_current_account_allowed
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager, ProviderSetupManager

@pytest.fixture
def host(tmp_path, monkeypatch):
    root=tmp_path.resolve()
    monkeypatch.setenv('HOME',str(root))
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE','host')
    for key in ('GLASSHIVE_SECURITY_MODE','GLASSHIVE_ENTERPRISE_MODE','WPR_ENTERPRISE_MODE','XPERFECT_STORAGE_ROOT'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.provider_setup_binary',lambda _: '/synthetic/claude')
    payload={'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','email':'person@example.test','orgId':'org-synthetic'}
    calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs))
        return subprocess.CompletedProcess(command,0,json.dumps(payload))
    monkeypatch.setattr(native.subprocess,'run',run)
    # Keep an unconfirmed stop's confirmation window short in tests.
    monkeypatch.setattr(current_native_binding,'STOP_CONFIRMATION_SECONDS',0.3)
    monkeypatch.setattr(current_native_binding,'STOP_CONFIRMATION_INTERVAL_SECONDS',0.01)
    binder=MissionProviderAccountBinder(db_path=str(root/'runtime.db'),home_root=root/'accounts')
    manager=native.CurrentNativeAccountManager(binder.store,binder.homes)
    return manager,binder,payload,calls


def connect(host):
    manager,binder,_,_=host
    result=manager.connect(tenant_id='local',owner_id='owner')
    account=result['account']
    worker={'worker_id':'worker','tenant_id':'local','owner_id':'owner','execution_mode':'host','profile':'claude-code',
            'bootstrap_bundle':{'provider_account':{'policy':'personal_required','account_id':account['account_id']}}}
    return account,worker


def test_current_sign_in_is_distinct_idempotent_private_and_never_copies_auth(host):
    manager,binder,_,calls=host
    account,_=connect(host)
    again=manager.connect(tenant_id='local',owner_id='owner')
    assert again['account_id']==account['account_id']
    assert account['auth_source']=='current_os_claude_subscription'
    assert 'person@example.test' not in json.dumps(account)
    assert 'person@example.test' not in binder.store.db_path.read_text(errors='ignore')
    assert not list(binder.homes.root.iterdir())
    assert all(command==['/synthetic/claude','auth','status','--json'] for command,_ in calls)


@pytest.mark.parametrize('field,value,state',[('loggedIn',False,'sign_in_required'),('authMethod','api_key','different_auth_route'),
    ('apiProvider','gateway','different_auth_route'),('email',None,'identity_unavailable'),('orgId',None,'identity_unavailable')])
def test_readiness_is_typed_and_requires_actual_subscription_identity(host,field,value,state):
    host[2][field]=value
    result=host[0].connect(tenant_id='local',owner_id='owner')
    assert result['state']==state and not result['complete']
    assert host[1].store.list_provider_accounts(tenant_id='local',owner_id='owner')==[]


def test_owner_environment_strips_ambient_auth_and_reports_missing_cli(host,monkeypatch):
    for key in ('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','CLAUDE_CODE_USE_VERTEX','ANTHROPIC_PROFILE','LD_PRELOAD','PYTHONPATH'):
        monkeypatch.setenv(key,'synthetic-ambient')
    native.readiness()
    assert not any(value=='synthetic-ambient' for value in host[3][-1][1]['env'].values())
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.provider_setup_binary',lambda _:None)
    assert native.readiness().state=='cli_missing'


@pytest.mark.parametrize('key,value',[('WPR_DEFAULT_EXECUTION_MODE','docker'),('GLASSHIVE_SECURITY_MODE','multi_user'),
    ('GLASSHIVE_ENTERPRISE_MODE','1'),('XPERFECT_STORAGE_ROOT','/synthetic/storage')])
def test_hosted_or_nonhost_never_probes_os_login(host,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises(ControlPlaneError):host[0].connect(tenant_id='local',owner_id='owner')
    assert host[3]==[]


def test_current_route_holds_exact_lease_and_does_not_allow_named_fallback(host):
    account,worker=connect(host);manager,binder,_,_=host
    stops=[]
    with binder.bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=lambda w:stops.append(w['_active_run_id'])) as bound:
        assert not native_current_account_allowed(bound) # generic ambient import remains blocked
        assert binder.store.active_provider_account_lease(account['account_id'])
        env={'ANTHROPIC_AUTH_TOKEN':'wrong','CLAUDE_CONFIG_DIR':'/synthetic/workspace/.claude'}
        apply_bound_provider_account_environment(bound,env,runtime_name='claude-code')
        assert 'ANTHROPIC_AUTH_TOKEN' not in env and env['CLAUDE_SECURESTORAGE_CONFIG_DIR']==''
        assert env['CLAUDE_CONFIG_DIR']=='/synthetic/workspace/.claude'
        with pytest.raises(ControlPlaneConflict):manager.verify(account_id=account['account_id'],tenant_id='local',owner_id='owner')
        with pytest.raises(ControlPlaneConflict):manager.disconnect(account_id=account['account_id'],tenant_id='local',owner_id='owner')
    assert stops==['run'] and binder.store.active_provider_account_lease(account['account_id']) is None
    assert not list(binder.homes.root.iterdir())
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    with pytest.raises(RuntimeErrorBase):
        apply_bound_provider_account_environment(worker,{},runtime_name='claude-code')
    with pytest.raises(ControlPlaneError, match='lease has ended'):
        apply_bound_provider_account_environment(bound,{},runtime_name='claude-code')


def test_changed_native_account_cannot_replace_selected_identity(host):
    account,worker=connect(host);host[2]['email']='different@example.test'
    result=host[0].verify(account_id=account['account_id'],tenant_id='local',owner_id='owner')
    assert result['status']=='action_required'
    with pytest.raises(ControlPlaneConflict):host[0].connect(tenant_id='local',owner_id='owner')
    with pytest.raises(ControlPlaneError):
        with host[1].bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=lambda _:None):pass


def test_uncertain_native_stop_retains_even_expired_lease_and_denies_reconnect(host):
    account,worker=connect(host);manager,binder,_,_=host
    def uncertain(_):raise RuntimeError('synthetic uncertain stop')
    with pytest.raises(RuntimeError):
        with binder.bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=uncertain):pass
    with binder.store._connect() as conn:conn.execute('UPDATE provider_account_leases SET expires_at=0 WHERE released_at IS NULL')
    with pytest.raises(ControlPlaneConflict):manager.verify(account_id=account['account_id'],tenant_id='local',owner_id='owner')
    with pytest.raises(ControlPlaneConflict):manager.disconnect(account_id=account['account_id'],tenant_id='local',owner_id='owner')


def test_stop_still_ending_from_an_interrupt_of_the_same_run_does_not_hold_the_account(host):
    """An Interrupt of the same run can still be ending its native generation when the run returns.
    The binding keeps confirming that exact stop, then releases the account as after any run."""
    account,worker=connect(host);manager,binder,_,_=host
    attempts=[]
    def ending(bound):
        attempts.append(bound['_active_run_id'])
        if len(attempts)<3:raise RuntimeError('Host run ownership changed during exact-run cleanup')
    with binder.bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=ending):pass
    assert attempts==['run','run','run']
    record=binder.store.get_provider_account_record(account_id=account['account_id'],tenant_id='local',owner_id='owner')
    assert record['status']=='ready' and record['recovery_code']==''
    assert binder.store.active_provider_account_lease(account['account_id']) is None
    assert manager.verify(account_id=account['account_id'],tenant_id='local',owner_id='owner')['status']=='ready'


def test_verify_releases_a_held_sign_in_only_after_its_exact_run_is_proven_stopped(host):
    account,worker=connect(host);manager,binder,_,_=host
    ids={'account_id':account['account_id'],'tenant_id':'local','owner_id':'owner'}
    def uncertain(_):raise RuntimeError('synthetic uncertain stop')
    with pytest.raises(RuntimeError):
        with binder.bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=uncertain):pass
    checked=[]
    def still_running(worker_id,run_id):
        checked.append((worker_id,run_id));raise RuntimeError('The exact host process identity is not confirmed')
    with pytest.raises(RuntimeError):recover_retained_leases(binder.store,confirm_stopped=still_running,**ids)
    assert checked==[('worker','run')]
    assert binder.store.active_provider_account_lease(account['account_id']) is not None
    with pytest.raises(ControlPlaneConflict):manager.verify(**ids)
    assert len(recover_retained_leases(binder.store,confirm_stopped=lambda *_:None,**ids))==1
    assert binder.store.active_provider_account_lease(account['account_id']) is None
    assert manager.verify(**ids)['status']=='ready'
    stops=[]
    with binder.bind(worker,runtime_name='claude-code',run_id='next',timeout_sec=1,abort_binding=lambda w:stops.append(w['_active_run_id'])):pass
    assert stops==['next']


def test_recovery_never_releases_the_lease_of_a_run_still_using_the_sign_in(host):
    account,worker=connect(host);binder=host[1]
    ids={'account_id':account['account_id'],'tenant_id':'local','owner_id':'owner'}
    with binder.bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=lambda _:None):
        assert recover_retained_leases(binder.store,confirm_stopped=lambda *_:pytest.fail('checked a live run'),**ids)==[]
        assert binder.store.active_provider_account_lease(account['account_id'])['run_id']=='run'


def test_second_owner_alias_and_preferred_or_container_route_cannot_use_current_account(host):
    account,worker=connect(host);manager,binder,_,_=host
    with pytest.raises(ControlPlaneConflict):manager.connect(tenant_id='local',owner_id='another-owner')
    for changed in (worker|{'execution_mode':'docker'},worker|{'bootstrap_bundle':{'provider_account':{'policy':'personal_preferred','account_id':account['account_id']}}}):
        with pytest.raises(ControlPlaneError):
            with binder.bind(changed,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=lambda _:None):pass
    setup=ProviderSetupManager(store=binder.store,home_root=binder.homes.root,homes=binder.homes)
    with pytest.raises(ControlPlaneError,match='Use Verify'):setup.start(account_id=account['account_id'],tenant_id='local',owner_id='owner')


def test_current_native_api_connect_verify_disconnect_are_owner_scoped(host,monkeypatch):
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app,StubRuntime
    manager,binder,_,_=host
    runtime=StubRuntime();runtime.host_claude=object();runtime.provider_account_binder=binder
    with TestClient(create_app(db_path=str(binder.store.db_path),runtime_backend='stub',runtime=runtime,reconcile_on_startup=False)) as client:
        assert client.get('/health').json()['current_native_claude']=={'available':True,'provider':'claude','execution_mode':'host'}
        assert client.get('/v1/provider-accounts/current-native/claude').json()['state']=='ready_to_try'
        result=client.post('/v1/provider-accounts/current-native/claude')
        assert result.status_code==200,result.text
        account_id=result.json()['account_id']
        assert client.post(f'/v1/provider-accounts/{account_id}/verify').json()['status']=='ready'
        assert client.post(f'/v1/provider-accounts/{account_id}/disconnect').json()['status']=='disconnected'
        assert 'person@example.test' not in client.get('/v1/provider-accounts').text


def test_held_run_recovery_needs_positive_proof_even_after_a_restart(tmp_path,monkeypatch):
    """A fresh runtime has no in-memory process handle, as after a restart. A missing session
    file is not proof: only the run's durable host records, with every recorded process proven
    gone, can release its held account lease. Uses real process identities, not the
    host fixture's faked subprocess.run."""
    monkeypatch.setenv('HOME',str(tmp_path))
    import fcntl,threading
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime
    runtime=HostClaudeCodeRuntime(base_dir=str(tmp_path/'native-work'))
    worker={'worker_id':'wrk_held','execution_mode':'host','profile':'claude-code'}
    live=subprocess.Popen(['sleep','60'],start_new_session=True)
    try:
        identity=runtime._process_start_identity(live.pid)
        assert identity.startswith('ps-lstart:')
        def record(**fields):
            return {'worker_id':'wrk_held','run_id':'run_held','status':'released','startup_state':'confirmed',
                    'pid':live.pid,'process_group':live.pid,'process_start_identity':identity,'attempt_id':'att_1',
                    'recorded_attempt_id':'att_1','attempt_runtime_invoked_at':'2026-09-26T00:00:00Z',**fields}
        def refused(records):
            with pytest.raises(RuntimeErrorBase):runtime.confirm_retained_run_stopped(worker,'run_held',records)
        assert not runtime._active_session_meta_path('wrk_held').exists()
        refused([])                                    # no durable record
        refused([record()])                            # released record, but its process is alive
        refused([record(status='active')])
        refused([record(startup_state='reserved',pid=None,process_group=None,process_start_identity='')])  # invoked, unidentified spawn
        refused([record(run_id='run_other')])
        runtime.confirm_retained_run_stopped(worker,'run_held',[record(startup_state='reserved',pid=None,process_group=None,
            process_start_identity='',attempt_runtime_invoked_at='')])  # never invoked the runtime
    finally:
        live.kill();live.wait()
    refused([record()])                                # supervisor gone, but its native child was never recorded
    run_root=runtime._run_root('wrk_held','run_held');run_root.mkdir(parents=True,exist_ok=True)
    child_record=runtime._native_child_record_path(run_root,'att_1')
    child_record.write_text(json.dumps({'supervisor_pid':live.pid+1,'child':None}))
    refused([record()])                                # a record from another supervisor proves nothing
    child_record.write_text(json.dumps({'supervisor_pid':live.pid,'child':None}))
    runtime.confirm_retained_run_stopped(worker,'run_held',[record()])  # the exact supervisor is gone and never started a child
    session=runtime._active_session_meta_path('wrk_held');session.parent.mkdir(parents=True,exist_ok=True)
    later=subprocess.Popen(['sleep','60'],start_new_session=True)
    try:
        later_identity=runtime._process_start_identity(later.pid)
        session.write_text(json.dumps({'session_name':'host-run_newer','run_id':'run_newer','process_pid':later.pid,
                                       'process_group':later.pid,'process_start_identity':later_identity}))
        refused([record()])                            # another run owns the workspace
        session.write_text(json.dumps({'session_name':'host-run_held','run_id':'run_held','process_pid':live.pid,
                                       'process_group':live.pid,'process_start_identity':identity}))
        runtime.confirm_retained_run_stopped(worker,'run_held',[record()])  # this run's session, process gone
        ledger=runtime._stop_descendant_ledger_path('wrk_held')
        ledger.write_text(json.dumps({'session':'x','descendants':{str(later.pid):later_identity}}))
        refused([record()])                            # a remembered descendant is still alive
        ledger.unlink()
        held=threading.Event();release=threading.Event()
        def hold():
            with open(session.parent/'active_terminal_session.lock','a+') as handle:
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX);held.set();release.wait(5)
        holder=threading.Thread(target=hold);holder.start();held.wait(5)
        started=time.monotonic()
        with pytest.raises(RuntimeErrorBase,match='still stopping'):runtime.confirm_retained_run_stopped(worker,'run_held',[record()])
        assert time.monotonic()-started<1              # never waits on a stop holding the lock
        release.set();holder.join()
    finally:
        later.kill();later.wait()


def test_held_run_recovery_needs_the_supervisors_native_child_gone_after_a_restart(tmp_path,monkeypatch):
    """The supervisor runs its native CLI in a separate process group. If the supervisor is
    killed abruptly, that child can outlive it with no Stop ledger entry, so only the child's
    own record, written before it runs, can prove the account free."""
    monkeypatch.setenv('HOME',str(tmp_path))
    import signal
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime
    runtime=HostClaudeCodeRuntime(base_dir=str(tmp_path/'native-work'))
    worker={'worker_id':'wrk_split','execution_mode':'host','profile':'claude-code'}
    run_root=runtime._run_root('wrk_split','run_split');run_root.mkdir(parents=True)
    child_record=runtime._native_child_record_path(run_root,'att_split')
    command=runtime._durable_host_process_command(['sleep','60'],run_root=run_root,exit_path=run_root/'exit_code',
                                                  child_record_path=child_record)
    supervisor=subprocess.Popen(command,start_new_session=True)
    child=None
    def wait_for(check):
        deadline=time.monotonic()+10
        while not check():
            assert time.monotonic()<deadline
            time.sleep(0.05)
    try:
        wait_for(lambda:(run_root/'supervisor-ready').exists())
        assert json.loads(child_record.read_text())=={'supervisor_pid':supervisor.pid,'child':None}
        (run_root/'start-permit').write_text('synthetic permit')
        wait_for(lambda:(json.loads(child_record.read_text()).get('child') or {}).get('process_start_identity'))
        child=json.loads(child_record.read_text())['child']
        assert child['pid']!=supervisor.pid and child['process_group']==child['pid']
        supervisor_identity=runtime._process_start_identity(supervisor.pid)
        os.kill(supervisor.pid,signal.SIGKILL);supervisor.wait()
        os.kill(child['pid'],0)                        # the native child outlived its supervisor
        record={'worker_id':'wrk_split','run_id':'run_split','status':'released','startup_state':'confirmed',
                'pid':supervisor.pid,'process_group':supervisor.pid,'process_start_identity':supervisor_identity,
                'attempt_id':'att_split','recorded_attempt_id':'att_split','attempt_runtime_invoked_at':'2026-09-26T00:00:00Z'}
        restarted=HostClaudeCodeRuntime(base_dir=str(tmp_path/'native-work'))  # empty handle map, no session or ledger
        assert not restarted._active_session_meta_path('wrk_split').exists()
        assert not restarted._stop_descendant_ledger_path('wrk_split').exists()
        with pytest.raises(RuntimeErrorBase):restarted.confirm_retained_run_stopped(worker,'run_split',[record])
    finally:
        if child:
            try:os.killpg(child['process_group'],signal.SIGKILL)
            except ProcessLookupError:pass
        if supervisor.poll() is None:supervisor.kill();supervisor.wait()
    def child_gone():
        try:os.kill(child['pid'],0)
        except ProcessLookupError:return True
        return restarted._pid_is_zombie(child['pid'])
    wait_for(child_gone)
    restarted.confirm_retained_run_stopped(worker,'run_split',[record])


def test_verify_api_checks_the_held_runs_exact_stop_before_releasing_the_sign_in(host):
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app,StubRuntime
    manager,binder,_,_=host
    runtime=StubRuntime();runtime.host_claude=object();runtime.provider_account_binder=binder
    checked=[]
    def confirm(worker,run_id,host_leases):
        assert host_leases==binder_store.host_run_leases_for_run(worker['worker_id'],run_id)
        checked.append((worker['worker_id'],worker['execution_mode'],run_id))
        if len(checked)==1:raise RuntimeError('The exact host process identity is not confirmed')
    runtime.confirm_native_run_stopped=confirm
    from workers_projects_runtime.store import Store
    binder_store=Store(str(binder.store.db_path))
    with TestClient(create_app(db_path=str(binder.store.db_path),runtime_backend='stub',runtime=runtime,reconcile_on_startup=False)) as client:
        account_id=client.post('/v1/provider-accounts/current-native/claude').json()['account_id']
        account=next(a for a in client.get('/v1/provider-accounts').json()['items'] if a['account_id']==account_id)
        owner,tenant=account['owner_id'],account['tenant_id']
        project=client.post('/v1/projects',json={'owner_id':owner,'title':'Held','goal':'Synthetic'}).json()
        worker=client.post(f"/v1/projects/{project['project_id']}/workers",json={'owner_id':owner,'name':'Held','role':'main',
            'profile':'claude-code','execution_mode':'host','bootstrap_bundle':{'provider_account':{'policy':'personal_required','account_id':account_id}}}).json()
        record=binder.store.get_provider_account_record(account_id=account_id,tenant_id=tenant,owner_id=owner)
        held={'worker_id':worker['worker_id'],'tenant_id':tenant,'owner_id':owner,'execution_mode':'host','profile':'claude-code',
              'bootstrap_bundle':{'provider_account':{'policy':'personal_required','account_id':account_id}}}
        assert record['status']=='ready'
        with pytest.raises(RuntimeError):
            with binder.bind(held,runtime_name='claude-code',run_id='run_held',timeout_sec=1,abort_binding=lambda _:(_ for _ in ()).throw(RuntimeError('uncertain'))):pass
        refused=client.post(f'/v1/provider-accounts/{account_id}/verify')
        assert refused.status_code==409 and 'not proven stopped' in refused.text
        assert checked==[(worker['worker_id'],'host','run_held')]
        assert binder.store.active_provider_account_lease(account_id)['run_id']=='run_held'
        result=client.post(f'/v1/provider-accounts/{account_id}/verify')
        assert result.status_code==200 and result.json()['status']=='ready'
        assert checked[-1]==(worker['worker_id'],'host','run_held')
        assert binder.store.active_provider_account_lease(account_id) is None


def test_no_fake_marker_or_changed_account_can_replay_native_binding(host):
    account,worker=connect(host);binder=host[1]
    fake=worker|{'_glasshive_provider_account_bound':True,native.MARKER:{'account_id':account['account_id'],'identity':'a'*64,'lease_id':'fake'}}
    with pytest.raises(ControlPlaneError,match='lease authority'):apply_bound_provider_account_environment(fake,{},runtime_name='claude-code')
    with binder.bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=lambda _:None) as bound:
        host[2]['email']='switched@example.test'
        with pytest.raises(ControlPlaneError,match='sign-in changed'):apply_bound_provider_account_environment(bound,{},runtime_name='claude-code')


def test_concurrent_connect_cannot_create_duplicate_current_native_records(host):
    manager,binder,_,_=host
    def connect_one(_):
        try:return manager.connect(tenant_id='local',owner_id='owner')['account_id']
        except ControlPlaneConflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(connect_one,range(2)))
    accounts=binder.store.list_provider_accounts(tenant_id='local',owner_id='owner')
    assert len(accounts)==1 and set(filter(None,ids))=={accounts[0]['account_id']}


def test_real_host_command_builder_uses_lease_route_without_keychain_copy(host,tmp_path,monkeypatch):
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime
    account,worker=connect(host);binder=host[1]
    monkeypatch.setenv('WPR_CLAUDE_CODE_ENABLE_CHROME','0')
    runtime=HostClaudeCodeRuntime(base_dir=str(tmp_path/'native-work'))
    monkeypatch.setattr(runtime,'_inject_private_subscription_auth',lambda *_:pytest.fail('generic account/keychain fallback'))
    monkeypatch.setattr('workers_projects_runtime.profile_runtime._read_claude_keychain_oauth',lambda *a,**k:pytest.fail('credential read'))
    worker.update(workspace_root=str(tmp_path/'workspace'),model='opus')
    with binder.bind(worker,runtime_name='claude-code',run_id='run',timeout_sec=1,abort_binding=lambda _:None) as bound:
        command,env=runtime._build_command(bound,'Synthetic task',runtime._host_runtime_info(bound))
        assert command[-2:]==['--setting-sources','']
        assert env['CLAUDE_SECURESTORAGE_CONFIG_DIR']==''
        assert not set(env).intersection({'CLAUDE_CODE_OAUTH_TOKEN','CLAUDE_CODE_OAUTH_REFRESH_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN'})


def test_conversation_flag_cannot_turn_selected_account_into_ambient_auth(host):
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    _,worker=connect(host)
    worker['bootstrap_bundle']['run_mode']='conversation'
    with pytest.raises(RuntimeErrorBase,match='explicitly selected account'):
        apply_bound_provider_account_environment(worker,{},runtime_name='claude-code')


def test_a_child_without_its_record_is_still_in_the_supervisors_group(tmp_path,monkeypatch):
    """Widen the gap between a child's fork and its record: until the record exists, the child
    stays in the supervisor's process group, so a supervisor killed in that gap can never be
    taken for one whose child never started."""
    monkeypatch.setenv('HOME',str(tmp_path))
    import signal
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime
    runtime=HostClaudeCodeRuntime(base_dir=str(tmp_path/'native-work'))
    worker={'worker_id':'wrk_gap','execution_mode':'host','profile':'claude-code'}
    run_root=runtime._run_root('wrk_gap','run_gap');run_root.mkdir(parents=True)
    child_record=runtime._native_child_record_path(run_root,'att_gap')
    command=runtime._durable_host_process_command(['sleep','60'],run_root=run_root,exit_path=run_root/'exit_code',
                                                  child_record_path=child_record)
    supervisor_script=run_root/'native-process-supervisor.py'
    source=supervisor_script.read_text()
    anchor='def record_child_before_exec() -> None:\n'
    assert source.count(anchor)==1
    supervisor_script.write_text(source.replace(anchor,anchor+'    time.sleep(2.0)  # widened scheduling gap\n'))
    supervisor=subprocess.Popen(command,start_new_session=True)
    child_pid=0
    try:
        deadline=time.monotonic()+10
        while not (run_root/'supervisor-ready').exists():
            assert time.monotonic()<deadline;time.sleep(0.02)
        (run_root/'start-permit').write_text('synthetic permit')
        while not child_pid:
            assert time.monotonic()<deadline
            found=subprocess.run(['pgrep','-P',str(supervisor.pid)],capture_output=True,text=True).stdout.split()
            child_pid=int(found[0]) if found else 0
            time.sleep(0.02)
        assert json.loads(child_record.read_text())['child'] is None   # forked, not yet recorded
        assert os.getpgid(child_pid)==supervisor.pid                   # and still in the supervisor's group
        supervisor_identity=runtime._process_start_identity(supervisor.pid)
        os.kill(supervisor.pid,signal.SIGKILL);supervisor.wait()
        record={'worker_id':'wrk_gap','run_id':'run_gap','status':'released','startup_state':'confirmed',
                'pid':supervisor.pid,'process_group':supervisor.pid,'process_start_identity':supervisor_identity,
                'attempt_id':'att_gap','recorded_attempt_id':'att_gap','attempt_runtime_invoked_at':'2026-09-26T00:00:00Z'}
        restarted=HostClaudeCodeRuntime(base_dir=str(tmp_path/'native-work'))
        with pytest.raises(RuntimeErrorBase):restarted.confirm_retained_run_stopped(worker,'run_gap',[record])
    finally:
        if child_pid:
            for target in (child_pid,):
                try:os.killpg(os.getpgid(target),signal.SIGKILL)
                except (ProcessLookupError,PermissionError):pass
                try:os.kill(target,signal.SIGKILL)
                except ProcessLookupError:pass
        if supervisor.poll() is None:supervisor.kill();supervisor.wait()
