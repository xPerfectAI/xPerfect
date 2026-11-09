from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import workers_projects_runtime.profile_runtime as module
from workers_projects_runtime.openclaw_runtime import RuntimeDependencyMissingError, RuntimeErrorBase


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('VIVENTIUM_NATIVE_FIRST_ADMIN_STATE', raising=False)
    monkeypatch.setenv('GLASSHIVE_HOST_NATIVE_WEB_ACCESS', 'inherit')
    monkeypatch.setenv('WPR_CLAUDE_CODE_ENABLE_CHROME', '1')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'synthetic-access-token')
    instance = module.HostClaudeCodeRuntime(base_dir=str(tmp_path / 'private-state'))
    monkeypatch.setattr(instance, '_help_text', lambda: (
        '--tools --permission-mode dontAsk --no-chrome --strict-mcp-config '
        '--setting-sources --json-schema --effort --safe-mode '
        '--disable-slash-commands --no-session-persistence --system-prompt-snapshot'))
    monkeypatch.setattr(instance, '_inject_private_subscription_auth', lambda env: 'projected_access_token')
    monkeypatch.setattr(instance, '_host_plugin_denylist', lambda: ())
    monkeypatch.setattr(instance, '_project_host_claude_capability_roots', lambda _: pytest.fail('Inherited Claude capabilities'))
    return instance


def worker(tmp_path, *, native_tools=False, access_mode='full'):
    life = tmp_path / 'Life'
    life.mkdir(exist_ok=True)
    (life / 'CLAUDE.md').write_text('Unrelated private project instructions')
    return {
        'worker_id': 'synthetic-claude-restricted', 'profile': 'claude-code',
        'execution_mode': 'host', 'trusted_run_lane': 'conversation',
        'workspace_root': str(life), 'model': 'claude-opus-5-5',
        'bootstrap_bundle_json': json.dumps({
            'run_mode': 'conversation', 'provider_model': 'claude-opus-5-5',
            'access_mode': access_mode, 'provider_capabilities': {'native_tools': native_tools},
            'developer_instructions': 'Use the admitted synthetic context.',
            'claude_project_mcp': {'unadmitted': {'command': 'unadmitted-tool'}},
            'claude_settings_local': {'autoMemoryEnabled': True, 'hooks': {'SessionStart': []},
                'env': {'CLAUDE_CODE_SAFE_MODE': '0'}, 'permissions': {'allow': ['Bash']}},
            'env': {'WPR_CLAUDE_CODE_EFFORT': 'high'},
        }),
    }


@pytest.mark.parametrize('access_mode', ['full', 'read_only', 'workspace'])
def test_restricted_claude_zero_tools_fresh_context_and_exact_model(runtime, tmp_path, access_mode):
    candidate = worker(tmp_path, access_mode=access_mode)
    authority = runtime._bootstrap_bundle_for_worker(candidate)
    runtime._write_conversation_runtime_files(candidate, authority)
    runtime._write_session_key(candidate['worker_id'], 'previous-full-authority-session')
    # Private native context selectors must not reintroduce settings, MCP or instructions.
    candidate['_native_context_placement'] = {'unadmitted': True}
    info = runtime._host_runtime_info(candidate)
    command, env = runtime._build_command(candidate, 'Synthetic.', info)
    assert command[command.index('--tools') + 1] == ''
    assert command[command.index('--model') + 1] == 'claude-opus-5-5'
    assert command[command.index('--effort') + 1] == 'high'
    assert command[command.index('--permission-mode') + 1] == 'dontAsk'
    assert {'--safe-mode', '--no-chrome', '--disable-slash-commands', '--no-session-persistence', '--strict-mcp-config'} <= set(command)
    assert '--resume' not in command and '--chrome' not in command and '--bare' not in command
    assert command[command.index('--setting-sources') + 1] == ''
    assert command[command.index('--system-prompt-snapshot') + 1] == 'off'
    settings = json.loads(command[command.index('--settings') + 1])
    assert settings == {'autoMemoryEnabled': False, 'disableAllHooks': True, 'permissions': {'defaultMode': 'dontAsk'}}
    mcp = Path(command[command.index('--mcp-config') + 1])
    assert json.loads(mcp.read_text()) == {'mcpServers': {}}
    assert Path(command[command.index('--append-system-prompt-file') + 1]).read_text() == authority['developer_instructions']
    assert info.workspace_dir == str(runtime._state_dir(candidate['worker_id']) / 'conversation-workspace')
    assert list(Path(info.workspace_dir).iterdir()) == []
    assert env['CLAUDE_CONFIG_DIR'].startswith(str(tmp_path / 'private-state'))
    assert 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN' not in env


