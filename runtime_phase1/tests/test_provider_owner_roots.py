from pathlib import Path
import os

import pytest

from workers_projects_runtime.api import create_app, StubRuntime
from workers_projects_runtime.control_plane import ControlPlaneError, ControlPlaneStore
from workers_projects_runtime.mission_provider_accounts import MissionProviderAccountBinder
from workers_projects_runtime.native_api_keys import NativeApiKeyManager
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager, ProviderSetupManager


def test_local_home_layout_remains_unchanged(tmp_path):
    root=tmp_path.resolve()/'legacy'
    homes=ProviderAccountHomeManager(root)
    first=homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_test',provider='codex')
    assert first == homes.account_home_path(tenant_id='tenant',owner_id='owner',account_id='acct_test')
    assert first.parent.parent.parent == root
    assert len(first.parent.name) == 24 and len(first.parent.parent.name) == 24


def test_hosted_resolver_places_all_account_bytes_under_owner_root_without_legacy_writes(tmp_path):
    root=tmp_path.resolve(); legacy=root/'legacy'; owner=root/'owner';owner.mkdir(mode=0o700)
    calls=[]
    def resolve(tenant_id,owner_id): calls.append((tenant_id,owner_id));return owner
    homes=ProviderAccountHomeManager(legacy,owner_root_resolver=resolve)
    assert not legacy.exists()
    for provider in ('codex','claude','grok'):
        home=homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_'+provider,provider=provider)
        assert home == owner/'provider-accounts'/('acct_'+provider)
        environment=homes.runtime_environment(provider=provider,account_home=home)
        assert all(Path(value).is_relative_to(owner) for value in environment.values())
        assert home.stat().st_mode & 0o777 == 0o700
    assert calls == [('tenant','owner')]*3
    assert not legacy.exists()


def test_existing_local_account_is_not_silently_migrated_or_modified(tmp_path):
    root=tmp_path.resolve();legacy=root/'legacy';owner=root/'owner';owner.mkdir(mode=0o700)
    old=ProviderAccountHomeManager(legacy).ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_existing',provider='codex')
    marker=old/'codex/auth.json';marker.write_text('synthetic old auth')
    homes=ProviderAccountHomeManager(legacy,owner_root_resolver=lambda tenant,owner_id:owner)
    with pytest.raises(ControlPlaneError,match='explicit stopped-state migration'):
        homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_existing',provider='codex')
    assert marker.read_text() == 'synthetic old auth'
    assert not (owner/'provider-accounts').exists()


def test_resolver_cannot_change_an_owner_path_or_inode_after_first_use(tmp_path):
    root=tmp_path.resolve();owner=root/'owner';owner.mkdir(mode=0o700)
    homes=ProviderAccountHomeManager(root/'unused',owner_root_resolver=lambda tenant,owner_id:owner)
    homes.account_home_path(tenant_id='tenant',owner_id='owner',account_id='acct_test')
    owner.rename(root/'previous');owner.mkdir(mode=0o700)
    with pytest.raises(ControlPlaneError,match='owner root changed'):
        homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_test',provider='codex')
    assert not (owner/'provider-accounts').exists()


def test_symlink_or_relative_resolver_and_redirected_provider_directory_are_rejected(tmp_path):
    root=tmp_path.resolve();owner=root/'owner';owner.mkdir(mode=0o700);alias=root/'alias';alias.symlink_to(owner)
    for result in (alias,Path('relative')):
        homes=ProviderAccountHomeManager(root/'unused',owner_root_resolver=lambda tenant,owner_id:result)
        with pytest.raises(ControlPlaneError): homes.account_home_path(tenant_id='tenant',owner_id='owner',account_id='acct_test')
    outside=root/'outside';outside.mkdir(mode=0o700);(owner/'provider-accounts').symlink_to(outside)
    homes=ProviderAccountHomeManager(root/'unused',owner_root_resolver=lambda tenant,owner_id:owner)
    with pytest.raises(ControlPlaneError,match='private trusted storage'):
        homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_test',provider='codex')
    assert not (outside/'acct_test').exists()


