"""The same authorized native login must serve conversation and durable mission roles."""
import json
from pathlib import Path

import pytest

from workers_projects_runtime import profile_runtime as runtime_module
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime


@pytest.fixture
def mission(tmp_path, monkeypatch):
    monkeypatch.setenv('WPR_CLAUDE_CODE_ENABLE_CHROME', '1')
    monkeypatch.setenv('WPR_CLAUDE_CODE_EFFORT', 'default')
    monkeypatch.delenv('GLASSHIVE_ENTERPRISE_MODE', raising=False)
    monkeypatch.delenv('WPR_ENTERPRISE_MODE', raising=False)
    monkeypatch.delenv('CLAUDE_CODE_USE_BEDROCK', raising=False)
    monkeypatch.delenv('WPR_CLAUDE_CODE_USE_API_KEY', raising=False)
    monkeypatch.setattr(runtime_module, 'native_installed_owner_id', lambda: None)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / 'runtime'))
    worker = {'worker_id': 'wrk_mission_auth', 'profile': 'claude-code', 'runtime': 'claude-code',
              'execution_mode': 'host', 'model': 'opus', 'bootstrap_bundle_json': '{}'}
    workspace = tmp_path / 'mission'
    workspace.mkdir()
    info = runtime._host_runtime_info(worker)
    info.workspace_dir = str(workspace)
    isolated = {'HOME': str(tmp_path / 'isolated'), 'CLAUDE_CONFIG_DIR': str(tmp_path / 'isolated/.claude'),
                'CLAUDE_CODE_OAUTH_REFRESH_TOKEN': 'synthetic-unselected-refresh'}
    monkeypatch.setattr(runtime, '_host_env', lambda _worker: dict(isolated))
    monkeypatch.setattr(runtime, '_read_provider_session_key', lambda _worker: 'synthetic-session')
    return runtime, worker, info, isolated


