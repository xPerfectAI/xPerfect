"""Conversation exits preserve executor types through the shared supervised owner."""
import json
import sys

import pytest

from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.direct_runtime import DirectModelRuntime
from workers_projects_runtime.failure_classification import classify_runtime_error
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime


def failing_conversation(tmp_path, monkeypatch, event, *, native=False, authority_failure=False):
    profile = 'claude-code' if native else 'direct-xai'
    runtime = (HostClaudeCodeRuntime(base_dir=str(tmp_path / 'state')) if native else
               DirectModelRuntime(profile, base_dir=str(tmp_path / 'state')))
    worker = {'worker_id': 'wrk_failure_fixture', 'tenant_id': 'local', 'owner_id': 'owner',
        'name': 'Synthetic failure', 'profile': profile, 'execution_mode': 'host',
        'workspace_root': str(tmp_path / 'workspace'), 'trusted_run_lane': 'conversation',
        'bootstrap_bundle_json': json.dumps({'run_mode': 'conversation', 'provider_model': 'opus' if native else 'grok-4.7'})}
    workspace = runtime._host_workspace_dir(worker)
    workspace.mkdir(parents=True, exist_ok=True)
    info = runtime._host_runtime_info(worker)
    monkeypatch.setattr(runtime, 'ensure_worker_ready', lambda _worker: info)
    monkeypatch.setattr(runtime, '_native_provider_authority_receipt', lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_command_stdin_text', lambda *a, **k: None)
    script = tmp_path / 'failed_provider.py'
    script.write_text('import sys\nprint(' + repr(json.dumps(event)) + ', flush=True)\nsys.exit(1)\n')
    monkeypatch.setattr(runtime, '_build_command', lambda *a: ([sys.executable, str(script)], {}))
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = 'run_conversation_failure'
    if authority_failure:
        root = runtime._run_root(worker['worker_id'], run_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / 'native-authority-failure.json').write_text(json.dumps({'code': 'authority_update_unconfirmed'}))
    with pytest.raises(RuntimeErrorBase) as caught:
        runtime._run_conversation_task(worker, 'Synthetic current source', run_id=run_id, timeout_sec=5)
    return runtime, worker, caught.value


@pytest.mark.parametrize('event,expected', [
    ({'type': 'direct.error', 'error_type': 'UnexpectedModelBehavior', 'stage': 'provider_request'}, 'provider_response_invalid'),
    ({'type': 'direct.error', 'error_type': 'ValidationError', 'stage': 'output_validation'}, 'provider_response_invalid'),
    ({'type': 'direct.error', 'error_type': 'AuthenticationError', 'status_code': 401, 'stage': 'provider_request'}, 'provider_auth_missing'),
    ({'type': 'direct.error', 'error_type': 'BadRequestError', 'status_code': 400, 'stage': 'provider_request'}, 'provider_request_rejected'),
])
def test_direct_error_survives_actual_supervised_conversation_exit(tmp_path, monkeypatch, event, expected):
    runtime, worker, error = failing_conversation(tmp_path, monkeypatch, event)
    classification = classify_runtime_error(error, runtime_name=runtime.runtime_name)
    assert classification.failure_class == expected and classification.structured
    assert not classification.retryable
    assert 'Synthetic current source' not in classification.diagnostic_summary
    mission_class, _ = runtime._provider_failure_for_process(worker=worker, run_id='run_mission_adjacent',
        stdout=json.dumps(event), stderr='', exit_code=1)
    assert classification.failure_class == mission_class.failure_class
    assert not ConversationProvider._serial_fallback_eligible(
        object(), {'fallback_model_id': 'claude-code:claude-opus-5-5'},
        {'state': 'failed', **classification.as_store_fields()}, set())


def test_native_unknown_exit_retains_private_unknown_classification(tmp_path, monkeypatch):
    runtime, _, error = failing_conversation(tmp_path, monkeypatch, {'synthetic': 'unrecognized'}, native=True)
    classification = classify_runtime_error(error, runtime_name=runtime.runtime_name)
    assert classification.failure_class == 'unknown' and not classification.retryable
    assert classification.diagnostic_summary == 'class=unknown; exit_code=1'


def test_authority_failure_still_overrides_executor_failure(tmp_path, monkeypatch):
    runtime, _, error = failing_conversation(tmp_path, monkeypatch,
        {'type': 'direct.error', 'error_type': 'AuthenticationError', 'status_code': 401, 'stage': 'provider_request'},
        authority_failure=True)
    classification = classify_runtime_error(error, runtime_name=runtime.runtime_name)
    assert classification.failure_class == 'authority_update_unconfirmed'
    assert classification.structured and classification.retryable
    assert classification.provider_event_source == 'native_authority_preflight'


def test_native_structured_exit_retains_its_existing_classification(tmp_path, monkeypatch):
    runtime, _, error = failing_conversation(tmp_path, monkeypatch, {
        'type': 'result', 'is_error': True, 'api_error_status': 401,
        'terminal_reason': 'api_error', 'result': 'Synthetic private provider diagnostic'}, native=True)
    classification = classify_runtime_error(error, runtime_name=runtime.runtime_name)
    assert classification.failure_class == 'provider_auth_missing'
    assert classification.structured and not classification.retryable
    assert 'Synthetic private provider diagnostic' not in classification.diagnostic_summary
