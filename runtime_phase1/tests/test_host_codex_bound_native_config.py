"""A named native login must not hide the worker's already-authorized tools/policy."""
import hashlib
import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from workers_projects_runtime import native_input
from workers_projects_runtime.codex_native_warm import command_config
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager

SOURCE = '''developer_instructions = "Exact worker authority"
[mcp_servers.native]
command = "/synthetic/native-tool"
args = ["mcp"]
enabled_tools = ["read", "act"]
[plugins."selected-plugin@example"]
enabled = true
'''


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('WPR_CODEX_CLI_PERSONALITY', 'inherit')
    monkeypatch.setenv('GLASSHIVE_HOST_PLUGIN_DENYLIST', '')
    monkeypatch.setenv('WPR_HOST_PLUGIN_DENYLIST', '')
    monkeypatch.setenv('WPR_MODEL_HOST_CODEX_CLI', 'gpt-6.1-sol')
    owner_home = tmp_path / 'owner' / '.codex'
    owner_home.mkdir(parents=True, mode=0o700)
    owner_home.chmod(0o700)
    (owner_home / 'config.toml').write_text(SOURCE)
    (owner_home / 'config.toml').chmod(0o600)
    package = owner_home / 'plugins/cache/example/selected-plugin/1.0.0'
    (package / '.codex-plugin').mkdir(parents=True)
    (package / '.codex-plugin/plugin.json').write_text('{"name":"selected-plugin","version":"1.0.0"}')
    monkeypatch.setenv('CODEX_HOME', str(owner_home))
    monkeypatch.delenv('GLASSHIVE_HOST_CODEX_PLUGIN_CACHE', raising=False)
    monkeypatch.delenv('WPR_HOST_CODEX_PLUGIN_CACHE', raising=False)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / 'runtime'))
    account = tmp_path / 'account' / 'codex'
    account.mkdir(parents=True, mode=0o700)
    account.chmod(0o700)
    for name, text in [('config.toml', ''), ('auth.json', '{"synthetic_login":"same account"}'),
                       ('native-history.json', '{"thread":"retained"}')]:
        (account / name).write_text(text)
        (account / name).chmod(0o600)
    worker = {'worker_id': 'wrk_selected_config', 'owner_id': 'owner', 'profile': 'codex-cli',
              'execution_mode': 'host', 'model': 'gpt-6.1-sol',
              'bootstrap_bundle_json': json.dumps({'provider_account': {'policy': 'personal_required',
                  'account_id': 'acct_selected'}, 'env': {'WPR_CODEX_CLI_REASONING_EFFORT': 'high'}}),
              '_glasshive_provider_account_bound': True,
              '_glasshive_provider_account_env': {'CODEX_HOME': str(account)}}
    home = runtime._host_codex_home(worker)
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    (home / 'config.toml').write_text(SOURCE)
    (home / 'config.toml').chmod(0o600)
    info = RuntimeInfo(runtime='codex-cli', model='gpt-6.1-sol', gateway_url='', gateway_port=None,
                       gateway_token=None, session_key=None, state_dir=str(tmp_path),
                       workspace_dir=str(tmp_path), pid=None)
    return runtime, worker, account, home, info


def native_profile(command, account):
    assert command[1] == 'exec' and command[2] == '--profile'
    name = command[3]
    assert name.startswith('xperfect-') and len(name) == len('xperfect-') + 24
    return account / (name + '.config.toml')


def test_bound_native_login_preserves_worker_policy_model_effort_and_saved_history(configured):
    runtime, worker, account, home, info = configured
    auth = (account / 'auth.json').read_bytes()
    history = (account / 'native-history.json').read_bytes()
    command, env = runtime._build_command(worker, 'Synthetic job', info)
    profile = native_profile(command, account)
    assert profile.read_text() == SOURCE
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600 and not profile.is_symlink()
    assert env['CODEX_HOME'] == str(account)
    assert (account / 'auth.json').read_bytes() == auth
    assert (account / 'native-history.json').read_bytes() == history
    assert not (home / 'auth.json').exists()
    assert '-m' in command and command[command.index('-m') + 1] == 'gpt-6.1-sol'
    assert 'model_reasoning_effort="high"' in command
    assert 'Exact worker authority' not in ' '.join(command)
    original_mtime = profile.stat().st_mtime_ns
    runtime._build_command(worker, 'Same job', info)
    assert profile.stat().st_mtime_ns == original_mtime
    # Removed capabilities are refreshed from the owner on the next admitted launch.
    (home / 'config.toml').write_text('developer_instructions="Updated authority"\n')
    runtime._build_command(worker, 'Follow-up', info)
    assert 'mcp_servers' not in tomllib.loads(profile.read_text())