@pytest.mark.parametrize('mcp_present', [False, True])
def test_host_mission_uses_existing_managed_login_and_only_its_mcp(mission, tmp_path, monkeypatch, mcp_present):
    runtime, worker, info, _ = mission
    mcp = Path(info.workspace_dir) / '.mcp.json'
    if mcp_present:
        runtime._write_host_claude_mcp_config(worker, mcp, {'provider_capabilities': {'native_tools': False}}, {'bound-broker': {'url': 'http://127.0.0.1:9000/mcp'}})
    selected = []
    def auth(env):
        assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env
        env['HOME'] = str(tmp_path / 'owner')
        env['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = ''
        selected.append(True)
        return 'owner_managed'
    monkeypatch.setattr(runtime, '_inject_private_subscription_auth', auth)
    command, env = runtime._build_command(worker, 'Prepare a note.', info)
    assert selected == [True]
    assert env['HOME'] == str(tmp_path / 'owner')
    assert env['CLAUDE_CONFIG_DIR'] == str(tmp_path / 'isolated/.claude')
    assert env['CLAUDE_SECURESTORAGE_CONFIG_DIR'] == ''
    assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env
    assert command[command.index('--setting-sources') + 1] == ''
    assert command[command.index('--mcp-config') + 1] == (str(mcp) if mcp_present else '{"mcpServers":{}}')
    assert '--strict-mcp-config' in command
    assert '--chrome' in command
    assert command[command.index('--model') + 1] == 'opus'
    assert '--effort' not in command
    assert command[command.index('--resume') + 1] == 'synthetic-session'
    assert command[command.index('--permission-mode') + 1] == 'bypassPermissions'


def test_host_mission_personal_required_never_discovers_owner_login(mission, monkeypatch):
    runtime, worker, info, _ = mission
    worker.update(bootstrap_bundle_json=json.dumps({'provider_account': {'policy': 'personal_required', 'account_id': 'acct_synthetic'}}),
                  _glasshive_provider_account_bound=True,
                  _glasshive_provider_account_env={'CLAUDE_CONFIG_DIR': '/synthetic/selected-account', 'CLAUDE_SECURESTORAGE_CONFIG_DIR': '/synthetic/selected-account'})
    monkeypatch.setattr(runtime, '_inject_private_subscription_auth', lambda _env: pytest.fail('must not read owner login'))
    command, env = runtime._build_command(worker, 'Prepare a note.', info)
    assert env['CLAUDE_CONFIG_DIR'] == '/synthetic/selected-account'
    assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env
    assert '--strict-mcp-config' in command


@pytest.mark.parametrize('authority', ['missing', 'access_token', 'api_key'])
def test_host_mission_enterprise_never_discovers_local_login(mission, monkeypatch, authority):
    runtime, worker, info, isolated = mission
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE', '1')
    if authority == 'access_token':
        isolated['CLAUDE_CODE_OAUTH_TOKEN'] = 'synthetic-server-access'
    if authority == 'api_key':
        isolated['ANTHROPIC_API_KEY'] = 'synthetic-server-api'
        monkeypatch.setenv('WPR_CLAUDE_CODE_USE_API_KEY', '1')
    monkeypatch.setattr(runtime, '_inject_private_subscription_auth', lambda _env: pytest.fail('must not read owner login'))
    if authority == 'missing':
        with pytest.raises(RuntimeErrorBase, match='server-owned'):
            runtime._build_command(worker, 'Prepare a note.', info)
    else:
        _, env = runtime._build_command(worker, 'Prepare a note.', info)
        assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env
        assert env['CLAUDE_CODE_OAUTH_TOKEN' if authority == 'access_token' else 'ANTHROPIC_API_KEY'].startswith('synthetic-server-')


def test_host_mission_explicit_api_key_keeps_selected_route(mission, monkeypatch):
    runtime, worker, info, isolated = mission
    isolated['ANTHROPIC_API_KEY'] = 'synthetic-api-key'
    monkeypatch.setenv('WPR_CLAUDE_CODE_USE_API_KEY', '1')
    monkeypatch.setattr(runtime, '_inject_private_subscription_auth', lambda _env: pytest.fail('must not replace selected API auth'))
    _, env = runtime._build_command(worker, 'Prepare a note.', info)
    assert env['ANTHROPIC_API_KEY'] == 'synthetic-api-key'
    assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env


def test_host_mission_bedrock_never_discovers_subscription_login(mission, monkeypatch):
    runtime, worker, info, isolated = mission
    isolated['CLAUDE_CODE_OAUTH_TOKEN'] = 'synthetic-unselected-access'
    monkeypatch.setenv('CLAUDE_CODE_USE_BEDROCK', '1')
    monkeypatch.setattr(runtime, '_inject_private_subscription_auth', lambda _env: pytest.fail('must not change Bedrock'))
    _, env = runtime._build_command(worker, 'Prepare a note.', info)
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in env
    assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env


def test_native_filter_command_preserves_explicit_settings_and_session(mission, monkeypatch):
    runtime, worker, info, isolated = mission
    isolated['CLAUDE_CODE_OAUTH_TOKEN'] = 'synthetic-access'
    original = {'disableAllHooks': True, 'hooks': {'PreToolUse': [{'matcher': 'Read', 'hooks': [{'type': 'command', 'command': 'true'}]}]},
                'enabledPlugins': {'synthetic@selected': True}, 'customInstructions': 'selected setting'}
    bundle = {'claude_settings_local': original}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    monkeypatch.setattr(runtime, '_host_codex_worker_config', lambda _: '[mcp_servers.cua_repl]\ncommand="/synthetic/cua"\nenabled_tools=["js","js_reset"]\n')
    monkeypatch.setattr(runtime, '_host_plugin_denylist', lambda: {'denied@policy'})
    runtime._write_host_claude_mcp_config(worker, Path(info.workspace_dir) / '.mcp.json', bundle, {})
    command, env = runtime._build_command(worker, 'Inspect the note.', info)
    settings = json.loads(command[command.index('--settings') + 1])
    assert settings['hooks']['PreToolUse'][0] == original['hooks']['PreToolUse'][0]
    assert len(settings['hooks']['PreToolUse']) == 1
    assert settings['disableAllHooks'] is True
    assert settings['enabledPlugins'] == {'synthetic@selected': True, 'denied@policy': False}
    assert settings['customInstructions'] == 'selected setting'
    assert command[command.index('--model') + 1] == 'opus'
    assert command[command.index('--resume') + 1] == 'synthetic-session'
    assert '--chrome' in command and '--strict-mcp-config' in command
    assert json.loads(worker['bootstrap_bundle_json']) == bundle

@pytest.mark.parametrize('installed', [False, True])
def test_owner_login_keeps_retained_session_directory(mission, monkeypatch, tmp_path, installed):
    runtime, worker, info, isolated = mission
    owner = tmp_path/'owner'; owner.mkdir()
    monkeypatch.setenv('HOME', str(owner))
    monkeypatch.setattr(runtime_module, 'native_installed_owner_id', lambda: 'synthetic-owner' if installed else None)
    monkeypatch.setattr(runtime_module, '_usable_explicit_claude_oauth_token', lambda: None)
    monkeypatch.setattr(runtime_module, '_read_claude_keychain_oauth', lambda: {})
    probes=[]
    def logged_in(_binary, *, child_env):
        probes.append(dict(child_env))
        return child_env.get('CLAUDE_SECURESTORAGE_CONFIG_DIR') == '' and child_env.get('HOME') == str(owner)
    monkeypatch.setattr(runtime_module, '_claude_cli_managed_auth_available', logged_in)
    env=dict(isolated)
    assert runtime._inject_private_subscription_auth(env) == 'owner_managed'
    assert env['CLAUDE_CONFIG_DIR'] == isolated['CLAUDE_CONFIG_DIR']
    assert env['HOME'] == str(owner)
    assert env['CLAUDE_SECURESTORAGE_CONFIG_DIR'] == ''
    assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env
    expected_probe = dict(env)
    if installed:
        expected_probe.pop('CLAUDE_CONFIG_DIR', None)
    assert probes[-1] == expected_probe


def test_unavailable_split_login_does_not_relocate_session(mission, monkeypatch, tmp_path):
    runtime, _, _, isolated = mission
    owner=tmp_path/'owner';owner.mkdir();monkeypatch.setenv('HOME',str(owner))
    monkeypatch.setattr(runtime_module, '_usable_explicit_claude_oauth_token', lambda: None)
    monkeypatch.setattr(runtime_module, '_read_claude_keychain_oauth', lambda: {})
    monkeypatch.setattr(runtime_module, '_claude_cli_managed_auth_available', lambda *_args, **_kwargs: False)
    env=dict(isolated)
    with pytest.raises(RuntimeErrorBase, match='authentication is unavailable'):
        runtime._inject_private_subscription_auth(env)
    assert env['CLAUDE_CONFIG_DIR'] == isolated['CLAUDE_CONFIG_DIR']
    assert env['HOME'] == isolated['HOME']
