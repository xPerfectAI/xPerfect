"""Stored bootstrap reaches the real host command builder; no native process runs."""
import json

import pytest
from test_current_native_account import host, connect
from workers_projects_runtime import current_native_account as native
from workers_projects_runtime.mission_provider_accounts import (
    _CLAUDE_CONFLICTING_ENV, apply_bound_provider_account_environment,
)
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime


# Keep the reproduced bypass inputs independent of the implementation inventory.
REPRODUCED_AUTH_ENV = {
    'ANTHROPIC_CUSTOM_HEADERS', 'CLAUDE_CODE_USE_ANTHROPIC_AWS',
    'ANTHROPIC_AWS_API_KEY', 'ANTHROPIC_AWS_WORKSPACE_ID', 'ANTHROPIC_API_KEY',
}
AUTH_ENV = REPRODUCED_AUTH_ENV | _CLAUDE_CONFLICTING_ENV | native._CURRENT_NATIVE_AUTH_ENV


@pytest.mark.parametrize('producer', ['environment', 'settings', 'both'])
def test_stored_bootstrap_cannot_override_current_native_identity(host, tmp_path, monkeypatch, producer):
    _, worker = connect(host)
    monkeypatch.setenv('WPR_CLAUDE_CODE_ENABLE_CHROME', '0')
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / 'native-work'))
    monkeypatch.setattr(runtime, '_inject_private_subscription_auth', lambda *_: pytest.fail('ambient auth import'))
    worker.update(workspace_root=str(tmp_path / 'workspace'), model='opus')
    hostile = {name: 'synthetic-override' for name in AUTH_ENV}
    hostile.update(HOME='/synthetic/other-owner', USER='synthetic-other', LOGNAME='synthetic-other')
    settings = {'permissions': {'allow': ['Read']}, 'enabledMcpjsonServers': ['approved-mcp'],
                'env': {'BASH_DEFAULT_TIMEOUT_MS': '30000'}}
    bundle = worker['bootstrap_bundle']
    bundle['env'] = {'SYNTHETIC_TASK_SETTING': 'retained'}
    if producer in {'environment', 'both'}:
        bundle['env'].update(hostile)
    if producer in {'settings', 'both'}:
        settings['env'].update(hostile, CLAUDE_CONFIG_DIR='/synthetic/other-config')
        settings.update({name: 'synthetic-helper' for name in native._CURRENT_NATIVE_AUTH_SETTINGS})
    bundle['claude_settings_local'] = settings
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    original = worker['bootstrap_bundle_json']
    with host[1].bind(worker, runtime_name='claude-code', run_id='run', timeout_sec=1, abort_binding=lambda _: None) as bound:
        command, env = runtime._build_command(bound, 'Synthetic task', runtime._host_runtime_info(bound))
        actual = json.loads(command[command.index('--settings') + 1])
        assert not AUTH_ENV.intersection(set(env) - {'CLAUDE_SECURESTORAGE_CONFIG_DIR'})
        assert not AUTH_ENV.intersection(actual['env'])
        assert not native._CURRENT_NATIVE_AUTH_SETTINGS.intersection(actual)
        assert not {'HOME', 'USER', 'LOGNAME', 'CLAUDE_CONFIG_DIR'}.intersection(actual['env'])
        assert env['HOME'] == native.owner_environment()['HOME']
        for key in ('USER', 'LOGNAME'):
            assert env.get(key) == native.owner_environment().get(key)
        assert env['CLAUDE_SECURESTORAGE_CONFIG_DIR'] == ''
        assert env['SYNTHETIC_TASK_SETTING'] == 'retained'
        assert actual['env']['BASH_DEFAULT_TIMEOUT_MS'] == '30000'
        assert actual['permissions'] == {'allow': ['Read']}
        assert actual['enabledMcpjsonServers'] == ['approved-mcp']
        assert command[command.index('--model') + 1] == runtime._provider_model_for_worker(worker)
        assert '--strict-mcp-config' in command
        assert not AUTH_ENV.intersection(host[3][-1][1]['env'])
    assert worker['bootstrap_bundle_json'] == original


def test_other_provider_environment_modes_do_not_use_current_native_policy():
    env = {name: 'synthetic-route' for name in native._CURRENT_NATIVE_AUTH_ENV}
    original = dict(env)
    apply_bound_provider_account_environment({'worker_id': 'unbound'}, env, runtime_name='claude-code')
    assert env == original


def test_current_native_settings_reject_invalid_env(host):
    _, worker = connect(host)
    with host[1].bind(worker, runtime_name='claude-code', run_id='run', timeout_sec=1, abort_binding=lambda _: None) as bound:
        with pytest.raises(native.ControlPlaneError, match='must be an object'):
            native.project_current_settings(bound, {'env': ['invalid']})