@pytest.mark.parametrize('missing', ['--tools', '--safe-mode', '--disable-slash-commands', '--no-session-persistence'])
def test_restricted_claude_requires_native_enforcement(runtime, tmp_path, monkeypatch, missing):
    advertised = runtime._help_text()
    monkeypatch.setattr(runtime, '_help_text', lambda: advertised.replace(missing, ''))
    candidate = worker(tmp_path)
    with pytest.raises(RuntimeDependencyMissingError, match='zero-tool controls'):
        runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))


def test_restricted_claude_refuses_changed_mcp_seal(runtime, tmp_path):
    candidate = worker(tmp_path)
    runtime._write_conversation_runtime_files(candidate, runtime._bootstrap_bundle_for_worker(candidate))
    mcp = runtime._state_dir(candidate['worker_id']) / 'conversation-mcp.json'
    mcp.write_text(json.dumps({'mcpServers': {'unadmitted': {'command': 'unadmitted'}}}))
    with pytest.raises(RuntimeErrorBase, match='unavailable or stale'):
        runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))


@pytest.mark.parametrize('value', [True, None, 'false', 0])
def test_only_typed_false_restricts_claude(runtime, tmp_path, monkeypatch, value):
    candidate = worker(tmp_path, native_tools=value)
    authority = runtime._bootstrap_bundle_for_worker(candidate)
    authority.pop('developer_instructions')
    authority.pop('claude_project_mcp')
    authority.pop('claude_settings_local')
    candidate['bootstrap_bundle_json'] = json.dumps(authority)
    monkeypatch.setattr(runtime, '_read_session_key', lambda _: 'existing-session')
    command, _ = runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))
    assert '--tools' not in command and '--safe-mode' not in command and '--no-session-persistence' not in command
    assert '--chrome' in command and '--resume' in command
    assert command[command.index('--permission-mode') + 1] == 'bypassPermissions'
    assert runtime._host_workspace_dir(candidate) == tmp_path / 'Life'


def test_restricted_claude_does_not_materialize_private_native_context(runtime, tmp_path):
    candidate = worker(tmp_path)
    authority = runtime._bootstrap_bundle_for_worker(candidate)
    runtime._write_conversation_runtime_files(candidate, authority)
    unadmitted = tmp_path / 'unadmitted-native-context'
    candidate['_native_context_placement'] = {
        'home_dir': str(unadmitted / 'home'),
        'workspace_dir': str(tmp_path / 'Life'),
        'private_project_dir': str(unadmitted / 'project'),
        'host_native': True,
    }
    command, _ = runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))
    assert not unadmitted.exists()
    assert Path(command[command.index('--append-system-prompt-file') + 1]).read_text() == authority['developer_instructions']