def test_resumed_profile_uses_first_class_thread_config_without_secrets_in_argv(configured, tmp_path, monkeypatch):
    runtime, worker, account, home, info = configured
    monkeypatch.setattr(runtime, '_resumable_codex_session_key', lambda _worker: 'saved-native-thread')
    command, env = runtime._build_command(worker, 'Follow-up', info)
    assert command[4] == 'resume' and command[-2] == 'saved-native-thread'
    assert native_profile(command, account).read_text() == SOURCE
    assert native_input.codex_app_launch(command) is None
    assert env['CODEX_HOME'] == str(account)
    assert 'model_reasoning_effort="high"' in command
    worker.update(_active_run_id='run-profile', _run_attempt_id='attempt-profile')
    monkeypatch.setattr(runtime, '_native_midstream_schema_supported', lambda: True)
    run_root = tmp_path / 'resume-control'
    run_root.mkdir()
    prepared = runtime._prepare_native_midstream_command(worker, command, run_root=run_root)
    context = native_input.read_object(run_root / 'native-input-context.json')
    launch = context['launch']
    assert launch['command'][1:3] == ['app-server', '--stdio']
    assert '--profile' not in prepared and 'Exact worker authority' not in ' '.join(prepared)
    assert launch['thread']['threadId'] == 'saved-native-thread'
    assert launch['thread']['config']['mcp_servers'] == tomllib.loads(SOURCE)['mcp_servers']
    assert launch['thread']['config']['plugins'] == tomllib.loads(SOURCE)['plugins']
    assert launch['thread']['model'] == 'gpt-6.1-sol' and launch['turn']['effort'] == 'high'
    assert launch['thread']['config']['approval_policy'] == 'never'
    assert launch['thread']['config']['sandbox_mode'] == launch['thread']['sandbox'] == 'danger-full-access'



def test_account_and_worker_profile_files_do_not_cross_scopes(configured, tmp_path):
    runtime, worker, account, home, info = configured
    command, _ = runtime._build_command(worker, 'First account', info)
    first = native_profile(command, account)
    first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
    second = tmp_path / 'other-account'
    second.mkdir(mode=0o700)
    (second / 'config.toml').write_text('')
    (second / 'config.toml').chmod(0o600)
    other_worker = {**worker, 'worker_id': 'wrk_other_config',
                    '_glasshive_provider_account_env': {'CODEX_HOME': str(second)}}
    other_home = runtime._host_codex_home(other_worker)
    other_home.mkdir(parents=True, mode=0o700)
    other_home.chmod(0o700)
    (other_home / 'config.toml').write_text('developer_instructions="Another worker"\n')
    (other_home / 'config.toml').chmod(0o600)
    other_command, env = runtime._build_command(other_worker, 'Other account', info)
    other_profile = native_profile(other_command, second)
    assert first.name != other_profile.name
    assert env['CODEX_HOME'] == str(second)
    assert hashlib.sha256(first.read_bytes()).hexdigest() == first_hash
    assert 'Another worker' in other_profile.read_text()
    assert not (second / first.name).exists() and not (account / other_profile.name).exists()


def test_rpc_profile_cannot_borrow_another_worker_or_unsafe_profile(configured, tmp_path, monkeypatch):
    runtime, worker, account, home, info = configured
    command, _ = runtime._build_command(worker, 'Synthetic job', info)
    worker.update(_active_run_id='run-isolation', _run_attempt_id='attempt-isolation')
    monkeypatch.setattr(runtime, '_native_midstream_schema_supported', lambda: True)
    run_root = tmp_path / 'isolated-control'
    run_root.mkdir()
    wrong = {**worker, 'worker_id': 'another-worker'}
    assert runtime._prepare_native_midstream_command(wrong, command, run_root=run_root) is command
    assert not (run_root / 'native-input-context.json').exists()
    native_profile(command, account).chmod(0o644)
    with pytest.raises(RuntimeErrorBase, match='unavailable or unsafe'):
        runtime._prepare_native_midstream_command(worker, command, run_root=run_root)
    assert not (run_root / 'native-input-context.json').exists()


