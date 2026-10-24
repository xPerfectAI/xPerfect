"""Selected native packages keep code assets, never login/data or ambient grants."""
import json
import os
import stat
from pathlib import Path

import pytest

from workers_projects_runtime.native_plugin_assets import (
    AccountAssets, NativeAssetError, active_version, project_selected,
)
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager
from workers_projects_runtime.control_plane import ControlPlaneError


@pytest.fixture
def native_assets(tmp_path, monkeypatch):
    owner = tmp_path / 'owner'
    owner.mkdir(mode=0o700)
    (owner / 'config.toml').write_text('[plugins."selected@example"]\nenabled=true\n[plugins."unused@example"]\nenabled=false\n')
    (owner / 'config.toml').chmod(0o600)
    account_home = tmp_path / 'account'
    account_home.mkdir(mode=0o700)
    account = account_home / 'codex'
    account.mkdir(mode=0o700)
    (account / 'auth.json').write_text('{"synthetic_login":"unchanged"}')
    (account / 'auth.json').chmod(0o600)
    monkeypatch.setenv('CODEX_HOME', str(owner))
    monkeypatch.delenv('GLASSHIVE_HOST_CODEX_PLUGIN_CACHE', raising=False)
    monkeypatch.delenv('WPR_HOST_CODEX_PLUGIN_CACHE', raising=False)
    cache = owner / 'plugins/cache'
    def package(name='selected', version='1.0.0'):
        root = cache / 'example' / name / version
        (root / '.codex-plugin').mkdir(parents=True)
        (root / '.codex-plugin/plugin.json').write_text(json.dumps({'name':name,'version':version,
            'skills':'./skills','hooks':'./hooks/hooks.json'}))
        (root / 'skills/selected-skill').mkdir(parents=True)
        (root / 'skills/selected-skill/SKILL.md').write_text('---\nname: selected-skill\ndescription: Synthetic local skill\n---\nUse the supplied note.\n')
        (root / 'hooks').mkdir()
        (root / 'hooks/hooks.json').write_text('{"hooks":{}}')
        (root / 'scripts').mkdir()
        (root / 'scripts/native-helper').write_text('#!/bin/sh\nprintf synthetic\n')
        (root / 'scripts/native-helper').chmod(0o755)
        return root
    selected = package()
    unused = package('unused')
    config = {'plugins':{'selected@example':{'enabled':True},'unused@example':{'enabled':False}}}
    return owner, account_home, account, cache, selected, config, package


def project(values):
    owner, account_home, account, cache, source, config, _ = values
    return project_selected(config, account, owner_home=owner, cache=cache)


def seal(values):
    owner, account_home, account, cache, source, config, _ = values
    ProviderAccountHomeManager(account_home.parent).tighten_permissions(account_home=account_home)


def test_only_selected_package_assets_are_private_executable_and_resealable(native_assets):
    owner, account_home, account, cache, source, config, _ = native_assets
    (owner / 'auth.json').write_text('must not copy')
    (owner / 'plugins/data').mkdir()
    (owner / 'plugins/data/connector.json').write_text('must not copy')
    original_auth = (account / 'auth.json').read_bytes()
    assert project(native_assets) == [{'plugin':'selected@example','version':'1.0.0'}]
    target = account / 'plugins/cache/example/selected/1.0.0'
    assert (target / 'skills/selected-skill/SKILL.md').read_bytes() == (source / 'skills/selected-skill/SKILL.md').read_bytes()
    assert stat.S_IMODE((target / 'scripts/native-helper').stat().st_mode) == 0o700
    assert stat.S_IMODE((target / 'hooks/hooks.json').stat().st_mode) == 0o600
    assert not (account / 'plugins/cache/example/unused').exists()
    assert not (account / 'plugins/data').exists()
    assert (account / 'auth.json').read_bytes() == original_auth
    seal(native_assets)  # Account status/refresh has no active-worker argument.
    assert stat.S_IMODE((target / 'scripts/native-helper').stat().st_mode) == 0o700
    assert project(native_assets) == [{'plugin':'selected@example','version':'1.0.0'}]
    assert (account / 'auth.json').read_bytes() == original_auth


@pytest.mark.parametrize('version', ['local','2.0.0'])
def test_native_target_active_version_cannot_override_selected_source(native_assets, version):
    owner, _, account, cache, _, config, _ = native_assets
    other = account / 'plugins/cache/example/selected' / version
    other.mkdir(parents=True)
    for directory in (account / 'plugins', account / 'plugins/cache', account / 'plugins/cache/example', other.parent, other):
        directory.chmod(0o700)
    # The rejection does not remove or replace a pre-existing local cache.
    with pytest.raises(NativeAssetError, match='override|different'):
        project(native_assets)
    assert other.is_dir()
    assert not (other.parent / '1.0.0').exists()


def test_changed_source_active_version_is_pinned_before_reseal_and_prior_assets_are_kept(native_assets):
    owner, _, account, cache, _, config, package = native_assets
    project(native_assets)
    package(version='1.1.0')
    assert project(native_assets) == [{'plugin':'selected@example','version':'1.1.0'}]
    assert active_version(account / 'plugins/cache/example/selected') == '1.1.0'
    assert (account / 'plugins/cache/example/selected/1.0.0').exists()
    seal(native_assets)


