from pathlib import Path
import json
import os
import subprocess
import sys
import pytest

from workers_projects_runtime import native_api_keys as keys
from workers_projects_runtime.control_plane import ControlPlaneStore, ControlPlaneError, ControlPlaneConflict
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager, ProviderSetupManager
from workers_projects_runtime.mission_provider_accounts import MissionProviderAccountBinder, apply_bound_provider_account_environment
from workers_projects_runtime.native_key_reader import NativeKeyReadError, key_value

@pytest.fixture
def connected(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_ENABLE_NATIVE_API_KEYS', '1')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'host')
    monkeypatch.delenv('GLASSHIVE_SECURITY_MODE', raising=False)
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.provider_setup_binary', lambda provider: '/synthetic/cli')
    monkeypatch.setattr(keys, '_prepare_native_key', lambda *args: None)
    monkeypatch.setattr(keys, 'check_key', lambda provider, value: (True, 'Synthetic check accepted'))
    store = ControlPlaneStore(str(tmp_path / 'db.sqlite'))
    homes = ProviderAccountHomeManager(tmp_path / 'accounts')
    account = store.create_provider_account(tenant_id='test', owner_id='owner', provider='codex', label='Synthetic', auth_method='api_key', platform_support='supported', secret_locator='native-home://api-key', status='disconnected')
    args = dict(account_id=account['account_id'], tenant_id='test', owner_id='owner')
    manager = keys.NativeApiKeyManager(store, homes)
    return manager, store, homes, args


def test_connect_is_private_persistent_and_not_in_database(connected):
    manager, store, homes, args = connected
    result = manager.connect(**args, value='synthetic-test-key')
    assert result['status'] == 'ready'
    home = homes.account_home_path(**args)
    assert keys.read_key(home / 'api-key.json') == 'synthetic-test-key'
    assert (home / 'api-key.json').stat().st_mode & 0o777 == 0o600
    assert 'synthetic-test-key' not in json.dumps(store.list_provider_accounts(tenant_id='test', owner_id='owner'))
    assert b'synthetic-test-key' not in store.db_path.read_bytes()
    assert manager.connect(**args)['status'] == 'ready'


def test_minimal_native_reader_requires_exact_mount_and_safe_inode(connected, tmp_path):
    manager, _, homes, args = connected
    manager.connect(**args, value='synthetic-native-key')
    home = homes.account_home_path(**args)
    path = home / 'api-key.json'
    assert key_value(str(path), expected_mount=str(home)) == 'synthetic-native-key'
    with pytest.raises(NativeKeyReadError, match='mount'):
        key_value(str(path), expected_mount=str(tmp_path / 'other'))
    linked = tmp_path / 'linked-key'
    os.link(path, linked)
    with pytest.raises(NativeKeyReadError, match='missing or unsafe'):
        key_value(str(path), expected_mount=str(home))
    linked.unlink()
    path.chmod(0o604)
    with pytest.raises(NativeKeyReadError, match='missing or unsafe'):
        key_value(str(path), expected_mount=str(home))


def test_wrong_owner_and_busy_account_do_not_change_secret(connected):
    manager, store, homes, args = connected
    manager.connect(**args, value='synthetic-original')
    with pytest.raises(ControlPlaneError):
        manager.connect(**{**args, 'owner_id':'other'}, value='synthetic-replacement')
    lease = store.acquire_provider_lease(**args, lane='test', worker_id='worker', run_id='run', ttl_seconds=60)
    with pytest.raises(ControlPlaneConflict):
        manager.connect(**args, value='synthetic-replacement')
    assert keys.read_key(homes.account_home_path(**args)/'api-key.json') == 'synthetic-original'
    store.release_provider_lease(lease_id=lease['lease_id'], tenant_id='test', owner_id='owner')


def test_rejected_key_never_replaces_existing_and_revocation_not_ready(connected, monkeypatch):
    manager, store, homes, args = connected
    manager.connect(**args, value='synthetic-original')
    monkeypatch.setattr(keys,'check_key',lambda *_: (False,'Provider rejected this key'))
    assert manager.connect(**args, value='synthetic-invalid')['status'] == 'action_required'
    assert keys.read_key(homes.account_home_path(**args)/'api-key.json') == 'synthetic-original'
    assert manager.connect(**args)['status'] == 'action_required'


@pytest.mark.parametrize('kind',['symlink','hardlink'])
def test_seal_refuses_redirected_or_shared_key(connected, tmp_path, kind):
    manager, _, homes, args = connected
    manager.connect(**args, value='synthetic-original')
    path = homes.account_home_path(**args)/'api-key.json'
    path.unlink()
    target=tmp_path/'foreign'; target.write_text('{"value":"synthetic-foreign"}'); target.chmod(0o600)
    if kind == 'symlink': path.symlink_to(target)
    else: os.link(target,path)
    with pytest.raises(ControlPlaneError): manager.connect(**args)
    assert target.read_text() == '{"value":"synthetic-foreign"}'


def test_multitenant_route_cannot_be_enabled(connected,monkeypatch):
    manager, _, _, args = connected
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE','multi_user')
    with pytest.raises(ControlPlaneError): manager.connect(**args,value='synthetic-key')


def test_hosted_native_key_route_requires_packaged_isolation(monkeypatch):
    monkeypatch.setenv('GLASSHIVE_ENABLE_NATIVE_API_KEYS', '1')
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'multi_user')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'docker')
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'hosted-xfs')
    monkeypatch.delenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', raising=False)
    assert keys.enabled() is False
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', 'per_worker_container')
    assert keys.enabled() is True
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'host')
    assert keys.enabled() is False


