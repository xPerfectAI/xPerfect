from __future__ import annotations

import json

import pytest

from test_account_api import account_client, account_headers, delegation_payload_with_origin
from workers_projects_runtime.store import CallbackIntentGenerationConflictError


def accepted_run(client):
    service, store = client.app.state.service, client.app.state.store
    service.executor.submit = lambda *args, **kwargs: None
    response = client.post('/v1/delegations', headers=account_headers(idempotency_key='accepted-input'), json=delegation_payload_with_origin())
    assert response.status_code == 202, response.text
    work_ref = response.json()['workRef']
    record = store.get_delegation(work_ref, tenant_id='tenant-a', owner_id='owner-a')
    return service, store, record


def emit_terminal(service, store, record, *, supplied=None, persist=True):
    run = store.get_run(record['current_run_id'])
    if run['state'] == 'queued':
        run = store.finalize_run_if_state(run['run_id'], 'queued', 'completed', output_text='The requested observation is ready.')
    return service._emit_callback(store.get_worker(record['worker_id']), 'run.completed', run=supplied or run, submit_delivery=False, persist_callback=persist)


@pytest.mark.parametrize('action', [None, 'queue', 'message', 'steer'])
def test_callback_preserves_exact_accepted_input_and_continuation(account_client, action):
    service, store, record = accepted_run(account_client)
    prior = store.get_run(record['current_run_id'])
    if action is not None:
        if action != 'steer':
            store.finalize_run(prior['run_id'], 'completed', output_text='Prior observation')
            store.update_worker_state(record['worker_id'], 'ready')
        response = account_client.post(f"/v1/work/{record['work_ref']}/actions", headers=account_headers(), json={
            'action': action, 'instruction': 'Compare the current chart. Preserve the source file. Café.', 'idempotencyKey': 'current-follow-up',
        })
        assert response.status_code == 202, response.text
        record = store.get_delegation(record['work_ref'], tenant_id='tenant-a', owner_id='owner-a')
    exact = store.get_run(record['current_run_id'])
    callback = emit_terminal(service, store, record)
    payload = json.loads(callback['payload_json'])
    expected = {'version': 1, 'run_id': exact['run_id'], 'instruction': exact['instruction']}
    if action is not None:
        expected['continuation_context'] = json.loads(exact['continuation_context_json'])
        assert expected['continuation_context']['base_instruction'] == prior['instruction']
        assert expected['continuation_context']['guidance'][-1].endswith('Café.')
    assert payload['run_input'] == expected
    replay = emit_terminal(service, store, record)
    assert replay['payload_json'] == callback['payload_json']
    assert replay['callback_id'] == callback['callback_id']


def test_callback_ignores_caller_forged_input_and_rejects_stale_terminal(account_client):
    service, store, record = accepted_run(account_client)
    exact = store.finalize_run_if_state(record['current_run_id'], 'queued', 'completed', output_text='Useful result')
    forged = {**exact, 'instruction': 'Forged input', 'continuation_context_json': '{"forged":true}', 'run_input': {'instruction': 'Forged input'}}
    payload = json.loads(emit_terminal(service, store, record, supplied=forged)['payload_json'])
    assert payload['run_input'] == {'version': 1, 'run_id': exact['run_id'], 'instruction': exact['instruction']}
    assert emit_terminal(service, store, record, supplied={**exact, 'active_attempt_id': 'stale-attempt'}) is None


@pytest.mark.parametrize('field', ['run_id', 'instruction', 'continuation_context'])
def test_store_rejects_forged_or_stale_input_before_outbox_insert(account_client, field):
    service, store, record = accepted_run(account_client)
    intent = emit_terminal(service, store, record, persist=False)
    exact = store.get_run(record['current_run_id'])
    payload = json.loads(intent['payload_json'])
    if field == 'continuation_context':
        payload['run_input'][field] = {'version': 1, 'base_instruction': 'Foreign base', 'guidance': []}
    else:
        payload['run_input'][field] = 'foreign-input'
    with pytest.raises(CallbackIntentGenerationConflictError, match='accepted run input'):
        store.insert_terminal_callback_outbox_if_current(
            **{key: value for key, value in intent.items() if key != 'tenant_id' and key != 'payload_json'},
            payload_json=json.dumps(payload), expected_state=exact['state'], expected_ended_at=exact['ended_at'],
            expected_attempt_id=exact['active_attempt_id'] or '', expected_result_revision=exact['terminal_result_revision'],
            expected_result_digest=store.terminal_result_digest(exact),
        )
    assert store.get_callback_outbox(intent['callback_id']) is None


