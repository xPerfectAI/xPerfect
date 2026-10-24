"""Foreground capacity admission and terminal cause retain native ownership fences."""
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from workers_projects_runtime.conversation_provider import ChatCompletionRequest
from workers_projects_runtime.openclaw_runtime import HostCapacityError
from workers_projects_runtime.service import HostResourceUsage
from test_conversation_provider import _client, _payload, AUTH, lifecycle_owned_test_clients


def low_disk(monkeypatch):
    from workers_projects_runtime import service
    monkeypatch.setattr(service, 'host_resource_usage', lambda _leases: HostResourceUsage(
        child_processes=0, threads=0, available_memory_bytes=16 * 1024**3,
        available_disk_bytes=2 * 1024**3,
    ))


def warmed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = _payload(tmp_path)
    response = client.post('/v1/chat/completions', headers=AUTH, json=body)
    assert response.status_code == 200, response.text
    store = client.app.state.store
    record = store.get_provider_request(response.json()['id'])
    return client, body, record, response.json()


def next_payload(body):
    body = json.loads(json.dumps(body))
    body['messages'].append({'role': 'user', 'content': 'Keep the exact new source.'})
    body['metadata'].update(message_id='message-b', stream_id='stream-b', idempotency_key='idem-b')
    return body


def test_warm_new_foreground_capacity_rejection_has_no_native_admission_or_cursor_effects(tmp_path, monkeypatch):
    client, body, first, _ = warmed(tmp_path, monkeypatch)
    store, provider = client.app.state.store, client.app.state.conversation_provider
    session_before = store.get_provider_session_by_id(first['session_id'])
    worker_before = store.get_worker(session_before['worker_id'])
    with store._connect() as conn:
        counts_before = [conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                         for table in ['provider_requests', 'runs', 'provider_session_visible_admissions']]
    low_disk(monkeypatch)
    body = next_payload(body)
    body['metadata'].update(native_invocation_id='a' * 64, native_body_sha256='b' * 64)
    with pytest.raises(HostCapacityError) as rejected:
        provider.start(ChatCompletionRequest.model_validate(body))
    assert rejected.value.capacity_class == 'resource_pressure'
    assert rejected.value.shortage['diskBytes'] == 2 * 1024**3
    assert rejected.value.native_admission == {
        'version': 1, 'accepted': False, 'invocation_id': 'a' * 64, 'body_sha256': 'b' * 64,
        'stream_id': 'stream-b', 'message_id': 'message-b',
    }
    assert store.get_provider_session_by_id(first['session_id']) == session_before
    worker_after = store.get_worker(session_before['worker_id'])
    for field in ['bootstrap_bundle_json', 'model', 'workspace_dir', 'last_run_id']:
        assert worker_after[field] == worker_before[field]
    raw_body = json.dumps(body, separators=(',', ':'))
    digest = hashlib.sha256(raw_body.encode()).hexdigest()
    response = client.post('/v1/chat/completions', headers={
        **AUTH, 'Content-Type': 'application/json',
        'X-Viventium-Native-Invocation-Id': 'a' * 64,
        'X-Viventium-Native-Body-SHA256': digest,
    }, content=raw_body)
    assert response.status_code == 503, response.text
    assert int(response.headers['Retry-After']) >= 1
    error = response.json()['error']
    assert error['code'] == 'host_capacity' and error['capacityClass'] == 'resource_pressure'
    assert error['native_admission'] == {**rejected.value.native_admission, 'body_sha256': digest}
    assert error['available']['diskBytes'] == 2 * 1024**3
    assert error['required']['diskBytes'] == 4 * 1024**3
    assert error['shortage']['diskBytes'] == 2 * 1024**3
    with store._connect() as conn:
        assert [conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ['provider_requests', 'runs', 'provider_session_visible_admissions']] == counts_before


def test_warm_new_foreground_checks_cached_resources_without_new_reservation(tmp_path, monkeypatch):
    client, body, first, _ = warmed(tmp_path, monkeypatch)
    service, store = client.app.state.service, client.app.state.store
    original = service._host_resource_capacity_error
    seen = []
    def observed(*args, **kwargs):
        seen.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(service, '_host_resource_capacity_error', observed)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('A warm foreground turn must not reserve a CLI preflight slot')
    monkeypatch.setattr(store, 'acquire_preflight_capacity_reservation', forbidden)
    response = client.post('/v1/chat/completions', headers=AUTH, json=next_payload(body))
    assert response.status_code == 200, response.text
    assert seen and seen[0] == ((), {'docker_cached_only': True})
    assert all(kwargs.get('docker_cached_only') is True for _, kwargs in seen)


