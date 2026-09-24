"""Public-safe regression evidence for reconciled runtime behavior."""
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from workers_projects_runtime.failure_classification import (
    FailureClassification, compact_provider_failure_diagnostic,
)
from workers_projects_runtime.profile_runtime import (
    HostCodexCliRuntime, _private_cli_failure_classification,
)


def test_compact_diagnostic_preserves_metadata_and_excludes_transcript():
    classification = FailureClassification(
        'provider_auth_missing', False, 'Reconnect', 'Repair credentials', 'private text',
        personal_account_reconnect=True, structured=True,
        retry_after_s=12, provider_event_source='native_result',
    )
    record = {'type': 'result', 'is_error': True, 'api_error_status': 403,
              'error': 'authentication_failed',
              'result': 'Synthetic private content; explicit deny in identity-based policy; bedrock:InvokeModelWithResponseStream'}
    result = _private_cli_failure_classification(
        classification, exit_code=1, stdout=json.dumps(record),
    )
    assert replace(result, diagnostic_summary=classification.diagnostic_summary) == classification
    assert result.diagnostic_summary == (
        'class=provider_auth_missing; reason=identity_policy_explicit_deny; status=403; '
        'operation=bedrock_invoke_stream; provider_error=authentication_failed; exit_code=1'
    )
    assert len(result.diagnostic_summary) <= 256
    record['type'] = 'assistant'
    assert compact_provider_failure_diagnostic(
        stdout=json.dumps(record), stderr='', classification=classification, exit_code=1,
    ) == 'class=provider_auth_missing; exit_code=1'


def test_compact_diagnostic_ignores_prior_attempt_and_unrelated_auth_label():
    classification = FailureClassification('provider_rate_limited', True, 'Wait', 'Retry', '')
    records = [
        {'type': 'thread.started'},
        {'type': 'result', 'is_error': True, 'api_error_status': 403,
         'error': 'authentication_failed'},
        {'type': 'thread.started'},
        {'type': 'result', 'is_error': True, 'api_error_status': 429,
         'error': 'authentication_failed'},
    ]
    assert compact_provider_failure_diagnostic(
        stdout='\n'.join(map(json.dumps, records)), stderr=json.dumps(records[1]),
        classification=classification, exit_code=1,
    ) == 'class=provider_rate_limited; status=429; exit_code=1'


def _record_session(runtime, worker_id, process):
    runtime._write_active_session(worker_id, {
        'session_name': 'job-run_synthetic', 'run_id': 'run_synthetic', 'process_pid': process.pid,
        'process_group': os.getpgid(process.pid),
        'process_start_identity': runtime._process_start_identity(process.pid),
    })


def test_real_host_stop_kills_child_after_leader_exits_on_term(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / 'runtime'))
    worker_id = 'wrk_synthetic_group'
    child_pid_file = tmp_path / 'child.pid'
    script = '''import os, signal, sys, time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(sys.argv[1], 'w') as f:
        f.write(str(os.getpid()))
    while True: time.sleep(1)
while True: time.sleep(1)
'''
    leader = subprocess.Popen([sys.executable, '-c', script, str(child_pid_file)], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert child_pid_file.exists()
        _record_session(runtime, worker_id, leader)
        runtime._register_process(worker_id, leader)
        assert runtime._stop_active_process(worker_id, worker={'worker_id': worker_id}, run_id='run_synthetic')
        leader.wait(timeout=2)
        assert not runtime._host_process_group_alive(leader.pid)
        assert runtime._read_active_session(worker_id) is None
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait(timeout=2)


def test_dead_leader_with_unverified_survivors_keeps_session(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker_id = 'wrk_synthetic_unverified'
    runtime._write_active_session(worker_id, {
        'session_name': 'job-run_synthetic', 'run_id': 'run_synthetic', 'process_pid': 23456, 'process_group': 23456,
        'process_start_identity': 'synthetic-original',
    })
    monkeypatch.setattr(runtime, '_recorded_process_is_running', lambda *a: False)
    monkeypatch.setattr(runtime, '_host_process_group_alive', lambda *a: True)
    assert not runtime._stop_active_process(worker_id, run_id='run_synthetic')
    assert runtime._read_active_session(worker_id) is not None


@pytest.mark.parametrize('profile', ['unknown', 'openclaw-unknown', '', 'grok'])
def test_unknown_profile_never_falls_through_to_openclaw(tmp_path, profile):
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
    from workers_projects_runtime.profile_registry import UnsupportedWorkerProfileError
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path))
    with pytest.raises(UnsupportedWorkerProfileError):
        runtime._runtime_for_profile(profile)
    with pytest.raises(UnsupportedWorkerProfileError):
        runtime._runtime_for_worker({'worker_id': 'wrk_missing_profile'})
    assert runtime._runtime_for_profile('openclaw') is runtime.openclaw
    assert runtime._runtime_for_profile('openclaw-general') is runtime.openclaw
    assert runtime._runtime_for_profile('openclaw-codex', 'host') is runtime.host_openclaw
    with pytest.raises(UnsupportedWorkerProfileError):
        runtime._runtime_for_profile('codex-cli', 'unsupported')


def test_finalization_cannot_race_stop_while_group_survives(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker_id = 'wrk_synthetic_finalize'
    runtime._write_active_session(worker_id, {
        'session_name': 'job-run_synthetic', 'run_id': 'run_synthetic',
        'process_pid': 23456, 'process_group': 23456,
        'process_start_identity': 'synthetic-original',
    })
    session = runtime._read_active_session(worker_id)
    monkeypatch.setattr(runtime, '_host_process_group_alive', lambda *a: True)
    assert not runtime._finalize_owned_host_generation(
        worker_id, expected_process=None, expected_session=session,
        expected_slot_token=None, clear_session=True,
    )
    assert runtime._read_active_session(worker_id) == session