def test_existing_callback_without_run_input_remains_byte_exact(account_client):
    service, store, record = accepted_run(account_client)
    intent = emit_terminal(service, store, record, persist=False)
    exact = store.get_run(record['current_run_id'])
    payload = json.loads(intent['payload_json'])
    payload.pop('run_input', None)
    old = store.insert_terminal_callback_outbox_if_current(
        **{key: value for key, value in intent.items() if key != 'tenant_id' and key != 'payload_json'},
        payload_json=json.dumps(payload, ensure_ascii=False), expected_state=exact['state'], expected_ended_at=exact['ended_at'],
        expected_attempt_id=exact['active_attempt_id'] or '', expected_result_revision=exact['terminal_result_revision'],
        expected_result_digest=store.terminal_result_digest(exact),
    )
    replay = emit_terminal(service, store, record)
    assert replay['payload_json'] == old['payload_json']
    assert 'run_input' not in json.loads(replay['payload_json'])


def emit_needs_input(service, store, record, *, supplied=None, persist=True, callback_id=""):
    run = store.get_run(record['current_run_id'])
    return service._emit_callback(store.get_worker(record['worker_id']), 'run.needs_input',
        run=supplied or run, callback_id=callback_id, insert_once=bool(callback_id),
        submit_delivery=False, persist_callback=persist)


def insert_attention_intent(store, intent):
    return store.insert_callback_outbox_once(**{key: intent[key] for key in (
        'callback_id', 'project_id', 'worker_id', 'run_id', 'attempt_number',
        'event_type', 'url', 'payload_json')})


@pytest.mark.parametrize('continuation', [False, True])
def test_current_needs_input_callback_carries_exact_accepted_goal(account_client, continuation):
    service, store, record = accepted_run(account_client)
    prior = store.get_run(record['current_run_id'])
    if continuation:
        store.finalize_run(prior['run_id'], 'completed', output_text='Prior observation')
        store.update_worker_state(record['worker_id'], 'ready')
        response = account_client.post(f"/v1/work/{record['work_ref']}/actions", headers=account_headers(), json={
            'action': 'message', 'instruction': 'Add an accessible sign-off. Keep original constraints.',
            'idempotencyKey': 'accepted-attention-continuation',
        })
        assert response.status_code == 202, response.text
        record = store.get_delegation(record['work_ref'], tenant_id='tenant-a', owner_id='owner-a')
    exact = store.update_run(record['current_run_id'], state='needs_input',
        failure_class='capability_authorization_horizon_expired',
        failure_user_message='The deferred capability authorization has expired.')
    callback = emit_needs_input(service, store, record, supplied={**exact, 'instruction': 'Caller forged input'})
    payload = json.loads(callback['payload_json'])
    expected = {'version': 1, 'run_id': exact['run_id'], 'instruction': exact['instruction']}
    if continuation:
        expected['continuation_context'] = json.loads(exact['continuation_context_json'])
        assert expected['continuation_context']['base_instruction'] == prior['instruction']
    assert payload['run_input'] == expected
    assert payload['work_terminal'] is False
    assert payload.get('result_revision', 0) == 0
    assert not payload.get('result_digest')
    assert not exact['runtime_invoked_at']


@pytest.mark.parametrize('field', ['run_id', 'instruction', 'continuation_context'])
def test_attention_outbox_transaction_rejects_forged_accepted_goal(account_client, field):
    service, store, record = accepted_run(account_client)
    store.update_run(record['current_run_id'], state='needs_input')
    intent = emit_needs_input(service, store, record, persist=False)
    payload = json.loads(intent['payload_json'])
    payload['run_input'][field] = {'version': 1, 'guidance': ['Foreign input']} if field == 'continuation_context' else 'foreign-input'
    intent['payload_json'] = json.dumps(payload)
    with pytest.raises(CallbackIntentGenerationConflictError, match='accepted run input'):
        insert_attention_intent(store, intent)
    assert store.get_callback_outbox(intent['callback_id']) is None


def test_attention_outbox_transaction_rejects_resumed_run(account_client):
    service, store, record = accepted_run(account_client)
    store.update_run(record['current_run_id'], state='needs_input')
    intent = emit_needs_input(service, store, record, persist=False)
    store.update_run(record['current_run_id'], state='queued')
    assert store.get_run(record['current_run_id'])['state'] == 'queued'
    assert 'run_input' in json.loads(intent['payload_json'])
    with pytest.raises(CallbackIntentGenerationConflictError, match='accepted run input'):
        insert_attention_intent(store, intent)
    assert store.get_callback_outbox(intent['callback_id']) is None