def test_completed_replay_still_returns_its_result_under_low_capacity(tmp_path, monkeypatch):
    client, body, first, result = warmed(tmp_path, monkeypatch)
    low_disk(monkeypatch)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('An accepted replay must not re-enter foreground capacity admission')
    monkeypatch.setattr(client.app.state.service, '_host_resource_capacity_error', forbidden)
    response = client.post('/v1/chat/completions', headers=AUTH, json=body)
    assert response.status_code == 200, response.text
    assert response.json() == result
    assert response.json()['id'] == first['request_id']


def test_accepted_replay_does_not_reenter_capacity_admission(tmp_path, monkeypatch, background_consumers_disabled):
    client = _client(tmp_path, monkeypatch)
    provider, service = client.app.state.conversation_provider, client.app.state.service
    monkeypatch.setattr(service, 'start_assigned_run', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider, '_ensure_detached_reconciliation', lambda *_args: None)
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path))
    first = provider.start(payload)
    low_disk(monkeypatch)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('An accepted replay must retain its existing admission')
    monkeypatch.setattr(service, '_host_resource_capacity_error', forbidden)
    replay = provider.start(payload)
    assert replay['request_id'] == first['request_id'] and replay['run_id'] == first['run_id']
    assert replay['admitted_instruction'] == first['admitted_instruction']


def pending_direct_client(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv('GLASSHIVE_ALLOWED_WORKER_PROFILES', 'codex-cli,claude-code,direct-xai')
    provider, store, service = client.app.state.conversation_provider, client.app.state.store, client.app.state.service
    monkeypatch.setattr(service, 'start_assigned_run', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider, '_ensure_detached_reconciliation', lambda *_args: None)
    body = _payload(tmp_path, model='direct-xai:grok-4.7')
    return client, provider, store, service, body


def completed_direct_session(provider, store, body):
    first = provider.start(ChatCompletionRequest.model_validate(body))
    store.update_run(first['run_id'], state='completed', ended_at=datetime.now(UTC).isoformat(), output_text='Stored result.')
    store.update_provider_request(first['request_id'], state='completed')
    session = store.get_provider_session_by_id(first['session_id'])
    store.update_worker(session['worker_id'], state='ready')
    return first


@pytest.mark.parametrize('binding', ['cold', 'warm', 'rebind'])
def test_direct_disk_guard_precedes_cold_warm_and_rebind_effects(tmp_path, monkeypatch, background_consumers_disabled, binding):
    _, provider, store, service, body = pending_direct_client(tmp_path, monkeypatch)
    if binding != 'cold':
        completed_direct_session(provider, store, body)
        body = next_payload(body)
        if binding == 'rebind':
            body['messages'][0]['content'] = 'Use the revised standing authority.'
    body['metadata'].update(native_invocation_id='c' * 64, native_body_sha256='d' * 64)
    with store._connect() as conn:
        tables = ['projects', 'workers', 'provider_sessions', 'provider_requests', 'runs',
                  'provider_session_visible_admissions', 'preflight_capacity_reservations']
        counts = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables}
    sessions = store.list_provider_sessions(owner_id='owner-a')
    workers = [store.get_worker(x['worker_id']) for x in sessions]
    low_disk(monkeypatch)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Measured resource pressure must precede all session lifecycle effects')
    session = provider._session
    monkeypatch.setattr(provider, '_session', forbidden)
    with pytest.raises(HostCapacityError) as rejected:
        provider.start(ChatCompletionRequest.model_validate(body))
    assert rejected.value.capacity_class == 'resource_pressure'
    assert rejected.value.available['diskBytes'] == 2 * 1024**3
    assert rejected.value.native_admission == {
        'version': 1, 'accepted': False, 'invocation_id': 'c' * 64, 'body_sha256': 'd' * 64,
        'stream_id': body['metadata']['stream_id'], 'message_id': body['metadata']['message_id'],
    }
    assert store.list_provider_sessions(owner_id='owner-a') == sessions
    assert [store.get_worker(x['worker_id']) for x in sessions] == workers
    with store._connect() as conn:
        assert {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables} == counts
    # Recovery admits this same source through the normal owner after headroom returns.
    from workers_projects_runtime import service as service_module
    monkeypatch.setattr(service_module, 'host_resource_usage', lambda _leases: HostResourceUsage(
        child_processes=0, threads=0, available_memory_bytes=16 * 1024**3, available_disk_bytes=64 * 1024**3))
    monkeypatch.setattr(provider, '_session', session)
    recovered = provider.start(ChatCompletionRequest.model_validate(body))
    assert recovered['state'] == 'queued'
    assert provider.start(ChatCompletionRequest.model_validate(body))['request_id'] == recovered['request_id']