def test_setup_api_key_binder_and_api_share_exact_trusted_manager(tmp_path, monkeypatch):
    root=tmp_path.resolve();owner=root/'owner';owner.mkdir(mode=0o700)
    binder=MissionProviderAccountBinder(db_path=str(root/'runtime.db'),home_root=root/'unused',owner_root_resolver=lambda tenant,owner_id:owner)
    setup=ProviderSetupManager(store=binder.store,home_root=root/'unused',homes=binder.homes)
    assert setup.homes is binder.homes
    assert NativeApiKeyManager(binder.store,setup.homes).homes is binder.homes
    runtime=StubRuntime();runtime.provider_account_binder=binder
    monkeypatch.setenv('VIVENTIUM_ENV_FILE','')
    app=create_app(db_path=str(root/'runtime.db'),runtime_backend='stub',runtime=runtime,reconcile_on_startup=False)
    assert app.state.provider_setup.homes is binder.homes
    assert not (root/'unused').exists()


def test_grok_verification_command_targets_the_native_image(monkeypatch):
    manager = object.__new__(ProviderSetupManager)
    monkeypatch.setattr(manager, '_binary', lambda _provider: '/usr/local/bin/grok')
    setup, verify = manager._commands('grok', contained=True)
    assert setup == ['/usr/local/bin/grok', 'login', '--device-auth']
    assert verify == [
        '/usr/bin/python3', '-I', '-m', 'workers_projects_runtime.grok_auth',
        '--binary', '/usr/local/bin/grok',
    ]
    _, host_verify = manager._commands('grok', contained=False)
    assert host_verify[0] == __import__('sys').executable
    assert host_verify[1].endswith('/workers_projects_runtime/grok_auth.py')


def test_contained_chromium_singleton_links_are_removed_after_quiescence(tmp_path):
    from workers_projects_runtime.contained_account_launch import ContainedAccountLauncher

    launcher = object.__new__(ContainedAccountLauncher)
    launcher.assert_quiescent = lambda _home: None
    manager = ProviderAccountHomeManager(tmp_path / 'provider-homes', native_launcher=launcher)
    home = manager.ensure_home(
        tenant_id='tenant-a', owner_id='user-a', account_id='acct_grok', provider='grok'
    )
    chromium = home / '.config' / 'chromium'
    chromium.mkdir(parents=True)
    (chromium / 'SingletonSocket').symlink_to(
        '/workspace/account/.tmp/org.chromium.synthetic/SingletonSocket'
    )
    (chromium / 'SingletonCookie').symlink_to('123456789')
    (chromium / 'SingletonLock').symlink_to('synthetic-host-17')
    tmp_chromium = home / '.tmp' / 'org.chromium.synthetic'
    tmp_chromium.mkdir(parents=True)
    (tmp_chromium / 'SingletonCookie').symlink_to('123456789')
    manager.tighten_permissions(account_home=home)
    assert not any(os.path.lexists(chromium / name) for name in (
        'SingletonSocket', 'SingletonCookie', 'SingletonLock'
    ))
    assert not os.path.lexists(tmp_chromium / 'SingletonCookie')


def test_unrecognized_chromium_link_remains_fail_closed_with_contained_launcher(tmp_path):
    from workers_projects_runtime.contained_account_launch import ContainedAccountLauncher

    launcher = object.__new__(ContainedAccountLauncher)
    launcher.assert_quiescent = lambda _home: None
    manager = ProviderAccountHomeManager(tmp_path / 'provider-homes', native_launcher=launcher)
    home = manager.ensure_home(
        tenant_id='tenant-a', owner_id='user-a', account_id='acct_grok', provider='grok'
    )
    chromium = home / '.config' / 'chromium'
    chromium.mkdir(parents=True)
    link = chromium / 'SingletonSocket'
    link.symlink_to('/workspace/account/.tmp/not-chromium/secret')
    with pytest.raises(ControlPlaneError, match='unsafe link'):
        manager.tighten_permissions(account_home=home)
    assert link.is_symlink()


def test_hosted_disconnect_removes_only_selected_owner_account(tmp_path):
    root=tmp_path.resolve();owners={key:root/key for key in ('owner-a','owner-b')}
    for owner in owners.values():owner.mkdir(mode=0o700)
    homes=ProviderAccountHomeManager(root/'unused',owner_root_resolver=lambda tenant,owner_id:owners[owner_id])
    for key in owners:
        path=homes.ensure_home(tenant_id='tenant',owner_id=key,account_id='acct_test',provider='codex')
        (path/'codex/auth.json').write_text('synthetic '+key)
    homes.remove_home(tenant_id='tenant',owner_id='owner-a',account_id='acct_test')
    assert not (owners['owner-a']/'provider-accounts/acct_test').exists()
    assert (owners['owner-b']/'provider-accounts/acct_test/codex/auth.json').read_text() == 'synthetic owner-b'