def test_local_linux_native_keys_require_the_packaged_container_boundary(monkeypatch):
    monkeypatch.setenv('GLASSHIVE_ENABLE_NATIVE_API_KEYS', '1')
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'local')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'docker')
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'local-linux')
    monkeypatch.delenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', raising=False)
    assert keys.enabled() is False
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', 'per_worker_container')
    assert keys.enabled() is True
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'hosted-xfs')
    assert keys.enabled() is False
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'local-linux')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'host')
    assert keys.enabled() is False


def test_hosted_api_key_does_not_fall_through_to_broker(monkeypatch):
    from workers_projects_runtime.provider_accounts import provider_platform_support

    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'multi_user')
    monkeypatch.delenv('GLASSHIVE_ENABLE_NATIVE_API_KEYS', raising=False)
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.inference_broker_config_from_environment', lambda: object())
    assert provider_platform_support(provider='codex', auth_method='api_key', platform_name='linux') == 'unavailable'
    assert provider_platform_support(provider='codex', auth_method='enterprise_route', platform_name='linux') == 'supported'


@pytest.mark.parametrize('provider,runtime', [('codex', 'codex-cli'), ('claude', 'claude-code'), ('grok', 'grok-build')])
def test_hosted_two_owner_key_lifecycle_uses_contained_homes(tmp_path, monkeypatch, provider, runtime):
    from workers_projects_runtime.contained_account_launch import ContainedAccountLauncher
    from workers_projects_runtime.provider_accounts import provider_platform_support

    class SyntheticContainedLauncher(ContainedAccountLauncher):
        def __init__(self):
            self.calls = []

        def assert_quiescent(self, account_home):
            return None

        def run(self, request, **options):
            self.calls.append((request.account_home, request.purpose))
            output = '{"authMethod":"api_key","apiKeySource":"ANTHROPIC_API_KEY"}' if request.purpose == 'api-key-verify' else ''
            return subprocess.CompletedProcess(request.command, 0, stdout=output, stderr='')

    monkeypatch.setenv('GLASSHIVE_ENABLE_NATIVE_API_KEYS', '1')
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'multi_user')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', 'per_worker_container')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'docker')
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'hosted-xfs')
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.provider_setup_binary', lambda _: '/synthetic/cli')
    accepted = {'owner-a': True, 'owner-b': True}
    monkeypatch.setattr(keys, 'check_key', lambda _provider, value: (accepted[value.split('-')[1] + '-' + value.split('-')[2]], 'Synthetic provider check'))
    roots = {owner: tmp_path / owner for owner in ('owner-a', 'owner-b')}
    for root in roots.values():
        root.mkdir(mode=0o700)
    resolver = lambda _tenant, owner: roots[owner]
    launcher = SyntheticContainedLauncher()
    homes = ProviderAccountHomeManager(tmp_path / 'untrusted-default', owner_root_resolver=resolver, native_launcher=launcher)
    store = ControlPlaneStore(str(tmp_path / 'accounts.db'))
    manager = keys.NativeApiKeyManager(store, homes)
    assert provider_platform_support(provider=provider, auth_method='api_key', platform_name='linux') == 'isolated_substrate_required'
    assert provider_platform_support(provider=provider, auth_method='api_key', platform_name='linux', native_homes=homes) == 'supported'

    accounts = {}
    for owner in roots:
        account = store.create_provider_account(tenant_id='tenant', owner_id=owner, provider=provider,
            label='Synthetic '+owner, auth_method='api_key', platform_support='supported',
            secret_locator='native-home://api-key', status='disconnected')
        args = {'account_id': account['account_id'], 'tenant_id': 'tenant', 'owner_id': owner}
        key = f'synthetic-{owner}-{provider}'
        assert manager.connect(**args, value=key)['status'] == 'ready'
        home = homes.account_home_path(**args)
        assert home.is_relative_to(roots[owner])
        assert keys.read_key(home / 'api-key.json') == key
        assert key.encode() not in store.db_path.read_bytes()
        accounts[owner] = (args, home, key)

    first, first_home, first_key = accounts['owner-a']
    second, second_home, second_key = accounts['owner-b']
    with pytest.raises(ControlPlaneError):
        manager.connect(**{**first, 'owner_id': 'owner-b'}, value='synthetic-wrong-owner')
    assert keys.read_key(first_home / 'api-key.json') == first_key
    assert keys.read_key(second_home / 'api-key.json') == second_key

    reopened = keys.NativeApiKeyManager(ControlPlaneStore(str(store.db_path)),
        ProviderAccountHomeManager(tmp_path / 'untrusted-default', owner_root_resolver=resolver, native_launcher=launcher))
    assert reopened.connect(**first)['status'] == 'ready'
    worker = {'execution_mode': 'docker', '_glasshive_provider_account_bound': True,
        '_glasshive_provider_account_mount_host': str(first_home),
        '_glasshive_provider_account_mount_target': '/workspace/.provider-account',
        '_glasshive_provider_api_key_file': str(first_home / 'api-key.json')}
    environment = {'OPENAI_API_KEY': 'synthetic-ambient', 'ANTHROPIC_API_KEY': 'synthetic-ambient', 'XAI_API_KEY': 'synthetic-ambient'}
    keys.project_key(worker, environment, runtime)
    assert environment['GLASSHIVE_NATIVE_API_KEY_FILE'] == '/workspace/.provider-account/api-key.json'
    assert all(value not in json.dumps(environment) for value in (first_key, second_key, 'synthetic-ambient'))
    with pytest.raises(ControlPlaneError):
        keys.project_key({**worker, '_glasshive_provider_api_key_file': str(second_home / 'api-key.json')}, {}, runtime)

    accepted['owner-a'] = False
    assert reopened.connect(**first)['status'] == 'action_required'
    assert keys.read_key(first_home / 'api-key.json') == first_key
    binder = MissionProviderAccountBinder(db_path=str(store.db_path), home_root=homes.root,
        owner_root_resolver=resolver, native_launcher=launcher)
    selected = {'worker_id': 'selected-worker', 'tenant_id': 'tenant', 'owner_id': 'owner-a',
        'profile': runtime, 'execution_mode': 'docker',
        'bootstrap_bundle': {'provider_account': {'policy': 'personal_required', 'account_id': first['account_id']}}}
    with pytest.raises(Exception, match='not ready'):
        with binder.bind(selected, runtime_name=runtime, run_id='revoked-run', timeout_sec=10,
                         release_binding=lambda *_: None, reconcile_binding=lambda *_: None):
            pytest.fail('Revoked account entered native work')
    setup = ProviderSetupManager(store=store, home_root=homes.root, homes=homes,
        reconcile_provider_account_binding=lambda _home: None)
    assert setup.disconnect(**first)['status'] == 'disconnected'
    assert not first_home.exists()
    accepted['owner-a'] = True
    assert reopened.connect(**first, value='synthetic-owner-a-reconnected')['status'] == 'ready'
    assert keys.read_key(first_home / 'api-key.json') == 'synthetic-owner-a-reconnected'
    assert keys.read_key(second_home / 'api-key.json') == second_key
    if provider == 'codex':
        assert any(purpose == 'api-key-login' for _, purpose in launcher.calls)
    if provider == 'claude':
        assert any(purpose == 'api-key-verify' for _, purpose in launcher.calls)