def synthetic_lease(store, service, *, family='direct-xai', mode='host', name='held'):
    project = store.create_project('owner-a', name, 'Synthetic capacity holder', 'codex-cli')
    worker = store.create_worker(project_id=project['project_id'], owner_id='owner-a', name=name,
        role='conversation-agent', profile=family, backend='', runtime=family,
        model='synthetic-model', execution_mode=mode, trusted_run_lane='conversation')
    run = store.create_run(worker['worker_id'], project['project_id'], 'Synthetic held work', state='queued')
    lease = store.acquire_host_run_lease(runtime_family=family, lane='conversation', tenant_id='local',
        owner_id='owner-a', worker_id=worker['worker_id'], run_id=run['run_id'], executor_id=service._executor_id,
        conversation_limit=2, mission_limit=4, account_mission_limit=4, tenant_mission_limit=8, lease_ttl_s=300)
    return worker, run, lease


def test_two_occupied_conversation_slots_keep_new_warm_turn_queued(tmp_path, monkeypatch, background_consumers_disabled):
    _, provider, store, service, body = pending_direct_client(tmp_path, monkeypatch)
    completed_direct_session(provider, store, body)
    holds = [synthetic_lease(store, service, name=f'held-{i}') for i in range(2)]
    record = provider.start(ChatCompletionRequest.model_validate(next_payload(body)))
    run = store.get_run(record['run_id'])
    worker = store.get_worker(run['worker_id'])
    assert record['state'] == run['state'] == 'queued'
    with pytest.raises(HostCapacityError) as blocked:
        service._acquire_host_run_lease(worker, run)
    assert blocked.value.capacity_class == 'family_lane'
    assert store.get_provider_request(record['request_id'])['state'] == 'queued'
    assert store.get_run(record['run_id'])['state'] == 'queued'
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM preflight_capacity_reservations').fetchone()[0] == 0
    # The same accepted run can acquire its ordinary native slot after one holder releases.
    store.release_host_run_lease(holds[0][2]['lease_id'], executor_id=service._executor_id, reason='synthetic_completed')
    admitted = service._acquire_host_run_lease(worker, run)
    assert admitted['run_id'] == record['run_id']
    assert store.get_provider_request(record['request_id'])['admitted_instruction'] == record['admitted_instruction']


def test_active_docker_worker_uses_only_cached_probe_and_keeps_unavailable_probe_queued(tmp_path, monkeypatch, background_consumers_disabled):
    _, provider, store, service, body = pending_direct_client(tmp_path, monkeypatch)
    completed_direct_session(provider, store, body)
    synthetic_lease(store, service, family='codex', mode='docker')
    probes = []
    def cached_probe(*, cached_only=False):
        probes.append(cached_only)
        assert cached_only is True, 'Foreground admission must not run an uncached Docker probe'
        return None
    monkeypatch.setattr(service.runtime, 'isolated_resource_usage', cached_probe)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Foreground resource inspection must not reserve a preflight slot')
    monkeypatch.setattr(store, 'acquire_preflight_capacity_reservation', forbidden)
    record = provider.start(ChatCompletionRequest.model_validate(next_payload(body)))
    assert probes == [True]
    assert record['state'] == store.get_run(record['run_id'])['state'] == 'queued'


