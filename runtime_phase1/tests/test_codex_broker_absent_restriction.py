from __future__ import annotations

import json
import tomllib

import pytest

import workers_projects_runtime.profile_runtime as module
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('VIVENTIUM_NATIVE_FIRST_ADMIN_STATE', raising=False)
    instance = module.HostCodexCliRuntime(base_dir=str(tmp_path / 'private-state'))
    monkeypatch.setattr(instance, '_copy_host_codex_auth', lambda _: None)
    monkeypatch.setattr(instance, '_host_plugin_denylist', lambda: ())
    monkeypatch.setattr(instance, '_host_codex_personality', lambda: 'inherit')
    monkeypatch.setattr(instance, '_project_host_codex_capability_roots', lambda _: pytest.fail('Inherited Codex capabilities'))
    return instance


def authority():
    return {'run_mode': 'conversation', 'access_mode': 'full',
        'provider_capabilities': {'native_tools': False},
        'developer_instructions': 'Use the admitted synthetic context.',
        'env': {'GLASSHIVE_PROVIDER_SESSION_MODE': 'stateless', 'WPR_CODEX_CLI_REASONING_EFFORT': 'high'}}


def worker(tmp_path, bundle):
    life = tmp_path / 'Life'
    life.mkdir(exist_ok=True)
    (life / 'AGENTS.md').write_text('Unrelated private project instructions')
    return {'worker_id': 'synthetic-codex-no-broker', 'profile': 'codex-cli',
        'execution_mode': 'host', 'trusted_run_lane': 'conversation',
        'workspace_root': str(life), 'model': 'gpt-6.1-sol',
        'bootstrap_bundle_json': json.dumps(bundle)}


def test_broker_absent_uses_sealed_no_mcp_configuration(runtime):
    bundle = authority()
    config = tomllib.loads(runtime._host_codex_worker_config(
        '[mcp_servers.unadmitted]\ncommand="unadmitted"',
        developer_instructions=bundle['developer_instructions'], capability_bundle=bundle))
    assert config.get('mcp_servers', {}) == {}
    assert config['developer_instructions'] == bundle['developer_instructions']
    assert config['sandbox_mode'] == 'read-only'
    assert config['approval_policy'] == 'never'
    assert config['project_doc_max_bytes'] == 0
    assert config['web_search'] == 'disabled'
    assert config['features']['code_mode_host'] is False
    assert config['features']['goals'] is False
    assert all(config['features'][key] is False for key in module._CODEX_RESTRICTED_NATIVE_FEATURES)


def test_broker_absent_launch_does_not_require_grant_or_resume(runtime, tmp_path):
    candidate = worker(tmp_path, authority())
    runtime._write_session_key(candidate['worker_id'], 'previous-authorized-session')
    runtime._write_conversation_runtime_files(candidate, runtime._bootstrap_bundle_for_worker(candidate))
    command, env = runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))
    assert command[command.index('-m') + 1] == 'gpt-6.1-sol'
    assert 'model_reasoning_effort="high"' in command
    assert command[1:3] == ['exec', '--json']
    assert '--ephemeral' in command and '--strict-config' in command and '--ignore-rules' in command
    assert ['--disable', 'code_mode_host'] in [command[i:i + 2] for i in range(len(command) - 1)]
    assert ['--disable', 'goals'] in [command[i:i + 2] for i in range(len(command) - 1)]
    assert '--add-dir' not in command and '--dangerously-bypass-approvals-and-sandbox' not in command
    assert command[command.index('-s') + 1] == 'read-only'
    assert command[command.index('-C') + 1] == str(runtime._state_dir(candidate['worker_id']) / 'conversation-workspace')
    assert 'GLASSHIVE_CAPABILITY_BROKER_TOKEN' not in env


@pytest.mark.parametrize('broker', [None, {}, 'unadmitted', {'version': 2, 'name': 'broker', 'url': 'https://example.test'}, {'version': 1, 'name': 'broker'}])
def test_malformed_present_broker_is_not_treated_as_absent(runtime, broker):
    bundle = {**authority(), 'glasshive_capability_broker': broker}
    with pytest.raises(RuntimeErrorBase, match='requires its signed capability broker'):
        runtime._host_codex_worker_config('', capability_bundle=bundle)


def test_broker_absent_changed_seal_refuses_launch(runtime, tmp_path):
    candidate = worker(tmp_path, authority())
    runtime._write_conversation_runtime_files(candidate, runtime._bootstrap_bundle_for_worker(candidate))
    path = runtime._host_codex_home(candidate) / 'config.toml'
    path.write_text(path.read_text() + '\n[mcp_servers.unadmitted]\ncommand="unadmitted"\n')
    with pytest.raises(RuntimeErrorBase, match='policy changed'):
        runtime._build_command(candidate, 'Synthetic.', runtime._host_runtime_info(candidate))