def test_hosted_key_without_owner_root_releases_lease_on_failure(connected, tmp_path, monkeypatch):
    from workers_projects_runtime.contained_account_launch import ContainedAccountLauncher

    class SyntheticContainedLauncher(ContainedAccountLauncher):
        def __init__(self):
            pass

        def assert_quiescent(self, account_home):
            return None

    manager, store, homes, args = connected
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'multi_user')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', 'per_worker_container')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'docker')
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'hosted-xfs')
    assert keys.support('codex', homes) == 'isolated_substrate_required'
    with pytest.raises(ControlPlaneError):
        manager.connect(**args, value='synthetic-key')
    assert store.active_provider_account_lease(args['account_id']) is None
    broken_homes = ProviderAccountHomeManager(tmp_path / 'unused',
        owner_root_resolver=lambda *_: tmp_path / 'missing-owner-root',
        native_launcher=SyntheticContainedLauncher())
    assert keys.support('codex', broken_homes) == 'supported'
    with pytest.raises(ControlPlaneError):
        keys.NativeApiKeyManager(store, broken_homes).connect(**args, value='synthetic-key')
    assert store.active_provider_account_lease(args['account_id']) is None


def test_hosted_native_key_projects_only_selected_private_mount(connected, monkeypatch):
    manager, store, homes, args = connected
    manager.connect(**args, value='synthetic-owner-a-key')
    foreign = store.create_provider_account(
        tenant_id='test', owner_id='owner-b', provider='codex', label='Foreign',
        auth_method='api_key', platform_support='supported',
        secret_locator='native-home://api-key', status='disconnected',
    )
    foreign_args = dict(account_id=foreign['account_id'], tenant_id='test', owner_id='owner-b')
    manager.connect(**foreign_args, value='synthetic-owner-b-key')
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'multi_user')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', 'per_worker_container')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'docker')
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'hosted-xfs')
    home = homes.account_home_path(**args)
    worker = {
        'execution_mode': 'docker', '_glasshive_provider_account_bound': True,
        '_glasshive_provider_account_mount_host': str(home),
        '_glasshive_provider_account_mount_target': '/workspace/.provider-account',
        '_glasshive_provider_api_key_file': str(home / 'api-key.json'),
    }
    env = {'OPENAI_API_KEY': 'synthetic-ambient-key'}
    keys.project_key(worker, env, 'codex-cli')
    assert 'OPENAI_API_KEY' not in env
    assert env['GLASSHIVE_NATIVE_API_KEY_FILE'] == '/workspace/.provider-account/api-key.json'
    assert env['GLASSHIVE_NATIVE_API_KEY_PROVIDER'] == 'codex'
    assert 'synthetic-owner-a-key' not in json.dumps(env)
    assert 'synthetic-owner-b-key' not in json.dumps(env)
    assert keys._container_key_value(str(home / 'api-key.json'), expected_mount=str(home)) == 'synthetic-owner-a-key'
    assert keys._container_key_value(
        str(homes.account_home_path(**foreign_args) / 'api-key.json'),
        expected_mount=str(homes.account_home_path(**foreign_args)),
    ) == 'synthetic-owner-b-key'
    with pytest.raises(ControlPlaneError, match='outside the selected account mount'):
        keys.project_key(
            {**worker, '_glasshive_provider_api_key_file': str(homes.account_home_path(**foreign_args) / 'api-key.json')},
            {}, 'codex-cli',
        )