@pytest.mark.parametrize('native_started', [False, True, 'race', 'attempt'])
def test_capacity_wait_deadline_preserves_cause_only_before_native_start(tmp_path, monkeypatch, native_started, background_consumers_disabled):
    client = _client(tmp_path, monkeypatch)
    provider, store, service = client.app.state.conversation_provider, client.app.state.store, client.app.state.service
    monkeypatch.setattr(service, 'start_assigned_run', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider, '_ensure_detached_reconciliation', lambda *_args: None)
    record = provider.start(ChatCompletionRequest.model_validate(_payload(tmp_path)))
    snapshot = {'diskBytes': 2 * 1024**3}
    required = {'diskBytes': 4 * 1024**3}
    # Populate the durable observation directly: update_run correctly rejects synthetic
    # transitions into running without a real lease/generation. No native process runs here.
    with store._connect() as conn:
        conn.execute("""UPDATE runs SET state=?, started_at=?, active_attempt_id=?,
                     failure_class='host_capacity', capacity_class='resource_pressure',
                     capacity_available_json=?, capacity_required_json=?, capacity_shortage_json=?
                     WHERE run_id=?""",
                     ('running' if native_started is True else 'queued',
                      datetime.now(UTC).isoformat() if native_started is True else None,
                      'synthetic-attempt' if native_started == 'attempt' else '',
                      json.dumps(snapshot), json.dumps(required), json.dumps(snapshot), record['run_id']))
        conn.execute('UPDATE provider_requests SET response_timeout_s=30, response_deadline_at=? WHERE request_id=?',
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), record['request_id']))
    if native_started == 'race':
        arbitrate = store.arbitrate_provider_request_deadline
        def start_before_transaction(*args, **kwargs):
            if store.get_run(record['run_id'])['state'] == 'queued':
                with store._connect() as conn:
                    conn.execute("UPDATE runs SET state='running', started_at=? WHERE run_id=?",
                                 (datetime.now(UTC).isoformat(), record['run_id']))
            return arbitrate(*args, **kwargs)
        monkeypatch.setattr(store, 'arbitrate_provider_request_deadline', start_before_transaction)
    releases = []
    monkeypatch.setattr(service, 'release_provider_deadline_compute', lambda worker_id, run_id:
                        releases.append((worker_id, run_id)) or True)
    request, run = provider._expire_response_deadline(record)
    expected = 'provider_response_deadline_exceeded' if native_started else 'host_capacity'
    assert request['state'] == 'failed'
    assert request['fallback_state'] == 'deadline_exceeded'
    assert run['state'] == 'failed'
    assert run['failure_class'] == expected
    if not native_started:
        assert json.loads(run['capacity_available_json']) == snapshot
        assert json.loads(run['capacity_required_json']) == required
    assert bool(releases) == bool(native_started)
    error = provider.deadline_error_payload(request, run)['error']
    assert error['code'] == expected
    if not native_started:
        assert error['capacityClass'] == 'resource_pressure'
        assert error['available'] == snapshot
        assert error['required'] == required
        assert error['shortage'] == snapshot
        # Exercise the actual stored-failure HTTP and streaming protocol consumers.
        response = client.post('/v1/chat/completions', headers=AUTH, json=_payload(tmp_path))
        assert response.status_code == 503
        for field in ['code', 'capacityClass', 'available', 'required', 'shortage']:
            assert response.json()['error'][field] == error[field]
        from workers_projects_runtime.conversation_provider import _responses_stream, ResponsesRequest
        class Connected:
            async def is_disconnected(self):
                return False
        chat = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))
        async def streams():
            chat_frames = [row async for row in provider._stream_chunks(request, chat, Connected())]
            responses_frames = [row async for row in _responses_stream(provider, request,
                ResponsesRequest(model=chat.model, input='Current goal.'), chat, Connected())]
            return chat_frames, responses_frames
        chat_frames, responses_frames = asyncio.run(streams())
        chat_events = [json.loads(row[6:]) for row in chat_frames if row.startswith('data: {')]
        stream_error = next(row['error'] for row in chat_events if row.get('error'))
        responses_events = [json.loads(line[6:]) for row in responses_frames for line in row.splitlines()
                            if line.startswith('data: {')]
        response_error = next(row for row in responses_events if row['type'] == 'error')
        failed_error = next(row['response']['error'] for row in responses_events if row['type'] == 'response.failed')
        for protocol_error in [stream_error, response_error, failed_error]:
            for field in ['code', 'capacityClass', 'available', 'required', 'shortage']:
                assert protocol_error[field] == error[field]
    activities = store.list_provider_activity(record['request_id'])
    assert json.loads(next(x for x in activities if x['event_type'] == 'failed')['payload_json'])['failure_class'] == expected
    again, rerun = provider._expire_response_deadline(request)
    assert again['state'] == 'failed' and rerun['failure_class'] == expected