@pytest.mark.parametrize('tamper', ['content','extra','source_link','source_group_write','ancestor_group_write','target_link','target_override'])
def test_source_escape_tamper_and_conflicting_code_are_rejected(native_assets, tmp_path, tamper):
    owner, _, account, cache, source, config, _ = native_assets
    project(native_assets)
    target = account / 'plugins/cache/example/selected/1.0.0'
    if tamper == 'content':
        (target / 'scripts/native-helper').write_text('not the approved package')
    elif tamper == 'extra':
        (target / 'unrelated-private-file').write_text('do not overwrite')
    elif tamper == 'source_link':
        (source / 'link').symlink_to(tmp_path)
    elif tamper == 'source_group_write':
        (source / 'scripts/native-helper').chmod(0o775)
    elif tamper == 'ancestor_group_write':
        (cache.parent).chmod(0o775)
    elif tamper == 'target_link':
        (target / 'scripts/native-helper').unlink()
        (target / 'scripts/native-helper').symlink_to(source / 'scripts/native-helper')
    else:
        (target.parent / 'local').mkdir(mode=0o700)
    with pytest.raises((NativeAssetError, OSError)):
        project(native_assets)
    if tamper not in {'extra'}:
        with pytest.raises((ControlPlaneError, OSError, ValueError)):
            seal(native_assets)


def test_restricted_worker_projects_no_package_even_when_another_worker_cached_it(native_assets, monkeypatch):
    owner, _, account, cache, source, config, _ = native_assets
    project(native_assets)
    restricted = {'features':{'plugins':False,'remote_plugin':False,'hooks':False},'plugins':{'selected@example':{'enabled':True}}}
    monkeypatch.setattr('workers_projects_runtime.native_plugin_assets._package', lambda *a, **k: pytest.fail('restricted worker read or projected native packages'))
    assert project_selected(restricted, account, owner_home=owner, cache=cache) == []
    assert (account / 'plugins/cache/example/selected/1.0.0').is_dir()


def test_new_unselected_plugin_needs_native_authority_and_does_not_modify_cache(native_assets):
    owner, _, account, cache, source, config, _ = native_assets
    config['plugins']['unused@example']['enabled'] = True
    with pytest.raises(NativeAssetError, match='native approval'):
        project(native_assets)
    assert not (account / 'plugins/cache').exists()


def test_native_version_order_uses_local_then_semver_ignores_symlink_alias(native_assets):
    _, _, _, _, source, _, package = native_assets
    package(version='1.10.0');package(version='1.9.0')
    base = source.parent
    (base / 'latest').symlink_to(base / '1.9.0')
    assert active_version(base) == '1.10.0'
    package(version='local')
    assert active_version(base) == 'local'



def test_two_workers_keep_distinct_selections_and_refresh_without_active_worker(native_assets):
    owner, _, account, cache, source, config, _ = native_assets
    (owner / 'config.toml').write_text('[plugins."selected@example"]\nenabled=true\n[plugins."unused@example"]\nenabled=true\n')
    first = {'plugins':{'selected@example':{'enabled':True}}}
    second = {'plugins':{'unused@example':{'enabled':True}}}
    assert project_selected(first, account, owner_home=owner, cache=cache) == [{'plugin':'selected@example','version':'1.0.0'}]
    assert project_selected(second, account, owner_home=owner, cache=cache) == [{'plugin':'unused@example','version':'1.0.0'}]
    assert set(first['plugins']) == {'selected@example'} and set(second['plugins']) == {'unused@example'}
    seal(native_assets)
    assert (account / 'plugins/cache/example/selected/1.0.0/scripts/native-helper').stat().st_mode & 0o100
    assert (account / 'plugins/cache/example/unused/1.0.0/scripts/native-helper').stat().st_mode & 0o100


def test_content_tamper_cannot_reuse_a_cached_digest_by_restoring_mtime(native_assets):
    _, _, account, _, _, _, _ = native_assets
    project(native_assets)
    path = account / 'plugins/cache/example/selected/1.0.0/scripts/native-helper'
    before = path.stat()
    original = path.read_bytes()
    path.write_bytes(b'X' * len(original))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(NativeAssetError):
        project(native_assets)
    with pytest.raises(ControlPlaneError):
        seal(native_assets)


def test_native_total_semver_order_includes_build_metadata_and_u64_limit(native_assets):
    _, _, _, _, source, _, package = native_assets
    package(version='2.0.0+first');package(version='2.0.0+second')
    assert active_version(source.parent) == '2.0.0+second'
    package(version='2.0.0+demo.90');package(version='2.0.0+demo.090')
    # Native BuildMetadata uses ASCII for nonnumeric components.
    assert active_version(source.parent) == '2.0.0+second'
    from workers_projects_runtime.native_plugin_assets import _compare
    assert _compare('2.0.0+demo.90', '2.0.0+demo.090') < 0
    assert _compare('2.0.0+demo.090', '2.0.0+demo.200') < 0
    assert _compare('2.0.0+demo.200', '2.0.0+demo.1a0') < 0
    assert _compare('2.0.0', '2.0.0+first') < 0
    assert _compare('18446744073709551616.0.0', '9.0.0') < 0