def test_hosted_sealing_uses_verified_owner_root_and_rejects_unmanaged_home(tmp_path):
    root = tmp_path.resolve()
    owner = root / 'owner'
    owner.mkdir(mode=0o700)
    homes = ProviderAccountHomeManager(root/'unused', owner_root_resolver=lambda *_: owner)
    home = homes.ensure_home(tenant_id='tenant', owner_id='owner', account_id='acct_test', provider='codex')
    token = home/'codex/auth.json'
    token.write_text('synthetic token')
    token.chmod(0o644)
    homes.tighten_permissions(account_home=home)
    assert token.stat().st_mode & 0o777 == 0o600
    outside = root/'outside'
    outside.mkdir(mode=0o755)
    with pytest.raises(ControlPlaneError, match='verified provisioned'):
        homes.tighten_permissions(account_home=outside)
    assert outside.stat().st_mode & 0o777 == 0o755


def test_managed_native_ingress_without_guard_never_calls_subprocess(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    owner = root/'owner'
    owner.mkdir(mode=0o700)
    homes = ProviderAccountHomeManager(root/'unused', owner_root_resolver=lambda *_: owner)
    home = homes.ensure_home(tenant_id='tenant', owner_id='owner', account_id='acct_test', provider='codex')
    monkeypatch.setattr('subprocess.run', lambda *a, **k: pytest.fail('unguarded native run'))
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: pytest.fail('unguarded native spawn'))
    for method, purpose in ((homes.popen_native, 'setup'), (homes.run_native, 'verify'), (homes.run_native, 'logout'),
                            (homes.run_native, 'api-key-login'), (homes.run_native, 'api-key-verify')):
        with pytest.raises(ControlPlaneError, match='native quota launch guard'):
            method(['/synthetic/cli'], account_home=home, purpose=purpose, env={})


def test_managed_guard_capability_receives_private_typed_launch_and_stdin(tmp_path, monkeypatch):
    from workers_projects_runtime.native_api_keys import _prepare_native_key
    import subprocess
    root = tmp_path.resolve()
    owner = root/'owner'
    owner.mkdir(mode=0o700)
    seen = []
    from workers_projects_runtime.contained_account_launch import ContainedAccountLauncher
    class FixtureGuard(ContainedAccountLauncher):
        def __init__(self): pass
        def assert_quiescent(self, home): pass
        def run(self, request, **options):
            seen.append((request, options))
            return subprocess.CompletedProcess(request.command, 0, stdout='{"authMethod":"api_key","apiKeySource":"ANTHROPIC_API_KEY"}')
        def popen(self, request, **options):
            seen.append((request, options))
            from workers_projects_runtime.contained_account_launch import ContainedAccountProcess
            return ContainedAccountProcess(None, None, None, -1)
    homes = ProviderAccountHomeManager(root/'unused', owner_root_resolver=lambda *_: owner, native_launcher=FixtureGuard())
    home = homes.ensure_home(tenant_id='tenant', owner_id='owner', account_id='acct_test', provider='codex')
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.provider_setup_binary', lambda _: '/synthetic/cli')
    _prepare_native_key('codex', 'synthetic-private-key', home, homes)
    request, options = seen.pop()
    assert request.purpose == 'api-key-login' and request.account_home == home
    assert request.command[-2:] == ('login', '--with-api-key')
    assert options['input'] == 'synthetic-private-key\n'
    assert 'synthetic-private-key' not in repr(request) and 'synthetic-private-key' not in repr(request.command)
    _prepare_native_key('claude', 'synthetic-private-key', home, homes)
    request, options = seen.pop()
    assert request.purpose == 'api-key-verify'
    assert request.environment['ANTHROPIC_API_KEY'] == 'synthetic-private-key'
    assert 'synthetic-private-key' not in repr(request)
    from workers_projects_runtime.contained_account_launch import ContainedAccountProcess
    assert isinstance(homes.popen_native(['/synthetic/cli'], account_home=home, purpose='setup', env={}, pass_fds=(123,)), ContainedAccountProcess)
    assert seen.pop()[1]['pass_fds'] == (123,)
    for options in ({'env': {'LD_PRELOAD': 'invalid'}}, {'env': {'PYTHONPATH': 'invalid'}}, {'env': {'PYTHONHOME': 'invalid'}},
                    {'env': {}, 'shell': True}, {'env': {}, 'cwd': root}):
        with pytest.raises(ControlPlaneError):
            homes.run_native(['/synthetic/cli'], account_home=home, purpose='verify', **options)
    assert not seen