def test_capacity_error_metadata_is_bounded_to_declared_numeric_dimensions():
    from workers_projects_runtime.conversation_provider import _openai_error
    response = _openai_error(503, 'Capacity is unavailable.', 'host_capacity', capacity_detail={
        'capacityClass': 'resource_pressure', 'reservation': {'private': 'discard'},
        'available': {'diskBytes': 42, 'memoryBytes': True, 'privatePath': '/private',
                      'threads': float('inf'), 'childProcesses': -1},
        'required': {'diskBytes': 64}, 'shortage': {'diskBytes': 22},
    })
    assert json.loads(response.body)['error'] == {
        'message': 'Capacity is unavailable.', 'type': 'server_error', 'param': None, 'code': 'host_capacity',
        'capacityClass': 'resource_pressure', 'available': {'diskBytes': 42},
        'required': {'diskBytes': 64}, 'shortage': {'diskBytes': 22},
    }


def stale_docker_snapshot(**extra):
    # Exact zero/false shape returned by the production cached adapter when its snapshot is absent.
    return {
        'child_processes': 0, 'threads': 0, 'available_memory_bytes': 0, 'available_disk_bytes': 0,
        'running_worker_containers': 0, 'running_worker_ids': [],
        'process_probe_ok': False, 'memory_probe_ok': False, 'disk_probe_ok': False, **extra,
    }


def host_usage(monkeypatch, *, memory=16 * 1024**3, disk=64 * 1024**3, healthy=True):
    from workers_projects_runtime import service as service_module
    monkeypatch.setattr(service_module, 'host_resource_usage', lambda _leases: HostResourceUsage(
        child_processes=0, threads=0, available_memory_bytes=memory, available_disk_bytes=disk,
        process_probe_ok=healthy, memory_probe_ok=healthy, disk_probe_ok=healthy))


@pytest.mark.parametrize('host_state', ['healthy', 'disk', 'memory', 'unknown'])
def test_stale_docker_zeros_do_not_hide_or_invent_host_shortage(tmp_path, monkeypatch, background_consumers_disabled, host_state):
    _, _, store, service, _ = pending_direct_client(tmp_path, monkeypatch)
    synthetic_lease(store, service, family='codex', mode='docker')
    memory = 1024**3 if host_state == 'memory' else 16 * 1024**3
    disk = 2 * 1024**3 if host_state == 'disk' else 64 * 1024**3
    host_usage(monkeypatch, memory=memory, disk=disk, healthy=host_state != 'unknown')
    probes = []
    def cached(*, cached_only=False):
        probes.append(cached_only)
        return stale_docker_snapshot()
    monkeypatch.setattr(service.runtime, 'isolated_resource_usage', cached)
    error, snapshot = service._host_resource_capacity_error(docker_cached_only=True, _include_snapshot=True)
    assert probes == [True]
    assert error.capacity_class == ('resource_pressure' if host_state in {'disk', 'memory'} else 'resource_probe_unavailable')
    assert error.probe_error_code == '' and error.probe_transient is False
    if host_state in {'disk', 'memory'}:
        assert error.available['diskBytes'] == disk and error.available['memoryBytes'] == memory
        assert error.required['diskBytes'] == 4 * 1024**3 and error.required['memoryBytes'] == 2 * 1024**3
        assert error.shortage['diskBytes'] == (2 * 1024**3 if host_state == 'disk' else 0)
        assert error.shortage['memoryBytes'] == (1024**3 if host_state == 'memory' else 0)
        assert snapshot['available'] == error.available
        assert all(value == 0 for value in snapshot['reservation'].values())