def test_restricted_bound_native_policy_cannot_gain_ambient_mcp_or_plugins(configured):
    runtime, worker, account, home, info = configured
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['provider_capabilities'] = {'native_tools': False}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    source = runtime._host_codex_worker_config('', capability_bundle=bundle)
    (home / 'config.toml').write_text(source)
    command, _ = runtime._build_command(worker, 'Restricted work', info)
    policy = tomllib.loads(native_profile(command, account).read_text())
    assert policy.get('mcp_servers', {}) == {} and policy.get('plugins', {}) == {}
    assert '--ephemeral' in command
    (account / 'config.toml').write_text('[mcp_servers.unselected]\ncommand="/synthetic/not-authorized"\n')
    with pytest.raises(RuntimeErrorBase, match='unavailable or unsafe'):
        runtime._build_command(worker, 'Still restricted', info)


@pytest.mark.parametrize('unsafe', ['source_link', 'target_link', 'base_link', 'account_mode', 'source_mode', 'missing_source'])
def test_private_profile_refuses_unsafe_native_files_without_touching_auth(configured, tmp_path, unsafe):
    runtime, worker, account, home, info = configured
    auth = (account / 'auth.json').read_bytes()
    target = account / ('xperfect-' + hashlib.sha256(worker['worker_id'].encode()).hexdigest()[:24] + '.config.toml')
    victim = tmp_path / 'unrelated'
    victim.write_text('Unrelated private file')
    if unsafe == 'missing_source':
        (home / 'config.toml').unlink()
    elif unsafe == 'account_mode':
        account.chmod(0o755)
    elif unsafe == 'source_mode':
        (home / 'config.toml').chmod(0o644)
    else:
        path = home / 'config.toml' if unsafe == 'source_link' else account / 'config.toml' if unsafe == 'base_link' else target
        if path.exists(): path.unlink()
        path.symlink_to(victim)
    with pytest.raises(RuntimeErrorBase, match='unavailable or unsafe'):
        runtime._build_command(worker, 'Synthetic job', info)
    assert victim.read_text() == 'Unrelated private file'
    assert (account / 'auth.json').read_bytes() == auth


def test_generated_native_profile_remains_compatible_with_account_reseal_and_refresh(configured):
    runtime, worker, account, home, info = configured
    command, _ = runtime._build_command(worker, 'Synthetic job', info)
    profile = native_profile(command, account)
    auth = (account / 'auth.json').read_bytes()
    homes = ProviderAccountHomeManager(account.parent.parent)
    homes.tighten_permissions(account_home=account.parent)
    assert profile.read_text() == SOURCE
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert (account / 'auth.json').read_bytes() == auth


@pytest.mark.parametrize('mode', ['mission', 'conversation'])
def test_native_skill_catalog_survives_account_binding_and_retracts_only_derived_alias(configured, tmp_path, mode):
    runtime, worker, account, home, info = configured
    source = runtime._source_host_codex_home() / 'skills' / 'synthetic-notes'
    source.mkdir(parents=True)
    (source / 'SKILL.md').write_text('---\nname: synthetic-notes\ndescription: Synthetic notes\n---\nNotes.\n')
    full = json.loads(worker['bootstrap_bundle_json'])
    def write(bundle):
        if mode == 'conversation':
            runtime._write_conversation_runtime_files(worker, bundle)
        else:
            runtime._write_host_project_mcp_files(worker, tmp_path, bundle)
    write(full)
    alias = home.parent / '.agents/skills/native-codex'
    assert alias.is_symlink() and alias.resolve() == (home / 'skills').resolve()
    assert (alias / 'synthetic-notes/SKILL.md').read_text() == (source / 'SKILL.md').read_text()
    restricted = {**full, 'provider_capabilities': {'native_tools': False}}
    write(restricted)
    assert not alias.is_symlink() and not alias.exists()
    assert (source / 'SKILL.md').exists() and (home / 'skills').exists()
    policy = tomllib.loads((home / 'config.toml').read_text())
    assert policy['features']['plugins'] is False and policy['features']['hooks'] is False
    assert policy.get('mcp_servers', {}) == {}


@pytest.mark.parametrize('kind', ['local', 'unrelated_link'])
def test_native_skill_alias_retraction_preserves_unrelated_local_state(configured, tmp_path, kind):
    runtime, worker, account, home, info = configured
    alias = home.parent / '.agents/skills/native-codex'
    alias.parent.mkdir(parents=True)
    unrelated = tmp_path / 'local-skills'
    unrelated.mkdir()
    (unrelated / 'local').write_text('Preserve local data')
    if kind == 'local':
        alias.mkdir()
        (alias / 'local').write_text('Preserve local data')
    else:
        alias.symlink_to(unrelated, target_is_directory=True)
    source = runtime._source_host_codex_home() / 'skills/synthetic-notes'
    source.mkdir(parents=True)
    (source / 'SKILL.md').write_text('Synthetic skill')
    with pytest.raises(RuntimeErrorBase, match='existing local entry was preserved'):
        runtime._project_host_codex_capability_roots(home)
    runtime._remove_host_codex_skill_alias(home)
    assert (alias / 'local').read_text() == 'Preserve local data'
    assert not (alias / 'synthetic-notes').exists()