def test_child_process_reads_only_its_selected_account_key(connected):
    manager, store, homes, args = connected
    manager.connect(**args, value='synthetic-selected-key')
    foreign = store.create_provider_account(
        tenant_id='test', owner_id='owner-b', provider='codex', label='Foreign',
        auth_method='api_key', platform_support='supported',
        secret_locator='native-home://api-key', status='disconnected',
    )
    foreign_args = dict(account_id=foreign['account_id'], tenant_id='test', owner_id='owner-b')
    manager.connect(**foreign_args, value='synthetic-foreign-key')
    home = homes.account_home_path(**args)
    code = (
        'import os,sys; from workers_projects_runtime.native_api_keys import _container_key_value; '
        'key=_container_key_value(sys.argv[1],expected_mount=sys.argv[2]); '
        'os.environ["CODEX_API_KEY"]=key; '
        'sys.stdout.write(os.environ["CODEX_API_KEY"])'
    )
    child = subprocess.run(
        [sys.executable, '-c', code, str(home / 'api-key.json'), str(home)],
        capture_output=True, text=True, check=True,
    )
    assert child.stdout == 'synthetic-selected-key'
    assert child.stdout != keys.read_key(homes.account_home_path(**foreign_args) / 'api-key.json')
    assert b'synthetic-selected-key' not in store.db_path.read_bytes()