@pytest.mark.parametrize('dimension', ['disk', 'memory'])
def test_proven_host_deficit_with_stale_docker_rejects_before_foreground_effects(tmp_path, monkeypatch, background_consumers_disabled, dimension):
    _, provider, store, service, body = pending_direct_client(tmp_path, monkeypatch)
    synthetic_lease(store, service, family='codex', mode='docker')
    body['metadata'].update(native_invocation_id='e' * 64, native_body_sha256='f' * 64)
    host_usage(monkeypatch, memory=1024**3 if dimension == 'memory' else 16 * 1024**3,
               disk=2 * 1024**3 if dimension == 'disk' else 64 * 1024**3)
    def cached(*, cached_only=False):
        assert cached_only is True
        return stale_docker_snapshot()
    monkeypatch.setattr(service.runtime, 'isolated_resource_usage', cached)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Proven host pressure must be rejected before session/source admission')
    monkeypatch.setattr(provider, '_session', forbidden)
    with store._connect() as conn:
        tables = ['projects', 'workers', 'provider_sessions', 'provider_requests', 'runs',
                  'provider_session_visible_admissions', 'preflight_capacity_reservations']
        before = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables}
    with pytest.raises(HostCapacityError) as rejected:
        provider.start(ChatCompletionRequest.model_validate(body))
    error = rejected.value
    assert error.capacity_class == 'resource_pressure'
    assert error.native_admission['accepted'] is False
    assert error.native_admission['invocation_id'] == 'e' * 64
    assert error.shortage['diskBytes'] == (2 * 1024**3 if dimension == 'disk' else 0)
    assert error.shortage['memoryBytes'] == (1024**3 if dimension == 'memory' else 0)
    with store._connect() as conn:
        assert {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables} == before


def test_healthy_host_with_production_stale_docker_remains_queued(tmp_path, monkeypatch, background_consumers_disabled):
    _, provider, store, service, body = pending_direct_client(tmp_path, monkeypatch)
    synthetic_lease(store, service, family='codex', mode='docker')
    host_usage(monkeypatch)
    def cached(*, cached_only=False):
        assert cached_only is True
        return stale_docker_snapshot()
    monkeypatch.setattr(service.runtime, 'isolated_resource_usage', cached)
    record = provider.start(ChatCompletionRequest.model_validate(body))
    assert record['state'] == store.get_run(record['run_id'])['state'] == 'queued'
    worker = store.get_worker(store.get_run(record['run_id'])['worker_id'])
    with pytest.raises(HostCapacityError) as waiting:
        service._acquire_host_run_lease(worker, store.get_run(record['run_id']))
    assert waiting.value.capacity_class == 'resource_probe_unavailable'
    assert not store.get_active_host_run_lease_for_run(record['run_id'])


@pytest.mark.parametrize('probe_code', ['shared_substrate_proof_unavailable', '/private/synthetic-probe'])
def test_stale_probe_keeps_only_scoped_valid_code_and_public_numeric_fields(tmp_path, monkeypatch, background_consumers_disabled, probe_code):
    from workers_projects_runtime.conversation_provider import _openai_error
    _, _, store, service, _ = pending_direct_client(tmp_path, monkeypatch)
    worker, _, _ = synthetic_lease(store, service, family='codex', mode='docker')
    host_usage(monkeypatch)
    monkeypatch.setattr(service.runtime, 'isolated_resource_usage', lambda **_: stale_docker_snapshot(
        probe_error_code=probe_code, probe_transient=True, raw_error='/private/synthetic-secret'))
    error, snapshot = service._host_resource_capacity_error(worker, docker_cached_only=True, _include_snapshot=True)
    assert error.capacity_class == 'resource_probe_unavailable'
    expected = probe_code if probe_code == 'shared_substrate_proof_unavailable' else ''
    assert error.probe_error_code == expected
    foreground = service._host_resource_capacity_error(docker_cached_only=True)
    assert foreground.probe_error_code == '' and foreground.probe_transient is False
    public = json.loads(_openai_error(503, 'Capacity is unavailable.', 'host_capacity',
        capacity_detail={'capacityClass': error.capacity_class, **snapshot}).body)['error']
    assert set(public) <= {'message', 'type', 'param', 'code', 'capacityClass', 'available', 'required', 'shortage'}
    assert 'synthetic-secret' not in json.dumps(public) and 'synthetic-probe' not in json.dumps(public)
