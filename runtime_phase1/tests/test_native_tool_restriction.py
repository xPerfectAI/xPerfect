from __future__ import annotations

import json
import tomllib
from types import SimpleNamespace

import pytest

import workers_projects_runtime.profile_runtime as runtime_module
from workers_projects_runtime.conversation_provider import ChatCompletionRequest, ConversationProvider
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase


def bundle(native_tools=False):
    return {
        'run_mode': 'conversation', 'provider_model': 'gpt-5.6-luna', 'access_mode': 'full',
        'provider_capabilities': {'native_tools': native_tools},
        'developer_instructions': 'Apply the admitted synthetic memory decision.',
        'glasshive_capability_broker': {
            'version': 1, 'name': 'glasshive-user-capabilities',
            'url': 'http://127.0.0.1:3080/api/viventium/glasshive/capabilities/mcp',
        },
        'env': {'GLASSHIVE_CAPABILITY_BROKER_TOKEN': 'synthetic-grant',
                'WPR_CODEX_CLI_REASONING_EFFORT': 'medium'},
        'codex_config_append': '[mcp_servers.unrelated]\ncommand="unrelated-tool"',
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('VIVENTIUM_NATIVE_FIRST_ADMIN_STATE', raising=False)
    monkeypatch.setenv('WPR_HOST_CODEX_CONVERSATION_PROJECT_INSTRUCTIONS', 'inherit')
    monkeypatch.setenv('WPR_CODEX_CLI_IGNORE_USER_CONFIG', '1')
    source_home = tmp_path / 'owner-codex'
    source_home.mkdir()
    (source_home / 'config.toml').write_text(
        'developer_instructions="Owner instructions"\n'
        '[features]\napps=true\nplugins=true\nshell_tool=true\n'
        '[mcp_servers.computer-use]\ncommand="owner-computer"\n'
        '[mcp_servers.unrelated]\ncommand="owner-unrelated"\n')
    monkeypatch.setenv('CODEX_HOME', str(source_home))
    instance = runtime_module.HostCodexCliRuntime(base_dir=str(tmp_path / 'private-state'))
    monkeypatch.setattr(instance, '_copy_host_codex_auth', lambda _: None)
    monkeypatch.setattr(instance, '_host_codex_native_mcp_allowlist', lambda: {'computer-use'})
    monkeypatch.setattr(instance, '_host_codex_bundled_mcp_config', lambda _names, _source: '')
    monkeypatch.setattr(instance, '_host_codex_known_native_mcp_config', lambda _: '')
    monkeypatch.setattr(instance, '_host_plugin_denylist', lambda: ())
    monkeypatch.setattr(instance, '_host_codex_personality', lambda: 'inherit')
    return instance


def worker(tmp_path, authority):
    life = tmp_path / 'Life'
    life.mkdir(exist_ok=True)
    (life / 'AGENTS.md').write_text('Unrelated project instructions')
    return {'worker_id': 'synthetic-writer', 'profile': 'codex-cli', 'execution_mode': 'host',
            'model': 'gpt-5.6-luna',
            'trusted_run_lane': 'conversation', 'workspace_root': str(life),
            'bootstrap_bundle_json': json.dumps(authority)}


@pytest.mark.parametrize('value,expected', [(False, False), (True, True), (None, True), ('false', True), (0, True)])
def test_provider_preserves_only_typed_false(value, expected):
    payload = ChatCompletionRequest(model='synthetic', messages=[{'role': 'user', 'content': 'Remember the synthetic fact.'}],
        metadata={'bootstrap_bundle': bundle(value)})
    model = SimpleNamespace(harness_profile='codex-cli', native_model='gpt-5.6-luna')
    result = ConversationProvider._native_bundle(None, payload, model, 'medium')
    assert result['provider_capabilities']['native_tools'] is expected
    assert result['provider_model'] == 'gpt-5.6-luna'
    assert result['env']['WPR_CODEX_CLI_REASONING_EFFORT'] == 'medium'
    if expected is False:
        assert result['env']['GLASSHIVE_PROVIDER_SESSION_MODE'] == 'stateless'


def test_unsupported_harness_restriction_fails_closed():
    from fastapi import HTTPException
    payload = ChatCompletionRequest(model='synthetic', messages=[{'role': 'user', 'content': 'Synthetic.'}],
        metadata={'bootstrap_bundle': bundle()})
    with pytest.raises(HTTPException, match='unsupported'):
        ConversationProvider._native_bundle(None, payload, SimpleNamespace(harness_profile='claude-code'), 'medium')


def test_sealed_config_keeps_only_signed_broker_and_developer_authority(runtime, tmp_path, monkeypatch):
    authority = bundle()
    candidate = worker(tmp_path, authority)
    monkeypatch.setattr(runtime, '_project_host_codex_capability_roots', lambda _: pytest.fail('Inherited capability roots'))
    runtime._write_conversation_runtime_files(candidate, authority)
    config = tomllib.loads((runtime._host_codex_home(candidate) / 'config.toml').read_text())
    assert config['developer_instructions'] == authority['developer_instructions']
    assert config['mcp_servers'] == {'glasshive-user-capabilities': {
        'url': authority['glasshive_capability_broker']['url'],
        'default_tools_approval_mode': 'approve',
        'bearer_token_env_var': 'GLASSHIVE_CAPABILITY_BROKER_TOKEN'}}
    assert config['sandbox_mode'] == 'read-only'
    assert config['approval_policy'] == 'never'
    assert config['web_search'] == 'disabled'
    assert config['project_doc_max_bytes'] == 0
    assert config['features']['code_mode_host'] is True
    assert all(config['features'][name] is False for name in runtime_module._CODEX_RESTRICTED_NATIVE_FEATURES)


def test_launch_uses_fresh_private_workspace_and_preserves_model_effort(runtime, tmp_path):
    authority = bundle()
    candidate = worker(tmp_path, authority)
    runtime._write_conversation_runtime_files(candidate, authority)
    runtime._write_session_key(candidate['worker_id'], 'existing-full-authority-session')
    command, env = runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))
    assert command[1:3] == ['exec', '--json']
    assert command[command.index('-m') + 1] == 'gpt-5.6-luna'
    assert 'model_reasoning_effort="medium"' in command
    assert command[command.index('-s') + 1] == 'read-only'
    assert 'approval_policy="never"' in command
    assert '--ignore-rules' in command
    assert '--strict-config' in command
    assert ['--enable', 'code_mode_host'] in [command[i:i + 2] for i in range(len(command) - 1)]
    assert '--ignore-user-config' not in command
    assert '--add-dir' not in command
    assert '--dangerously-bypass-approvals-and-sandbox' not in command
    assert '--full-auto' not in command
    assert command[command.index('-C') + 1] == str(runtime._state_dir(candidate['worker_id']) / 'conversation-workspace')
    assert env['CODEX_HOME'] == str(runtime._host_codex_home(candidate))