def test_admitted_foreground_native_child_sources_selected_owner_key(
    connected, tmp_path, monkeypatch
):
    from workers_projects_runtime.contained_account_launch import ContainedAccountLauncher
    from workers_projects_runtime.openclaw_runtime import RuntimeInfo
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime

    manager, store, homes, args = connected
    manager.connect(**args, value='synthetic-owner-a-key')
    foreign = store.create_provider_account(
        tenant_id='test', owner_id='owner-b', provider='codex', label='Foreign',
        auth_method='api_key', platform_support='supported',
        secret_locator='native-home://api-key', status='disconnected',
    )
    foreign_args = dict(account_id=foreign['account_id'], tenant_id='test', owner_id='owner-b')
    manager.connect(**foreign_args, value='synthetic-owner-b-key')
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'multi_user')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', 'per_worker_container')
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'docker')
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'hosted-xfs')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT', str(homes.root))

    class NativeChildProbe:
        runtime_name = 'codex-cli'

        def ensure_worker_ready(self, worker):
            return RuntimeInfo('codex-cli', 'synthetic-model', '', None, None, None, '', '', 1)

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            env = {'OPENAI_API_KEY': 'synthetic-ambient-key'}
            apply_bound_provider_account_environment(worker, env, runtime_name='codex-cli')
            assert env['GLASSHIVE_NATIVE_API_KEY_FILE'] == '/workspace/.provider-account/api-key.json'
            assert 'OPENAI_API_KEY' not in env
            home = Path(worker['_glasshive_provider_account_mount_host'])
            child = subprocess.run(
                [sys.executable, '-c',
                 'import os,sys; from workers_projects_runtime.native_api_keys import _container_key_value; '
                 'os.environ["CODEX_API_KEY"]=_container_key_value(sys.argv[1],expected_mount=sys.argv[2]); '
                 'sys.stdout.write(os.environ["CODEX_API_KEY"])',
                 str(home / 'api-key.json'), str(home)],
                capture_output=True, text=True, check=True,
            )
            return child.stdout

        def release_provider_account_binding(self, worker):
            return None

        def reconcile_provider_account_binding(self, account_home):
            return None

    class SyntheticContainedLauncher(ContainedAccountLauncher):
        def __init__(self):
            pass

        def assert_quiescent(self, account_home):
            return None

    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(store.db_path),
        provider_account_native_launcher=SyntheticContainedLauncher(),
    )
    runtime.codex = NativeChildProbe()
    receipts = []
    runtime.provider_account_binder.configure_allowed_ai_start(
        lambda worker, receipt: receipts.append(dict(receipt))
    )

    def foreground(account_id, owner_id, run_id):
        worker = {
            'worker_id': f'worker-{owner_id}', 'tenant_id': 'test', 'owner_id': owner_id,
            'profile': 'codex-cli', 'execution_mode': 'docker',
            'bootstrap_bundle_json': json.dumps({
                'run_mode': 'conversation', 'connection_id': account_id,
                'provider_account': {'policy': 'personal_required', 'account_id': account_id},
            }),
            '_allowed_ai_admission': {'connection_id': account_id},
        }
        return runtime._run_task_with_provider_account(
            worker, 'Synthetic admitted foreground work', timeout_sec=30, run_id=run_id
        )

    assert foreground(args['account_id'], 'owner', 'run-a') == 'synthetic-owner-a-key'
    assert foreground(foreign['account_id'], 'owner-b', 'run-b') == 'synthetic-owner-b-key'
    assert [receipt['connection_id'] for receipt in receipts] == [args['account_id'], foreign['account_id']]
    assert all(receipt['route_kind'] == 'native' for receipt in receipts)
    assert store.active_provider_account_lease(args['account_id']) is None
    assert store.active_provider_account_lease(foreign['account_id']) is None
    unguarded = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(store.db_path)
    )
    unguarded.codex = NativeChildProbe()
    worker = {
        'worker_id': 'worker-unguarded', 'tenant_id': 'test', 'owner_id': 'owner',
        'profile': 'codex-cli', 'execution_mode': 'docker',
        'bootstrap_bundle_json': json.dumps({
            'run_mode': 'conversation',
            'provider_account': {'policy': 'personal_required', 'account_id': args['account_id']},
        }),
        '_allowed_ai_admission': {'connection_id': args['account_id']},
    }
    with pytest.raises(Exception, match='verified native quota launch guard'):
        unguarded._run_task_with_provider_account(
            worker, 'Must not run', timeout_sec=30, run_id='run-unguarded'
        )