@pytest.mark.parametrize('parent', ['.agents', '.agents/skills'])
def test_native_skill_alias_cannot_create_or_remove_through_escaping_parent(configured, tmp_path, parent):
    runtime, worker, account, home, info = configured
    source = runtime._source_host_codex_home() / 'skills/synthetic-notes'
    source.mkdir(parents=True)
    (source / 'SKILL.md').write_text('Synthetic skill')
    outside = tmp_path / 'unrelated-local'
    outside.mkdir()
    parent_path = home.parent / parent
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    parent_path.symlink_to(outside, target_is_directory=True)
    leaf = outside / ('skills/native-codex' if parent == '.agents' else 'native-codex')
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.symlink_to(home / 'skills', target_is_directory=True)
    for operation in (runtime._project_host_codex_capability_roots, runtime._remove_host_codex_skill_alias):
        with pytest.raises(RuntimeErrorBase, match='unavailable or unsafe'):
            operation(home)
        assert leaf.is_symlink() and leaf.readlink() == home / 'skills'



def test_warm_ineligible_selected_account_keeps_profile_control_without_pool_claim(configured, tmp_path, monkeypatch):
    runtime, worker, account, home, info = configured
    worker.update(_active_run_id='run-profile', _run_attempt_id='attempt-profile')
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['runtime_performance'] = {'native_warm_servers': 1}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    monkeypatch.setattr(runtime, '_native_midstream_schema_supported', lambda: True)
    command, env = runtime._build_command(worker, 'Synthetic job', info)
    run_root = tmp_path / 'run'
    run_root.mkdir()
    prepared = runtime._prepare_native_warm_command(worker, command, run_root=run_root)
    assert prepared is not command
    context = native_input.read_object(run_root / 'native-input-context.json')
    assert context['launch']['thread']['config']['developer_instructions'] == 'Exact worker authority'
    assert '--profile' not in prepared
    assert not getattr(runtime, '_warm_leases', {})
    old_hash = hashlib.sha256(native_profile(command, account).read_bytes()).hexdigest()
    (home / 'config.toml').write_text('developer_instructions="New native authority"\n')
    second, _ = runtime._build_command(worker, 'Later job', info)
    assert hashlib.sha256(native_profile(second, account).read_bytes()).hexdigest() != old_hash
    assert not getattr(runtime, '_warm_leases', {})


def test_warm_startup_policy_rejection_retains_a_validated_native_control_relay(configured, tmp_path, monkeypatch):
    runtime, worker, account, home, info = configured
    worker.update(_active_run_id='run-control', _run_attempt_id='attempt-control')
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['runtime_performance'] = {'native_warm_servers': 1}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    monkeypatch.setattr(runtime, '_native_midstream_schema_supported', lambda: True)
    command = ['codex','exec','-c','approval_policy="never"',
               '-c','mcp_servers.synthetic.command="synthetic-child"','-']
    run_root = tmp_path / 'run-control'
    run_root.mkdir()
    prepared = runtime._prepare_native_warm_command(worker, command, run_root=run_root)
    assert prepared != command and prepared[1].endswith('native-input-relay.py')
    context = native_input.read_object(run_root / 'native-input-context.json')
    assert context['workerId'] == worker['worker_id'] and context['attemptId'] == 'attempt-control'
    assert 'warmConfigDigest' not in context and not getattr(runtime, '_warm_leases', {})



def test_bare_codex_mission_materializes_native_config_without_unrelated_prompt_or_plugin_flags(configured, tmp_path, monkeypatch):
    runtime, worker, account, home, info = configured
    (home / 'config.toml').unlink()
    monkeypatch.setattr(runtime, '_project_host_codex_capability_roots', lambda path: None)
    monkeypatch.setattr(runtime, '_host_codex_worker_config', lambda *args, **kwargs: SOURCE)
    workspace = tmp_path / 'bare-job'
    workspace.mkdir()
    runtime._write_host_project_mcp_files(worker, workspace, {})
    assert (home / 'config.toml').read_text() == SOURCE + '\n'
    command, env = runtime._build_command(worker, 'Bare job', info)
    assert native_profile(command, account).read_text() == SOURCE + '\n'
    assert env['CODEX_HOME'] == str(account)