def test_legacy_attention_outbox_keeps_original_bytes_without_accepted_goal(account_client):
    service, store, record = accepted_run(account_client)
    store.update_run(record['current_run_id'], state='needs_input')
    callback_id = 'cb_legacy_attention_input'
    intent = emit_needs_input(service, store, record, persist=False, callback_id=callback_id)
    payload = json.loads(intent['payload_json']);payload.pop('run_input')
    intent['payload_json'] = json.dumps(payload, ensure_ascii=False)
    original = insert_attention_intent(store, intent)
    replay = emit_needs_input(service, store, record, callback_id=callback_id)
    assert replay['payload_json'] == original['payload_json']
    assert 'run_input' not in json.loads(replay['payload_json'])


@pytest.mark.parametrize('kind', ['permission', 'question'])
def test_current_running_native_attention_carries_exact_goal(account_client, monkeypatch, kind):
    from test_account_api import _truthfully_invoke_run
    service, store, record = accepted_run(account_client)
    run = _truthfully_invoke_run(store, record['current_run_id'], suffix='bound-attention')
    pending = {'version': 1, 'runId': run['run_id'], 'attemptId': run['active_attempt_id'],
        'sessionId': 'session-attention', 'requestId': 'request-attention',
        'requestFingerprint': 'a' * 64, 'expiresAt': '2099-01-01T00:00:00+00:00',
        'kind': kind, 'state': 'pending', 'mode': 'form'}
    monkeypatch.setattr(service.runtime, 'pending_native_input', lambda *a, **k: pending, raising=False)
    callback = service._emit_callback_parallel(store.get_worker(record['worker_id']), 'run.needs_input',
        run=run, pending_native_input=pending, submit_delivery=False)
    payload = json.loads(callback['payload_json'])
    assert payload['run_input'] == {'version': 1, 'run_id': run['run_id'], 'instruction': run['instruction']}
    assert payload['pending_native_input']['kind'] == kind
    # The process stays running while its exact current offer blocks work.
    # Core uses this public work state to admit Main's natural question.
    assert payload['run_state'] == 'running'
    assert payload['work_state'] == 'needs_input'
    assert payload['work_terminal'] is False
    assert store.get_run(run['run_id'])['state'] == 'running'


@pytest.mark.parametrize('change', ['unbound', 'attempt', 'run', 'kind', 'state', 'current_run'])
def test_running_attention_goal_requires_exact_current_native_binding(account_client, monkeypatch, change):
    from test_account_api import _truthfully_invoke_run
    service, store, record = accepted_run(account_client)
    run = _truthfully_invoke_run(store, record['current_run_id'], suffix='bound-rejection')
    pending = {'version': 1, 'runId': run['run_id'], 'attemptId': run['active_attempt_id'],
        'sessionId': 'session-attention', 'requestId': 'request-attention',
        'requestFingerprint': 'a' * 64, 'expiresAt': '2099-01-01T00:00:00+00:00',
        'kind': 'question', 'state': 'pending', 'mode': 'form'}
    monkeypatch.setattr(service.runtime, 'pending_native_input', lambda *a, **k: pending, raising=False)
    intent = service._emit_callback_parallel(store.get_worker(record['worker_id']), 'run.needs_input',
        run=run, pending_native_input=pending, submit_delivery=False, persist_callback=False)
    payload = json.loads(intent['payload_json'])
    if change == 'unbound': payload.pop('pending_native_input')
    elif change == 'current_run':
        later = store.create_run(record['worker_id'], record['project_id'], 'Later accepted goal')
        with store._connect() as conn:
            conn.execute('UPDATE delegations SET current_run_id = ? WHERE work_ref = ?', (later['run_id'], record['work_ref']))
    else:
        field = {'attempt': 'attemptId', 'run': 'runId', 'kind': 'kind', 'state': 'state'}[change]
        payload['pending_native_input'][field] = 'foreign'
    intent['payload_json'] = json.dumps(payload)
    with pytest.raises(CallbackIntentGenerationConflictError, match='accepted run input'):
        insert_attention_intent(store, intent)
    assert store.get_callback_outbox(intent['callback_id']) is None