def test_bound_environment_has_key_only_during_trusted_projection(connected):
    manager, store, homes, args = connected
    manager.connect(**args,value='synthetic-key')
    binder=MissionProviderAccountBinder(db_path=str(store.db_path),home_root=homes.root)
    worker={'worker_id':'worker','tenant_id':'test','owner_id':'owner','profile':'codex-cli','execution_mode':'host',
            'bootstrap_bundle': {'provider_account': {'policy':'personal_required','account_id':args['account_id']}}}
    # Selection metadata uses the canonical bundle contract.
    from workers_projects_runtime.mission_provider_accounts import mission_provider_account_selection
    assert mission_provider_account_selection(worker) is not None
    with binder.bind(worker,runtime_name='codex-cli',run_id='run',timeout_sec=10) as bound:
        assert 'synthetic-key' not in json.dumps(bound)
        env={'OPENAI_API_KEY':'ambient-wrong','OPENAI_BASE_URL':'https://invalid.example'}
        apply_bound_provider_account_environment(bound,env,runtime_name='codex-cli')
        assert env['CODEX_API_KEY']=='synthetic-key'
        assert env['OPENAI_API_KEY']=='synthetic-key'
        assert 'OPENAI_BASE_URL' not in env
    assert '_glasshive_provider_api_key_file' not in worker