def test_changed_seal_refuses_launch(runtime, tmp_path):
    authority = bundle()
    candidate = worker(tmp_path, authority)
    runtime._write_conversation_runtime_files(candidate, authority)
    config = runtime._host_codex_home(candidate) / 'config.toml'
    config.write_text(config.read_text() + '\n[mcp_servers.extra]\ncommand="extra"\n')
    with pytest.raises(RuntimeErrorBase, match='policy changed'):
        runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))


@pytest.mark.parametrize('missing', ['glasshive_capability_broker', 'env'])
def test_missing_signed_broker_fails_closed(runtime, missing):
    authority = bundle()
    authority.pop(missing)
    with pytest.raises(RuntimeErrorBase, match='requires its signed'):
        runtime._host_codex_worker_config('', capability_bundle=authority)


def test_normal_configuration_still_inherits_native_capabilities(runtime):
    config = tomllib.loads(runtime._host_codex_worker_config('', capability_bundle=bundle(True)))
    assert config['features']['apps'] is True
    assert config['features']['plugins'] is True
    assert config['features']['shell_tool'] is True
    assert 'computer-use' in config['mcp_servers']
    assert 'sandbox_mode' not in config


@pytest.mark.parametrize('lane', ['conversation', 'mission'])
def test_normal_conversation_and_mission_commands_keep_full_access(runtime, tmp_path, lane):
    authority = bundle(True)
    authority.pop('developer_instructions')
    authority['run_mode'] = lane
    candidate = worker(tmp_path, authority)
    candidate['trusted_run_lane'] = lane
    command, _ = runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))
    assert '--dangerously-bypass-approvals-and-sandbox' in command
    assert '--disable' not in command
    assert '--ignore-rules' not in command
