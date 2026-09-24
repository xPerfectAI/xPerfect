from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import subprocess
import time

import pytest
from workers_projects_runtime import current_native_account as native
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
