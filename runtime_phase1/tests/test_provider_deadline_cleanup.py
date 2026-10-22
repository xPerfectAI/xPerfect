"""A provider deadline releases only its exact, proven-stopped native generation."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.openclaw_runtime import StubRuntime, RuntimeErrorBase
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


@pytest.fixture
def deadline(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'state.db'))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False,
                                    start_background_consumers=False)
    service.executor.submit = Mock()
    provider = ConversationProvider(store, service)
    project = store.create_project('owner-a', 'Deadline fixture', 'Exact cleanup', 'codex-cli')
    worker = store.create_worker(project_id=project['project_id'], owner_id='owner-a',
        name='Synthetic', role='conversation-agent', profile='codex-cli', backend='',
        runtime='codex-cli', model='gpt-5.6-sol', execution_mode='host')
    worker_id = worker['worker_id']
    store.update_worker(worker_id, state='ready', trusted_run_lane='conversation')
    session = store.upsert_provider_session(tenant_id='local', owner_id='owner-a',
        conversation_id='deadline-fixture', agent_id='agent-a', model_id='codex-cli:gpt-5.6-sol',
        project_id=project['project_id'], worker_id=worker_id, workspace_dir=str(tmp_path), access_mode='read_only')
    requests = []
    for name in ['historical', 'current']:
        request, _ = store.create_provider_request(tenant_id='local', owner_id='owner-a',
            session_id=session['session_id'], idempotency_key=name, message_id=name, stream_id=name,
            requested_history_count=1, admitted_instruction='Synthetic source', response_timeout_s=60,
            response_deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            replay_decision={'provider_session_mode': 'stateless'})
        run, _ = store.create_and_attach_provider_run(request_id=request['request_id'], worker_id=worker_id,
            project_id=project['project_id'], instruction=request['admitted_instruction'])
        if name == 'historical':
            store.update_run(run['run_id'], state='needs_input', failure_class='provider_progress_stalled')
            store.update_provider_request(request['request_id'], state='failed')
            historical = store.get_run(run['run_id'])
        else:
            claimed = store.claim_next_queued_run(worker_id, executor_id='synthetic-executor')
            assert claimed['run_id'] == run['run_id']
            lease = store.acquire_host_run_lease(runtime_family='codex', lane='conversation', tenant_id='local',
                owner_id='owner-a', worker_id=worker_id, run_id=run['run_id'], executor_id='synthetic-executor',
                conversation_limit=2, mission_limit=4, account_mission_limit=4, tenant_mission_limit=8,
                lease_ttl_s=300)
            assert store.admit_claimed_run(run['run_id'], lease_id=lease['lease_id'], executor_id='synthetic-executor')
            invoked = store.mark_run_runtime_invoked(run['run_id'], lease_id=lease['lease_id'], executor_id='synthetic-executor')
            assert store.confirm_host_run_start(worker_id=worker_id, run_id=run['run_id'],
                run_started_at=invoked['runtime_invoked_at'], lease_id=lease['lease_id'],
                startup_token=lease['startup_token'], executor_id='synthetic-executor', identity_kind='host_process',
                pid=43101, process_group=43101, process_start_identity='ps-lstart:synthetic',
                container_id='', session_id='host-exact')
            request = store.update_provider_request(request['request_id'], state='running',
                response_deadline_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        requests.append(request)
    store.update_worker(worker_id, state='running', pid=43101, compute_released_at=None)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / 'native'))
    monkeypatch.setattr(service, 'runtime', runtime)
    monkeypatch.setattr(runtime, '_pid_is_live', lambda pid: False)
    monkeypatch.setattr(runtime, '_recorded_pid_is_proven_gone', lambda pid, identity: True)
    monkeypatch.setattr(runtime, '_host_process_group_alive', lambda group: False)
    session_path = runtime._active_session_meta_path(worker_id)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps({'run_id': run['run_id'], 'session_name': 'host-exact',
        'process_pid': 43101, 'process_group': 43101, 'process_start_identity': 'ps-lstart:synthetic',
        'attempt_id': invoked['active_attempt_id']}))
    run_root = runtime._run_root(worker_id, run['run_id']); run_root.mkdir(parents=True, exist_ok=True)
    child_path = runtime._native_child_record_path(run_root, invoked['active_attempt_id'])
    child_path.write_text(json.dumps({'supervisor_pid': 43101, 'child': None})); child_path.chmod(0o600)
    try:
        yield store, service, provider, requests[-1], historical, runtime, child_path
    finally:
        provider.shutdown(); service.shutdown(); store.close()


def test_deadline_uses_exact_native_cleanup_and_preserves_failed_history(deadline):
    store, service, provider, request, historical, runtime, child = deadline
    result, run = provider._expire_response_deadline(request)
    worker = store.get_worker(run['worker_id'])
    assert result['state'] == run['state'] == 'failed'
    assert run['failure_class'] == 'provider_response_deadline_exceeded'
    assert worker['compute_released_at'] and not worker['compute_release_token']
    assert worker['pid'] is None and worker['last_error'] == ''
    assert worker['state'] == 'ready'
    assert store.worker_native_compute_is_released(run['worker_id'])
    assert store.get_run(historical['run_id']) == historical
    failed = dict(store.get_run(run['run_id']))
    assert service.release_provider_deadline_compute(run['worker_id'], run['run_id'])
    assert store.get_run(run['run_id']) == failed
    events = store.list_provider_activity(request['request_id'])
    assert json.loads(events[-1]['payload_json'])['native_cleanup'] == 'accepted'


def _queue_deadline_successor(store, request, run):
    successor_request, _ = store.create_provider_request(
        tenant_id='local', owner_id='owner-a', session_id=request['session_id'],
        idempotency_key='queued-successor', message_id='queued-successor',
        stream_id='queued-successor', requested_history_count=2,
        admitted_instruction='Preserve this queued continuation', response_timeout_s=600,
        response_deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        replay_decision={'provider_session_mode': 'stateless'},
    )
    successor, _ = store.create_and_attach_provider_run(
        request_id=successor_request['request_id'], worker_id=run['worker_id'],
        project_id=run['project_id'], instruction=successor_request['admitted_instruction'],
    )
    store.update_worker(run['worker_id'], last_run_id=successor['run_id'], state='running')
    return successor_request, successor


@pytest.mark.parametrize('already_failed', [False, True])
def test_deadline_cleanup_preserves_a_never_invoked_queued_successor(deadline, monkeypatch, already_failed):
    store, service, provider, request, historical, runtime, child = deadline
    original_release = service.release_provider_deadline_compute
    if already_failed:
        monkeypatch.setattr(service, 'release_provider_deadline_compute', lambda *_args: None)
        request, expired = provider._expire_response_deadline(request)
        monkeypatch.setattr(service, 'release_provider_deadline_compute', original_release)
    else:
        expired = store.get_run(request['run_id'])
    next_request, successor = _queue_deadline_successor(store, request, expired)
    preserved_request = dict(store.get_provider_request(next_request['request_id']))
    runtime._write_session_key(expired['worker_id'], 'synthetic-retained-context')
    native_context = json.loads(runtime._session_meta_path(expired['worker_id']).read_text())

    result, failed = provider._expire_response_deadline(request)

    worker = store.get_worker(expired['worker_id'])
    assert result['state'] == failed['state'] == 'failed'
    assert failed['failure_class'] == 'provider_response_deadline_exceeded'
    assert worker['compute_released_at'] and not worker['compute_release_token']
    assert worker['state'] == 'ready' and worker['last_run_id'] == successor['run_id']
    assert store.get_run(successor['run_id']) == successor
    assert store.get_provider_request(next_request['request_id']) == preserved_request
    retained_context = json.loads(runtime._session_meta_path(expired['worker_id']).read_text())
    assert {key: retained_context[key] for key in native_context} == native_context
    assert store.get_run(historical['run_id']) == historical
    assert runtime._read_active_session(expired['worker_id']) is None
    if already_failed:
        assert failed == expired


@pytest.mark.parametrize('guard', ['invoked', 'invoked_attempt', 'restore_hold'])
def test_deadline_cleanup_rejects_a_successor_with_execution_or_restore_authority(deadline, monkeypatch, guard):
    store, service, provider, request, historical, runtime, child = deadline
    original_release = service.release_provider_deadline_compute
    monkeypatch.setattr(service, 'release_provider_deadline_compute', lambda *_args: None)
    request, expired = provider._expire_response_deadline(request)
    monkeypatch.setattr(service, 'release_provider_deadline_compute', original_release)
    next_request, successor = _queue_deadline_successor(store, request, expired)
    if guard == 'invoked':
        store.update_run(successor['run_id'], runtime_invoked_at=datetime.now(timezone.utc).isoformat())
    elif guard == 'invoked_attempt':
        with store._connect() as connection:
            connection.execute(
                'INSERT INTO run_attempts(attempt_id,run_id,attempt_number,state,claimed_at,runtime_invoked_at) '
                'VALUES(?,?,1,?,?,?)',
                ('synthetic-invoked-successor', successor['run_id'], 'failed', 'synthetic', 'synthetic'),
            )
    else:
        store.update_provider_request(next_request['request_id'], restore_hold=1)
    before = dict(store.get_worker(expired['worker_id']))
    cleanup = Mock(side_effect=AssertionError('A successor authority boundary must remain untouched'))
    monkeypatch.setattr(runtime, 'cleanup_orphaned_run', cleanup)

    assert service.release_provider_deadline_compute(expired['worker_id'], expired['run_id']) is None

    assert not cleanup.called
    assert store.get_worker(expired['worker_id']) == before
    assert store.get_run(expired['run_id']) == expired


@pytest.mark.parametrize('race', ['claim', 'finalization'])
def test_deadline_cleanup_rechecks_queued_successor_execution_at_ownership_boundaries(deadline, monkeypatch, race):
    store, service, provider, request, historical, runtime, child = deadline
    original_release = service.release_provider_deadline_compute
    monkeypatch.setattr(service, 'release_provider_deadline_compute', lambda *_args: None)
    request, expired = provider._expire_response_deadline(request)
    monkeypatch.setattr(service, 'release_provider_deadline_compute', original_release)
    _next_request, successor = _queue_deadline_successor(store, request, expired)
    cleanup = Mock(wraps=runtime.cleanup_orphaned_run)
    monkeypatch.setattr(runtime, 'cleanup_orphaned_run', cleanup)
    if race == 'claim':
        claim = service._claim_exact_run_control
        def race_claim(*args, **kwargs):
            store.update_run(successor['run_id'], runtime_invoked_at=datetime.now(timezone.utc).isoformat())
            return claim(*args, **kwargs)
        monkeypatch.setattr(service, '_claim_exact_run_control', race_claim)
    else:
        confirm = runtime.confirm_retained_run_stopped
        def race_finalization(*args, **kwargs):
            confirm(*args, **kwargs)
            store.update_run(successor['run_id'], runtime_invoked_at=datetime.now(timezone.utc).isoformat())
        monkeypatch.setattr(runtime, 'confirm_retained_run_stopped', race_finalization)

    with pytest.raises(RuntimeErrorBase):
        service.release_provider_deadline_compute(expired['worker_id'], expired['run_id'])

    assert cleanup.call_count == (0 if race == 'claim' else 1)
    assert not store.get_worker(expired['worker_id'])['compute_released_at']
    assert store.get_run(expired['run_id']) == expired
    assert store.get_run(successor['run_id'])['runtime_invoked_at']


def test_unrecorded_child_stays_fenced_and_existing_claim_retry_recovers(deadline):
    store, service, provider, request, historical, runtime, child = deadline
    content = child.read_bytes(); child.unlink()
    _, run = provider._expire_response_deadline(request)
    worker = store.get_worker(run['worker_id'])
    assert not worker['compute_released_at'] and worker['compute_release_token']
    assert worker['last_error'] == 'GlassHive could not confirm native deadline cleanup'
    retained = dict(store.get_run(run['run_id']))
    child.write_bytes(content); child.chmod(0o600)
    # The existing expired-claim consumer must repeat proof, never clear a terminal claim blindly.
    store.update_worker(run['worker_id'], compute_release_expires_at='2000-01-01T00:00:00+00:00')
    recovered = service.recover_expired_compute_release_claims_once()
    assert any(item['target_run_id'] == run['run_id'] for item in recovered)
    assert store.get_worker(run['worker_id'])['compute_released_at']
    assert store.get_run(run['run_id']) == retained
    assert store.get_run(historical['run_id']) == historical


@pytest.mark.parametrize('guard', ['other_session', 'live_child', 'stop', 'paused', 'foreign_request', 'other_lease'])
def test_deadline_cleanup_keeps_native_and_control_boundaries(deadline, monkeypatch, guard):
    store, service, provider, request, historical, runtime, child = deadline
    worker_id = historical['worker_id']; run_id = request['run_id']
    if guard == 'other_session':
        path = runtime._active_session_meta_path(worker_id)
        data = json.loads(path.read_text()); data['run_id'] = 'run_other'; path.write_text(json.dumps(data))
    elif guard == 'live_child':
        child.write_text(json.dumps({'supervisor_pid': 43101, 'child':
            {'pid': 43102, 'process_group': 43102, 'process_start_identity': 'ps-lstart:child'}}))
        monkeypatch.setattr(runtime, '_recorded_pid_is_proven_gone', lambda pid, identity: pid != 43102)
    elif guard == 'stop':
        store.update_worker(worker_id, work_stop_id='synthetic-stop', state='stopping')
    elif guard == 'paused':
        store.update_worker(worker_id, state='paused')
    elif guard == 'foreign_request':
        store.update_provider_request(request['request_id'], owner_id='owner-other')
    else:
        other = store.create_run(worker_id, historical['project_id'], 'Other reservation', state='queued')
        store.acquire_host_run_lease(runtime_family='codex', lane='conversation', tenant_id='local',
            owner_id='owner-a', worker_id=worker_id, run_id=other['run_id'], executor_id='other-executor',
            conversation_limit=2, mission_limit=4, account_mission_limit=4, tenant_mission_limit=8, lease_ttl_s=300)
        store.update_worker(worker_id, last_run_id=run_id)
    provider._expire_response_deadline(request)
    worker = store.get_worker(worker_id)
    assert not worker['compute_released_at']
    assert store.get_run(historical['run_id']) == historical
    if guard in {'stop', 'paused'}:
        assert worker['state'] == ('stopping' if guard == 'stop' else 'paused')
    if guard == 'other_session':
        assert runtime._read_active_session(worker_id)['run_id'] == 'run_other'


def test_normal_same_session_selection_retries_cleanup(deadline, monkeypatch, tmp_path):
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest
    store, service, provider, request, historical, runtime, child = deadline
    content = child.read_bytes(); child.unlink()
    _, run = provider._expire_response_deadline(request)
    assert store.get_worker(run['worker_id'])['state'] == 'failed'
    child.write_bytes(content); child.chmod(0o600)
    retained = dict(store.get_run(run['run_id']))
    payload = ChatCompletionRequest.model_validate({'model': 'codex-cli:gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'A separate accepted turn'}],
        'metadata': {'owner_id': 'owner-a', 'conversation_id': 'deadline-fixture', 'agent_id': 'agent-a',
            'message_id': 'fresh', 'stream_id': 'fresh', 'provider_session_mode': 'stateless',
            'glasshive_options': {'workspace': {'mode': 'custom', 'path': str(tmp_path)}, 'access': 'read_only'}}})
    model = provider._model(payload.model)
    selected, _ = provider._session(payload, model, tmp_path, 'high', tenant_id='local')
    assert selected['session_id'] == request['session_id']
    assert selected['worker_id'] == run['worker_id']
    assert store.get_worker(run['worker_id'])['compute_released_at']
    assert store.get_run(run['run_id']) == retained
    assert store.get_run(historical['run_id']) == historical


def test_deadline_cleanup_rejects_changed_docker_generation(deadline, monkeypatch):
    store, service, provider, request, historical, runtime, child = deadline
    replacement = StubRuntime()
    monkeypatch.setattr(service, 'runtime', replacement)
    monkeypatch.setattr(replacement, 'compute_identity', Mock(side_effect=[
        {'container_id': 'container-old'}, {'container_id': 'container-new'}]), raising=False)
    interrupt = Mock(side_effect=AssertionError('replacement must not be interrupted'))
    monkeypatch.setattr(replacement, 'interrupt_worker', interrupt)
    store.update_worker(historical['worker_id'], execution_mode='docker')
    provider._expire_response_deadline(request)
    worker = store.get_worker(historical['worker_id'])
    assert not interrupt.called
    assert worker['compute_release_token'] and not worker['compute_released_at']
    assert worker['compute_release_container_id'] == 'container-old'


def test_slow_deadline_cleanup_does_not_hold_provider_admission_lock(deadline, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    store, service, provider, request, historical, runtime, child = deadline
    entered, proceed = Event(), Event()
    original = runtime.cleanup_orphaned_run
    def cleanup(worker, run_id):
        entered.set()
        assert proceed.wait(2)
        return original(worker, run_id)
    monkeypatch.setattr(runtime, 'cleanup_orphaned_run', cleanup)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(provider._expire_response_deadline, request)
        try:
            assert entered.wait(2)
            assert provider._start_lock.acquire(timeout=0.5)
            provider._start_lock.release()
        finally:
            proceed.set()
        assert pending.result(timeout=3)[0]['state'] == 'failed'


def _select_retained_session(provider, tmp_path, *, mode='stateless'):
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest
    payload = ChatCompletionRequest.model_validate({'model': 'codex-cli:gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'A separate accepted turn'}],
        'metadata': {'owner_id': 'owner-a', 'conversation_id': 'deadline-fixture', 'agent_id': 'agent-a',
            'message_id': 'fresh', 'stream_id': 'fresh', 'provider_session_mode': mode,
            'glasshive_options': {'workspace': {'mode': 'custom', 'path': str(tmp_path)}, 'access': 'read_only'}}})
    return provider._session(payload, provider._model(payload.model), tmp_path, 'high', tenant_id='local')


@pytest.mark.parametrize('error', [ValueError, OSError])
def test_retry_cleanup_errors_keep_fenced_conflict_without_private_error_text(
    deadline, monkeypatch, tmp_path, caplog, error,
):
    from fastapi import HTTPException
    store, service, provider, request, historical, runtime, child = deadline
    child.unlink()
    _, run = provider._expire_response_deadline(request)
    before = store.get_worker(run['worker_id'])
    def unavailable(*args):
        raise error('synthetic private detail must not be logged')
    monkeypatch.setattr(service, 'release_provider_deadline_compute', unavailable)
    with pytest.raises(HTTPException) as raised:
        _select_retained_session(provider, tmp_path)
    assert raised.value.status_code == 409
    assert raised.value.detail['code'] == 'conversation_session_authority_conflict'
    assert store.get_worker(run['worker_id']) == before
    assert 'synthetic private detail' not in caplog.text
    assert error.__name__ in caplog.text


@pytest.mark.parametrize('mode', ['stateless', 'persistent'])
def test_deadline_success_preserves_binding_and_native_continuity_for_next_turn(
    deadline, monkeypatch, tmp_path, mode,
):
    store, service, provider, request, historical, runtime, child = deadline
    if mode == 'persistent':
        # Persistent sessions retain their existing ordering fence for needs_input;
        # this case has an ordinary completed predecessor and one expired current run.
        store.update_run(historical['run_id'], state='completed')
    store.update_provider_request(request['request_id'], replay_decision_json=json.dumps({'provider_session_mode': mode}))
    store.update_provider_session_history(request['session_id'], history_count=2,
        context_manifest={'native_context_epoch': 'synthetic-retained-epoch'})
    native_context = runtime._session_meta_path(historical['worker_id'])
    native_context.parent.mkdir(parents=True, exist_ok=True)
    runtime._write_session_key(historical['worker_id'], 'synthetic-retained-native-session')
    preserved = {key: json.loads(native_context.read_text())[key] for key in ('session_key', 'context_epoch', 'runtime')}
    result, run = provider._expire_response_deadline(request)
    assert store.get_worker(run['worker_id'])['compute_released_at']
    retained = dict(store.get_run(run['run_id']))
    monkeypatch.setattr(service, 'terminate_worker', Mock(side_effect=AssertionError('cleanup must not rebind the session')))
    selected, changed = _select_retained_session(provider, tmp_path, mode=mode)
    assert selected['session_id'] == request['session_id'] and selected['worker_id'] == run['worker_id']
    assert not changed and selected['history_count'] == 2
    assert json.loads(selected['context_manifest_json'])['native_context_epoch'] == 'synthetic-retained-epoch'
    assert {key: json.loads(native_context.read_text())[key] for key in preserved} == preserved
    next_request, _ = store.create_provider_request(tenant_id='local', owner_id='owner-a',
        session_id=request['session_id'], idempotency_key='next-after-deadline', message_id='next', stream_id='next',
        requested_history_count=3, admitted_instruction='Synthetic next source',
        replay_decision={'provider_session_mode': mode})
    successor, _ = store.create_and_attach_provider_run(request_id=next_request['request_id'],
        worker_id=run['worker_id'], project_id=run['project_id'], instruction=next_request['admitted_instruction'])
    accepted = store.claim_next_queued_run(run['worker_id'], executor_id='synthetic-next-executor')
    assert accepted and accepted['run_id'] == successor['run_id']
    assert store.get_run(run['run_id']) == retained


def test_stop_request_during_unproved_deadline_cleanup_finishes_after_safe_recovery(deadline):
    store, service, provider, request, historical, runtime, child = deadline
    content = child.read_bytes(); child.unlink()
    _, expired = provider._expire_response_deadline(request)
    worker_id = expired['worker_id']; original_claim = store.get_worker(worker_id)['compute_release_token']
    store.update_worker(worker_id, compute_release_expires_at='2000-01-01T00:00:00+00:00')
    # The normal Stop owner cannot replace a different unresolved generation claim.
    waiting = service.stop_run(worker_id, historical['run_id'])
    assert not waiting['accepted'] and not waiting['confirmation_pending']
    assert store.get_worker(worker_id)['compute_release_token'] == original_claim
    assert not store.get_worker(worker_id)['work_stop_id']
    # Exact native proof permits the existing recovery owner to settle its own claim.
    child.write_bytes(content); child.chmod(0o600)
    assert service.recover_expired_compute_release_claims_once()
    recovered = store.get_worker(worker_id)
    assert recovered['compute_released_at'] and not recovered['compute_release_token']
    stopped = service.stop_run(worker_id, historical['run_id'])
    assert stopped['accepted'] and not stopped['confirmation_pending']
    worker = store.get_worker(worker_id)
    assert worker['work_stop_id'] and worker['work_stop_settled_at']
    assert not worker['compute_release_token']
    assert store.get_run(expired['run_id']) == expired
