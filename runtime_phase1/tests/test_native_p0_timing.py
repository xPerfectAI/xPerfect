"""Timing is optional and cannot change the existing admission or queue contracts."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.service import WorkersProjectsService
import workers_projects_runtime.native_model_selection as timing


@pytest.mark.parametrize('state,missing', [('queued',False), ('cancelled',False), ('queued',True)])
def test_start_lock_timings_preserve_exact_start_fence_and_release(state, missing, monkeypatch):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY','1')
    logger=Mock()
    monkeypatch.setattr(timing,'_logger',logger)
    monkeypatch.setattr(timing.time,'monotonic_ns',Mock(side_effect=[0,2_000_000,9_000_000,10_000_000,11_000_000]))
    # The event helper also reads the local monotonic clock. Assert ordering/duration separately.
    provider=ConversationProvider.__new__(ConversationProvider)
    provider._start_lock=threading.RLock()
    current={'run_id':'synthetic-run','state':state}
    provider.store=SimpleNamespace(get_provider_request=lambda _:None if missing else current,
                                   get_run=lambda _:{'worker_id':'synthetic-worker','state':'queued'})
    provider.service=SimpleNamespace(start_assigned_run=Mock())
    provider._deadline_reached=lambda _:False
    if missing:
        with pytest.raises(HTTPException) as caught:
            provider._start_assigned_run_before_deadline('synthetic-request',run_id='synthetic-run',worker_id='synthetic-worker')
        assert caught.value.status_code==404
    else:
        result=provider._start_assigned_run_before_deadline('synthetic-request',run_id='synthetic-run',worker_id='synthetic-worker')
        assert result==(current,state=='queued')
    assert provider.service.start_assigned_run.call_count==(1 if state=='queued' and not missing else 0)
    assert provider._start_lock.acquire(blocking=False)
    provider._start_lock.release()
    rows=[json.loads(call.args[1]) for call in logger.info.call_args_list]
    assert [row['stage'] for row in rows]==['start_lock_acquired','start_lock_pre_release']
    assert rows[0]['waitMs']==2
    assert rows[1]['heldUntilPreReleaseMs']==8
    assert rows[0]['requestHash']==rows[1]['requestHash']
    assert 'synthetic-request' not in json.dumps(rows)


def test_executor_markers_use_same_existing_worker_generation_and_submission(monkeypatch):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY','1')
    logger=Mock(); monkeypatch.setattr(timing,'_logger',logger)
    service=WorkersProjectsService.__new__(WorkersProjectsService)
    service.store=SimpleNamespace(get_worker=lambda _:{'state':'ready'}, has_active_operator_pause=lambda _:False,
                                  has_unconfirmed_host_run_start=lambda _:False)
    service._trusted_run_lane=lambda _:'conversation'
    service._bootstrap_bundle_for=lambda _:{'run_mode':'conversation'}
    service._processors_lock=threading.Lock()
    service._shutdown_event=threading.Event()
    service._active_processors=set(); service._processor_generations={}
    submitted=[]; entries=[]
    def submit(method,*args):
        submitted.append(args)
        return method(*args)
    service.conversation_executor=SimpleNamespace(submit=submit)
    service.executor=SimpleNamespace(submit=Mock(side_effect=AssertionError('wrong lane')))
    service._process_worker_queue_parallel=lambda *args:entries.append(args)
    service._ensure_worker_processor('synthetic-worker')
    assert submitted==entries==[('synthetic-worker',1)]
    rows=[json.loads(call.args[1]) for call in logger.info.call_args_list]
    assert [row['stage'] for row in rows]==['executor_submit','executor_entry']
    assert rows[0]['workerHash']==rows[1]['workerHash']
    assert rows[0]['generation']==rows[1]['generation']==1
    assert 'synthetic-worker' not in json.dumps(rows)


# Admission durations bracket complete calls, rather than subtracting the Store's
# claimed_at/admitted_at timestamps (which are taken before those calls commit).
import workers_projects_runtime.service as service_module
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.store import Store


def _admission_fixture(tmp_path, monkeypatch, enabled, *, logger_failure=False):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1' if enabled else '0')
    log = Mock()
    if logger_failure:
        log.info.side_effect = OSError('synthetic-private-log-error')
    monkeypatch.setattr(timing, '_logger', log)
    store = Store(str(tmp_path / 'synthetic-admission.sqlite3'))
    observed = []

    class ObservingRuntime(StubRuntime):
        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)
            durable = store.get_run(run_id)
            lease = store.get_active_host_run_lease_for_run(run_id)
            observed.append((worker, instruction, durable, lease))
            return 'synthetic-result'

    service = WorkersProjectsService(store, ObservingRuntime(), reconcile_on_startup=False,
                                     start_background_consumers=False)
    project = store.create_project('synthetic-owner', 'Synthetic', 'Synthetic', 'codex-cli')
    worker = store.create_worker(
        project_id=project['project_id'], owner_id='synthetic-owner', name='Synthetic',
        role='worker', profile='codex-cli', backend='codex-cli', runtime='codex-cli',
        model='stub/codex-cli', execution_mode='docker',
        bootstrap_bundle={'env': {'SYNTHETIC_PRIVATE_VALUE': 'synthetic-secret'},
                          'runtime_config': {'reasoning_effort': 'high'}},
    )
    store.update_worker_state(worker['worker_id'], 'ready')
    run = service.assign_run(worker['worker_id'], 'synthetic-private-instruction', start_processor=False)
    with service._processors_lock:
        service._active_processors.add(worker['worker_id'])
        service._processor_generations[worker['worker_id']] = 1
    return service, store, worker, run, observed, log


def _admission_rows(log):
    return [row for call in log.info.call_args_list
            if isinstance(call.args[1], str)
            for row in [json.loads(call.args[1])]
            if row['stage'].startswith('admission_')]


@pytest.mark.parametrize('enabled', [False, True])
def test_admission_observation_preserves_durable_attempt_lease_and_exact_inputs(tmp_path, monkeypatch, enabled):
    service, store, worker, run, observed, log = _admission_fixture(tmp_path, monkeypatch, enabled)
    try:
        service._process_worker_queue(worker['worker_id'], 1)
        assert len(observed) == 1
        projected, instruction, durable, lease = observed[0]
        assert instruction == 'synthetic-private-instruction'
        assert projected['model'] == 'stub/codex-cli'
        assert json.loads(projected['bootstrap_bundle_json'])['runtime_config'] == {'reasoning_effort': 'high'}
        assert projected['_active_run_id'] == run['run_id']
        assert durable['state'] == 'running' and durable['runtime_invoked_at']
        assert lease['attempt_id'] == durable['active_attempt_id']
        assert store.get_run(run['run_id'])['state'] == 'completed'
        attempts = store.list_run_attempts(run['run_id'])
        assert len(attempts) == 1 and attempts[0]['state'] == 'completed'
        rows = _admission_rows(log)
        assert bool(rows) == enabled
        if enabled:
            assert [row['stage'] for row in rows[:11]] == [
                'admission_claim', 'admission_health', 'admission_lease_generation',
                'admission_context', 'admission_scoped_authority', 'admission_invocation_grant',
                'admission_bundle', 'admission_files', 'admission_workspace',
                'admission_provider_deadline', 'admission_commit',
            ]
            assert all(row['status'] == 'completed' and row['durationMs'] >= 0 for row in rows)
            run_rows = rows[:11]
            assert {row['runHash'] for row in run_rows} == {timing.native_timing_hash('run', run['run_id'])}
            assert {row['workerHash'] for row in rows} == {timing.native_timing_hash('worker', worker['worker_id'])}
            assert all(set(row) <= {'event', 'stage', 'observedAtMs', 'monotonicMs',
                                    'durationMs', 'status', 'workerHash', 'runHash'} for row in rows)
            assert all(value not in json.dumps(rows) for value in
                       (worker['worker_id'], run['run_id'], 'synthetic-secret', instruction, 'high'))
    finally:
        service.shutdown(); store.close()


@pytest.mark.parametrize('enabled', [False, True])
def test_admission_lost_cas_preserves_no_runtime_requeue_and_failed_status(tmp_path, monkeypatch, enabled):
    service, store, worker, run, observed, log = _admission_fixture(tmp_path, monkeypatch, enabled)
    monkeypatch.setattr(store, 'admit_claimed_run', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, '_ensure_worker_processor', lambda _worker_id: None)
    try:
        service._process_worker_queue(worker['worker_id'], 1)
        assert observed == []
        assert store.get_run(run['run_id'])['state'] == 'queued'
        assert not store.list_active_host_run_leases()
        rows = _admission_rows(log)
        assert bool(rows) == enabled
        if enabled:
            assert rows[-1]['stage'] == 'admission_commit' and rows[-1]['status'] == 'failed'
    finally:
        service.shutdown(); store.close()


@pytest.mark.parametrize('enabled', [False, True])
def test_admission_unhealthy_route_stops_before_compute_grant_and_commit(tmp_path, monkeypatch, enabled):
    service, store, worker, run, observed, log = _admission_fixture(tmp_path, monkeypatch, enabled)
    monkeypatch.setattr(service, '_handle_unhealthy_provider_route', lambda *_args: True)
    try:
        service._process_worker_queue(worker['worker_id'], 1)
        assert observed == []
        assert [row['stage'] for row in _admission_rows(log)] == (
            ['admission_claim', 'admission_health'] if enabled else [])
        assert not store.get_run(run['run_id'])['runtime_invoked_at']
    finally:
        service.shutdown(); store.close()


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('bad_value', ['synthetic-private-malformed', '[]', '{"private":"synthetic-secret"'])
def test_admission_malformed_continuation_retains_same_error_and_safe_failure(tmp_path, monkeypatch, enabled, bad_value):
    service, store, worker, run, observed, log = _admission_fixture(tmp_path, monkeypatch, enabled)
    run = {**run, 'continuation_contract_json': bad_value}
    try:
        with pytest.raises(RuntimeError, match='^continuation_contract_invalid$'):
            service._run_local_worker(worker, run)
        rows = _admission_rows(log)
        if enabled:
            assert rows[-1]['stage'] == 'admission_bundle' and rows[-1]['status'] == 'failed'
            assert all(value not in json.dumps(rows) for value in (bad_value, 'synthetic-secret'))
        else:
            assert rows == []
    finally:
        service.shutdown(); store.close()


@pytest.mark.parametrize('enabled', [False, True])
def test_admission_file_failure_retains_exact_exception_and_skips_workspace(tmp_path, monkeypatch, enabled):
    service, store, worker, run, observed, log = _admission_fixture(tmp_path, monkeypatch, enabled)
    error = RuntimeError('synthetic-secret-file-path')
    monkeypatch.setattr(store, 'get_run_file_manifest', Mock(side_effect=error))
    try:
        with pytest.raises(RuntimeError) as caught:
            service._run_local_worker(worker, run)
        assert caught.value is error
        rows = _admission_rows(log)
        if enabled:
            assert rows[-1]['stage'] == 'admission_files' and rows[-1]['status'] == 'failed'
            assert str(error) not in json.dumps(rows)
        else:
            assert rows == []
    finally:
        service.shutdown(); store.close()


def test_admission_logger_failure_cannot_change_real_execution(tmp_path, monkeypatch):
    service, store, worker, run, observed, log = _admission_fixture(tmp_path, monkeypatch, True, logger_failure=True)
    try:
        service._process_worker_queue(worker['worker_id'], 1)
        assert len(observed) == 1 and store.get_run(run['run_id'])['state'] == 'completed'
        assert log.info.call_count >= 11
    finally:
        service.shutdown(); store.close()


def test_admission_default_off_adds_no_clock_hash_or_log(monkeypatch):
    monkeypatch.delenv('VIVENTIUM_VOICE_LOG_LATENCY', raising=False)
    clock = Mock(side_effect=AssertionError('disabled clock called'))
    event = Mock(side_effect=AssertionError('disabled event called'))
    digest = Mock(side_effect=AssertionError('disabled hash called'))
    monkeypatch.setattr(service_module.time, 'monotonic_ns', clock)
    monkeypatch.setattr(service_module, 'native_timing_event', event)
    monkeypatch.setattr(service_module, 'native_timing_hash', digest)
    with service_module._native_admission_timing('admission_commit', 'worker', 'run'):
        pass
    clock.assert_not_called(); event.assert_not_called(); digest.assert_not_called()


@pytest.mark.parametrize('stage', ['untrusted-secret-stage', None, {'secret': 'synthetic-secret'}])
def test_admission_unknown_or_malformed_stage_cannot_enter_log(monkeypatch, stage):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    event = Mock(); monkeypatch.setattr(service_module, 'native_timing_event', event)
    with service_module._native_admission_timing(stage, 'synthetic-worker', 'synthetic-run'):
        pass
    event.assert_not_called()


@pytest.mark.parametrize('bad_id', [None, {}, 's' * 257])
def test_admission_malformed_id_is_omitted_not_serialized(monkeypatch, bad_id):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    event = Mock(); monkeypatch.setattr(service_module, 'native_timing_event', event)
    with service_module._native_admission_timing('admission_commit', bad_id, bad_id):
        pass
    event.assert_called_once()
    assert set(event.call_args.kwargs) == {'durationMs', 'status'}


@pytest.mark.parametrize('failure_point', ['enabled', 'start_clock', 'end_clock', 'hash', 'event'])
def test_admission_observer_failure_preserves_original_result_and_exception(monkeypatch, failure_point):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    broken = Mock(side_effect=OSError('synthetic-secret-observer-error'))
    if failure_point == 'enabled':
        monkeypatch.setattr(service_module, 'native_timing_enabled', broken)
    elif failure_point in {'start_clock', 'end_clock'}:
        if failure_point == 'end_clock':
            broken.side_effect = [1, OSError('synthetic-secret-observer-error'), 1, OSError('synthetic-secret-observer-error')]
        monkeypatch.setattr(service_module.time, 'monotonic_ns', broken)
    else:
        monkeypatch.setattr(service_module, 'native_timing_hash' if failure_point == 'hash' else 'native_timing_event', broken)
    with service_module._native_admission_timing('admission_commit', 'worker', 'run'):
        result = 'unchanged'
    assert result == 'unchanged'
    original = RuntimeError('synthetic-original-error')
    with pytest.raises(RuntimeError) as caught:
        with service_module._native_admission_timing('admission_commit', 'worker', 'run'):
            raise original
    assert caught.value is original


@pytest.mark.parametrize('error', [RuntimeError('synthetic-secret-error'), KeyboardInterrupt(), SystemExit(7)])
def test_admission_fixed_failure_status_preserves_error_identity_without_text(monkeypatch, error):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    event = Mock(); monkeypatch.setattr(service_module, 'native_timing_event', event)
    with pytest.raises(type(error)) as caught:
        with service_module._native_admission_timing('admission_commit', 'worker', 'run'):
            raise error
    assert caught.value is error
    assert event.call_args.kwargs['status'] == 'failed'
    assert set(event.call_args.kwargs) == {'durationMs', 'status', 'workerHash', 'runHash'}


@pytest.mark.parametrize('end', [-1, float('nan'), float('inf')])
def test_admission_invalid_duration_is_not_emitted(monkeypatch, end):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    monkeypatch.setattr(service_module.time, 'monotonic_ns', Mock(side_effect=[0, end]))
    event = Mock(); monkeypatch.setattr(service_module, 'native_timing_event', event)
    with service_module._native_admission_timing('admission_commit', 'worker', 'run'):
        pass
    event.assert_not_called()


def test_admission_duration_brackets_complete_call_and_emits_after_operation(monkeypatch):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    ordering = []
    clock = Mock(side_effect=[1_000_000, 4_000_000])
    monkeypatch.setattr(service_module.time, 'monotonic_ns', clock)
    event = Mock(side_effect=lambda *_args, **_kwargs: ordering.append('observed'))
    monkeypatch.setattr(service_module, 'native_timing_event', event)
    with service_module._native_admission_timing('admission_commit', 'worker', 'run'):
        ordering.append('commit_finished')
    assert ordering == ['commit_finished', 'observed']
    assert event.call_args.kwargs['durationMs'] == 3


def test_admission_claim_uses_returned_exact_run_not_queued_hint(monkeypatch):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    event = Mock(); monkeypatch.setattr(service_module, 'native_timing_event', event)
    with service_module._native_admission_timing('admission_claim', 'worker') as identity:
        identity['run'] = 'returned-run'
        identity['untrusted-secret'] = 'must-not-serialize'
    assert event.call_args.kwargs['runHash'] == timing.native_timing_hash('run', 'returned-run')
    assert set(event.call_args.kwargs) == {'durationMs', 'status', 'workerHash', 'runHash'}