def test_restricted_claude_does_not_apply_preexisting_native_selectors(runtime, tmp_path):
    candidate = worker(tmp_path)
    authority = runtime._bootstrap_bundle_for_worker(candidate)
    runtime._write_conversation_runtime_files(candidate, authority)
    candidate['_native_context_placement'] = {'unadmitted': True}
    candidate['_native_context_selectors'] = {
        'settings_data': {'autoMemoryDirectory': str(tmp_path / 'unadmitted-memory')},
        'mcp_config': str(tmp_path / 'unadmitted-mcp.json'),
        'claude_instructions': str(tmp_path / 'unadmitted-instructions.md'),
    }
    command, env = runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))
    assert Path(command[command.index('--mcp-config') + 1]).name == 'conversation-mcp.json'
    assert Path(command[command.index('--append-system-prompt-file') + 1]).read_text() == authority['developer_instructions']
    assert json.loads(command[command.index('--settings') + 1]) == {
        'autoMemoryEnabled': False, 'disableAllHooks': True, 'permissions': {'defaultMode': 'dontAsk'}}
    assert env['CLAUDE_CONFIG_DIR'].startswith(str(tmp_path / 'private-state'))


def admitted_broker(candidate):
    bundle = json.loads(candidate['bootstrap_bundle_json'])
    expiry = int(time.time()) + 3600
    broker = {'version': 1, 'name': 'app', 'url': 'http://127.0.0.1:12345/mcp',
              'grant_id': 'synthetic-current-grant', 'grant_expires_at': expiry}
    broker['tool_catalog'] = {'version': 1, 'grant_id': broker['grant_id'],
        'expires_at': expiry, 'server_name': broker['name'], 'broker_url': broker['url'],
        'tools': [{'name': 'hand_off_to', 'description': 'Inherit current original input.',
                   'inputSchema': {'type': 'object', 'properties': {}}}]}
    bundle['glasshive_capability_broker'] = broker
    bundle['env']['GLASSHIVE_CAPABILITY_BROKER_TOKEN'] = 'synthetic-access-only-grant'
    bundle['claude_project_mcp']['app'] = {'type': 'http', 'url': broker['url'],
        'headers': {'Authorization': 'Bearer synthetic-access-only-grant'}}
    candidate['bootstrap_bundle_json'] = json.dumps(bundle)
    return bundle


def test_restricted_claude_keeps_only_its_admitted_broker(runtime, tmp_path):
    candidate = worker(tmp_path)
    bundle = admitted_broker(candidate)
    runtime._write_conversation_runtime_files(candidate, bundle)
    command, _ = runtime._build_command(candidate, 'Use the original input.', runtime._host_runtime_info(candidate))
    payload = json.loads(Path(command[command.index('--mcp-config') + 1]).read_text())
    assert set(payload['mcpServers']) == {'app'}
    assert payload['mcpServers']['app']['headers']['Authorization'] == 'Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}'
    assert '--safe-mode' not in command
    assert command[command.index('--tools') + 1] == ''
    assert '--no-chrome' in command and '--disable-slash-commands' in command
    settings = json.loads(command[command.index('--settings') + 1])
    assert settings['permissions'] == {'defaultMode': 'dontAsk', 'allow': ['mcp__app__hand_off_to']}
    assert settings['disableAllHooks'] and not settings['autoMemoryEnabled']
    assert settings['claudeMdExcludes'] == ['**']


@pytest.mark.parametrize('defect', ['expired', 'wrong_url', 'missing_token'])
def test_restricted_claude_refuses_an_invalid_declared_broker(runtime, tmp_path, defect):
    candidate = worker(tmp_path)
    bundle = admitted_broker(candidate)
    if defect == 'expired':
        bundle['glasshive_capability_broker']['grant_expires_at'] = 1
    elif defect == 'wrong_url':
        bundle['claude_project_mcp']['app']['url'] = 'http://127.0.0.1:23456/other'
    else:
        bundle['env'].pop('GLASSHIVE_CAPABILITY_BROKER_TOKEN')
    candidate['bootstrap_bundle_json'] = json.dumps(bundle)
    with pytest.raises(RuntimeErrorBase, match='grant is unavailable or stale'):
        runtime._write_conversation_runtime_files(candidate, bundle)
        runtime._build_command(candidate, 'Current original input.', runtime._host_runtime_info(candidate))
