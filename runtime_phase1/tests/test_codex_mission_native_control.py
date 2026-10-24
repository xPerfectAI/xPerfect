"""Native mission control must not depend on optional process prewarming."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from workers_projects_runtime import native_input
from test_host_codex_bound_native_config import configured


def mission(configured, tmp_path, monkeypatch, *, supported=True, warm=None):
    runtime, worker, account, home, info = configured
    worker.update(_active_run_id='run-native-mission', _run_attempt_id='attempt-native-mission')
    if warm is not None:
        bundle = json.loads(worker['bootstrap_bundle_json'])
        bundle['runtime_performance'] = {'native_warm_servers': warm}
        worker['bootstrap_bundle_json'] = json.dumps(bundle)
    assert not runtime._conversation_mode_from_worker(worker)
    monkeypatch.setattr(runtime, '_native_midstream_schema_supported', lambda: supported)
    monkeypatch.setattr(runtime, '_warm_context', lambda *_: pytest.fail('Warm-OFF mission must not lease or initialize a pool'))
    command, env = runtime._build_command(worker, 'Synthetic native mission', info)
    root = tmp_path / 'mission-control'
    root.mkdir()
    return runtime, worker, command, env, root


@pytest.mark.parametrize('warm', [None, 0])
def test_selected_account_mission_keeps_native_control_with_warm_off(configured, tmp_path, monkeypatch, warm):
    runtime, worker, command, env, root = mission(configured, tmp_path, monkeypatch, warm=warm)
    prepared = runtime._prepare_native_authority_command(worker, command, run_root=root, timeout_sec=30)
    assert prepared != command and Path(prepared[1]).name == 'native-input-relay.py'
    context = native_input.read_object(root / 'native-input-context.json')
    assert context['protocol'] == 'codex-app-server'
    assert context['workerId'] == worker['worker_id'] and context['runId'] == worker['_active_run_id']
    assert context['attemptId'] == worker['_run_attempt_id']
    launch = context['launch']
    assert launch['thread']['model'] == 'gpt-6.1-sol' and launch['turn']['effort'] == 'high'
    assert launch['thread']['config']['mcp_servers']['native']['enabled_tools'] == ['read', 'act']
    assert launch['thread']['config']['plugins']['selected-plugin@example']['enabled'] is True
    assert launch['thread']['config']['developer_instructions'] == 'Exact worker authority'
    assert launch['thread']['approvalPolicy'] == 'never'
    assert launch['thread']['config']['sandbox_mode'] == launch['thread']['sandbox'] == 'danger-full-access'
    assert '--profile' not in prepared and 'Exact worker authority' not in ' '.join(prepared)
    assert env['CODEX_HOME'].endswith('/account/codex')
    assert not runtime._warm_leases


@pytest.mark.parametrize('unsupported', ['version', 'policy'])
def test_warm_off_mission_preserves_exact_unsupported_native_exec(configured, tmp_path, monkeypatch, unsupported):
    runtime, worker, command, env, root = mission(configured, tmp_path, monkeypatch, supported=unsupported != 'version')
    if unsupported == 'policy': command[-1:-1] = ['--unknown-native-policy']
    assert runtime._prepare_native_authority_command(worker, command, run_root=root, timeout_sec=30) is command
    assert not (root / 'native-input-context.json').exists()
    assert not runtime._warm_leases


def test_warm_off_restricted_mission_does_not_gain_native_control_or_tools(configured, tmp_path, monkeypatch):
    runtime, worker, account, home, info = configured
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['provider_capabilities'] = {'native_tools': False}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    (home / 'config.toml').write_text(runtime._host_codex_worker_config('', capability_bundle=bundle))
    runtime, worker, command, env, root = mission(configured, tmp_path, monkeypatch)
    assert runtime._prepare_native_authority_command(worker, command, run_root=root, timeout_sec=30) is command
    assert '--ignore-rules' in command and '--strict-config' in command
    assert command[command.index('-s') + 1] == 'read-only'
    assert not (root / 'native-input-context.json').exists()
    assert not runtime._warm_leases