@pytest.mark.parametrize('provider,runtime,key_name', [('codex','codex-cli','CODEX_API_KEY'),('claude','claude-code','ANTHROPIC_API_KEY'),('grok','grok-build','XAI_API_KEY')])
def test_three_native_projections_preserve_selected_provider(connected, monkeypatch, provider, runtime, key_name):
    manager, store, homes, _ = connected
    account = store.create_provider_account(tenant_id='test', owner_id='owner', provider=provider, label='Selected native key', auth_method='api_key', platform_support='supported',secret_locator='native-home://api-key',status='disconnected')
    args=dict(account_id=account['account_id'],tenant_id='test',owner_id='owner')
    manager.connect(**args,value='synthetic-selected-key')
    monkeypatch.setattr(keys,'_require_claude_key_route',lambda *_:None)
    binder=MissionProviderAccountBinder(db_path=str(store.db_path),home_root=homes.root)
    worker={'worker_id':'worker','tenant_id':'test','owner_id':'owner','profile':runtime,'execution_mode':'host', 'bootstrap_bundle':{'provider_account':{'policy':'personal_required','account_id':args['account_id']}}}
    with binder.bind(worker,runtime_name=runtime,run_id='run',timeout_sec=10) as bound:
        env={'ANTHROPIC_AUTH_TOKEN':'ambient','CLAUDE_CODE_USE_VERTEX':'1','CLAUDE_CODE_USE_FOUNDRY':'1','ANTHROPIC_PROFILE':'ambient','XAI_API_KEY':'ambient'}
        apply_bound_provider_account_environment(bound,env,runtime_name=runtime)
        assert env[key_name]=='synthetic-selected-key'
        if provider=='claude':
            assert not set(env).intersection({'ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY','ANTHROPIC_PROFILE'})
    worker['execution_mode']='docker'
    with pytest.raises(Exception):
        with binder.bind(worker,runtime_name=runtime,run_id='run',timeout_sec=10): pass


def test_disconnect_deletes_only_managed_home_without_native_logout(connected, monkeypatch):
    manager, store, homes, args=connected
    manager.connect(**args,value='synthetic-key')
    setup=ProviderSetupManager(store=store,home_root=homes.root)
    monkeypatch.setattr('subprocess.run',lambda *a,**k:pytest.fail('API key removal must not log out another native session'))
    result=setup.disconnect(**args)
    assert result['status']=='disconnected'
    assert not homes.account_home_path(**args).exists()
    assert 'Revoke' in result['message']


def test_fixed_endpoint_check_rejects_redirect_and_hides_provider_body(monkeypatch):
    from urllib.error import HTTPError
    seen=[]
    class FakeOpener:
        def open(self,request,timeout):
            seen.append((request.full_url,timeout))
            raise HTTPError(request.full_url,302,'secret-provider-body',{},None)
    monkeypatch.setattr(keys,'build_opener',lambda *args:FakeOpener())
    accepted,message=keys.check_key('codex','synthetic-key')
    assert not accepted and 'secret-provider-body' not in message
    assert seen==[('https://api.openai.com/v1/models',12)]
    assert keys._NoRedirect().redirect_request(None,None,302,'',{},'https://invalid.example') is None


def test_credential_api_validation_never_echoes_key(connected, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT',str(tmp_path/'api-accounts'))
    with TestClient(create_app(db_path=str(tmp_path/'api.sqlite'),runtime_backend='stub')) as client:
        response=client.post('/v1/provider-accounts',json={'provider':'codex','label':'Synthetic','auth_method':'api_key','platform_support':'ignored'})
        assert response.status_code==201,response.text
        account=response.json(); assert account['credential_route']=='native' and account['status']=='disconnected'
        path=f"/v1/provider-accounts/{account['account_id']}/credentials"
        invalid=client.post(path,json={'value':{'secret':'synthetic-secret'}})
        assert invalid.status_code==422 and 'synthetic-secret' not in invalid.text
        good=client.post(path,json={'value':'synthetic-secret'})
        assert good.status_code==200 and good.json()['status']=='ready'
        assert 'synthetic-secret' not in good.text
        assert client.post(f"/v1/provider-accounts/{account['account_id']}/verify").json()['status']=='ready'
        assert 'synthetic-secret' not in client.get('/v1/provider-accounts').text
