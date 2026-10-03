"""Live native permission uses the existing account work control and private mailbox."""
from __future__ import annotations

import hashlib
import json
import time
from threading import Event, Thread

import pytest

from test_account_api import (
    ASSERTION_SECRET, account_client, account_headers, delegation_payload_with_origin,
    _truthfully_invoke_run,
)
from workers_projects_runtime.grok_control import (
    ControlMailbox, read_message, submit_control, write_private, permission_message_id,
)
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.profile_runtime import RuntimeErrorBase
from workers_projects_runtime.service_assertions import mint_service_assertion


class NativeSession:
    session_id = 'native-mission'
    def cancel(self):
        pass


@pytest.fixture
def permission(tmp_path, monkeypatch, request):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    worker = {'worker_id': 'worker-mission', 'owner_id': 'owner-a', 'tenant_id': 'tenant-a',
              'profile': 'grok-build', 'execution_mode': 'host', '_run_attempt_id': 'attempt-a'}
    active = {'run_id': 'run-a', 'attempt_id': 'attempt-a', 'native_session_id': 'native-mission'}
    monkeypatch.setattr(runtime, '_read_active_session', lambda _: active or None)
    monkeypatch.setattr(runtime, '_read_session_key', lambda _: 'native-mission')
    root = runtime._control_dir(worker, 'run-a', 'attempt-a')
    seen, result, events = Event(), {}, []
    def observe(event):
        events.append(event)
        if event['type'] == 'grok.permission.requested':
            seen.set()
    mailbox = ControlMailbox(root, run_id='run-a', attempt_id='attempt-a',
        session=NativeSession(), event=observe, permission_timeout=getattr(request, 'param', 30))
    mailbox.start()
    params = {'sessionId': 'native-mission', 'toolCall': {'title': 'Run the requested operation',
        'rawInput': {'private': 'not public'}}, 'options': [
        {'optionId': 'allow-once', 'kind': 'allow_once', 'name': 'Allow once'},
        {'optionId': 'allow-always', 'kind': 'allow_always', 'name': 'Allow always'},
        {'optionId': 'reject-once', 'kind': 'reject_once', 'name': 'Reject once'},
        {'optionId': 'reject-always', 'kind': 'reject_always', 'name': 'Reject always'},
    ]}
    thread = Thread(target=lambda: result.update(option=mailbox.permission(params)))
    thread.start()
    assert seen.wait(1)
    yield runtime, worker, active, root, mailbox, result, events
    mailbox.cancel_turn()
    mailbox.close()
    thread.join(2)
    assert not thread.is_alive()


@pytest.mark.parametrize('choice', ['allow-once', 'allow-always', 'reject-once', 'reject-always'])
def test_live_mission_projects_and_answers_all_native_choices(permission, choice):
    runtime, worker, _, root, _, result, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    assert pending['kind'] == 'permission'
    assert pending['requestedSchema']['properties']['optionId']['enum'] == [
        'allow-once', 'allow-always', 'reject-once', 'reject-always']
    assert 'not public' not in json.dumps(pending)
    accepted = runtime.respond_native_input(worker, run_id='run-a', request_id=pending['requestId'],
        request_fingerprint=pending['requestFingerprint'], action='accept',
        content={'optionId': choice})
    assert accepted['status'] == 'accepted'
    assert read_message(next(root.glob('*.response')))['status'] == 'permission_submitted'
    assert runtime.respond_native_input(worker, run_id='run-a', request_id=pending['requestId'],
        request_fingerprint=pending['requestFingerprint'], action='accept',
        content={'optionId': choice})['status'] == 'already_accepted'
    assert result.get('option') == choice


@pytest.mark.parametrize('action', ['decline', 'cancel'])
def test_native_decline_and_cancel_answer_without_grant(permission, action):
    runtime, worker, _, _, mailbox, result, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    assert runtime.respond_native_input(worker, run_id='run-a', request_id=pending['requestId'],
        request_fingerprint=pending['requestFingerprint'], action=action)['status'] == 'accepted'
    assert result.get('option') is None
    assert mailbox.terminal_cause() == 'dismissed'


@pytest.mark.parametrize('field,value', [
    ('run_id', 'other-run'), ('request_id', 'other-question'),
    ('request_fingerprint', 'b' * 64), ('choice', 'unoffered'),
    ('attempt', 'other-attempt'), ('session', 'other-session'),
])
def test_permission_identity_mismatch_cannot_answer(permission, field, value):
    runtime, worker, active, root, _, result, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    kwargs = {'run_id': 'run-a', 'request_id': pending['requestId'],
        'request_fingerprint': pending['requestFingerprint'], 'action': 'accept',
        'content': {'optionId': 'allow-once'}}
    if field == 'choice':
        kwargs['content'] = {'optionId': value}
    elif field == 'attempt':
        worker['_run_attempt_id'] = value
    elif field == 'session':
        active['native_session_id'] = value
    else:
        kwargs[field] = value
    with pytest.raises(RuntimeErrorBase):
        runtime.respond_native_input(worker, **kwargs)
    assert not list(root.glob('*.request'))
    assert not list(root.glob('*.response'))
    assert result == {}


def test_stop_wins_after_question_read_before_owner_answer(permission):
    runtime, worker, _, _, mailbox, result, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    mailbox.cancel_turn()
    with pytest.raises(RuntimeErrorBase, match='native_input_stale'):
        runtime.respond_native_input(worker, run_id='run-a', request_id=pending['requestId'],
            request_fingerprint=pending['requestFingerprint'], action='accept',
            content={'optionId': 'allow-once'})
    assert result.get('option') is None


@pytest.mark.parametrize('permission', [0.2], indirect=True)
def test_expired_unsubmitted_choice_never_creates_control(permission):
    runtime, worker, _, root, mailbox, result, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    deadline = time.monotonic() + 1
    while not mailbox.unanswered and time.monotonic() < deadline:
        time.sleep(.01)
    assert mailbox.unanswered == ['expired']
    with pytest.raises(RuntimeErrorBase, match='native_input_stale'):
        runtime.respond_native_input(worker, run_id='run-a', request_id=pending['requestId'],
            request_fingerprint=pending['requestFingerprint'], action='accept',
            content={'optionId': 'allow-once'})
    assert not list(root.glob('*.request'))
    assert not list(root.glob('*.response'))
    assert result.get('option') is None


@pytest.mark.parametrize('permission', [0.2], indirect=True)
def test_uncertain_submitted_ack_reconciles_after_expiry_without_resending(permission, monkeypatch):
    from workers_projects_runtime import grok_control
    runtime, worker, active, root, _, result, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    acknowledged, release = Event(), Event()
    original_write = grok_control.write_private
    def delayed_ack(path, value, **kwargs):
        if path.suffix == '.response':
            acknowledged.set()
            assert release.wait(2)
        return original_write(path, value, **kwargs)
    monkeypatch.setattr(grok_control, 'write_private', delayed_ack)
    original_submit = grok_control.submit_control
    monkeypatch.setattr(grok_control, 'submit_control',
        lambda *args, **kw: original_submit(*args, timeout=.01, **kw))
    kwargs = {'run_id': 'run-a', 'request_id': pending['requestId'],
        'request_fingerprint': pending['requestFingerprint'], 'action': 'accept',
        'content': {'optionId': 'allow-once'}}
    try:
        assert runtime.respond_native_input(worker, **kwargs)['status'] == 'pending'
        assert acknowledged.wait(1)
        submitted = next(root.glob('*.request'))
        first_body = submitted.read_bytes()
        time.sleep(.25)
        active.clear()
        assert runtime.pending_native_input(worker, run_id='run-a') is None
        assert runtime.respond_native_input(worker, allow_new=False, **kwargs)['status'] == 'pending'
        assert submitted.read_bytes() == first_body
        release.set()
        deadline = time.monotonic() + 1
        while not list(root.glob('*.response')) and time.monotonic() < deadline:
            time.sleep(.01)
        assert runtime.respond_native_input(worker, allow_new=False, **kwargs)['status'] == 'already_accepted'
        assert result.get('option') == 'allow-once'
        assert len(list(root.glob('*.response'))) == 1
        assert not list(root.glob('*.request'))
    finally:
        release.set()


def test_changed_answer_cannot_reuse_native_ack(permission):
    runtime, worker, _, root, _, _, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    kwargs = {'run_id': 'run-a', 'request_id': pending['requestId'],
        'request_fingerprint': pending['requestFingerprint'], 'action': 'accept',
        'content': {'optionId': 'allow-once'}}
    assert runtime.respond_native_input(worker, **kwargs)['status'] == 'accepted'
    original = next(root.glob('*.response')).read_bytes()
    with pytest.raises(RuntimeErrorBase, match='native_input_conflict'):
        runtime.respond_native_input(worker, **{**kwargs, 'content': {'optionId': 'allow-always'}})
    assert next(root.glob('*.response')).read_bytes() == original


def test_tampered_ack_never_claims_acceptance(permission):
    runtime, worker, _, root, _, _, _ = permission
    pending = runtime.pending_native_input(worker, run_id='run-a')
    message_id = permission_message_id('run-a', 'attempt-a', pending['requestId'], pending['requestFingerprint'])
    write_private(root / (message_id + '.response'), {'status': 'permission_submitted'})
    with pytest.raises(RuntimeErrorBase, match='native_input_conflict'):
        runtime.respond_native_input(worker, run_id='run-a', request_id=pending['requestId'],
            request_fingerprint=pending['requestFingerprint'], action='accept',
            content={'optionId': 'allow-once'})


def _signed_headers(body):
    raw = json.dumps(body, separators=(',', ':')).encode()
    assertion = mint_service_assertion(ASSERTION_SECRET, tenant_id='tenant-a', owner_id='owner-a',
        native_input_digest=hashlib.sha256(raw).hexdigest())
    return raw, {**account_headers(assertion=assertion), 'Content-Type': 'application/json'}


def test_uncertain_native_ack_keeps_same_work_action_recoverable(account_client, monkeypatch):
    response = account_client.post('/v1/delegations', headers=account_headers(idempotency_key='pending-input'),
        json=delegation_payload_with_origin())
    assert response.status_code == 202, response.text
    work_ref = response.json()['workRef']
    service, store = account_client.app.state.service, account_client.app.state.store
    record = store.get_delegation(work_ref, tenant_id='tenant-a', owner_id='owner-a')
    _truthfully_invoke_run(store, record['current_run_id'], suffix='pending-input')
    calls = []
    def respond(worker, **kwargs):
        calls.append(kwargs)
        return {'status': 'pending'} if len(calls) == 1 else {'status': 'already_accepted'}
    monkeypatch.setattr(service.runtime, 'respond_native_input', respond, raising=False)
    body = {'action': 'resume', 'idempotencyKey': 'one-operation', 'nativeInput': {
        'version': 1, 'requestId': 'request-a', 'requestFingerprint': 'a' * 64,
        'action': 'accept', 'content': {'optionId': 'allow-once'}}}
    raw, headers = _signed_headers(body)
    path = f'/v1/work/{work_ref}/actions'
    first = account_client.post(path, headers=headers, content=raw)
    assert first.status_code == 202, first.text
    assert first.json()['status'] == 'pending'
    assert first.json()['confirmationPending'] is True
    with store._connect() as conn:
        action = dict(conn.execute('SELECT * FROM active_work_action_uses').fetchone())
    assert action['status'] == 'pending'
    second = account_client.post(path, headers=_signed_headers(body)[1], content=raw)
    assert second.status_code == 202, second.text
    assert second.json()['status'] == 'already_accepted'
    assert second.json()['confirmationPending'] is False
    assert calls[0]['allow_new'] is True and calls[1]['allow_new'] is False
    assert {key: value for key, value in calls[0].items() if key != 'allow_new'} == {
        key: value for key, value in calls[1].items() if key != 'allow_new'}
    repeated = account_client.post(path, headers=_signed_headers(body)[1], content=raw)
    assert repeated.json()['idempotentReplay'] is True
    assert len(calls) == 2
    assert store.get_run(record['current_run_id'])['state'] == 'running'


def test_actual_mailbox_ack_commits_signed_work_action_once(account_client, monkeypatch, tmp_path):
    accepted = account_client.post('/v1/delegations',
        headers=account_headers(idempotency_key='actual-permission'),
        json=delegation_payload_with_origin()).json()
    service, store = account_client.app.state.service, account_client.app.state.store
    record = store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    run = _truthfully_invoke_run(store, record['current_run_id'], suffix='actual-permission')
    runtime = HostGrokBuildRuntime(str(tmp_path / 'actual-runtime'))
    worker = store.get_worker(record['worker_id'])
    active = {'run_id': run['run_id'], 'attempt_id': run['active_attempt_id'],
              'native_session_id': 'native-mission'}
    monkeypatch.setattr(runtime, '_read_active_session', lambda _: active or None)
    monkeypatch.setattr(runtime, '_read_session_key', lambda _: 'native-mission')
    root = runtime._control_dir(worker, run['run_id'], run['active_attempt_id'])
    mailbox = ControlMailbox(root, run_id=run['run_id'], attempt_id=run['active_attempt_id'],
        session=NativeSession(), event=lambda _: None)
    mailbox.start()
    result = {}
    thread = Thread(target=lambda: result.update(option=mailbox.permission({
        'sessionId': 'native-mission', 'options': [{'optionId': 'allow-once', 'kind': 'allow_once'}]})))
    thread.start()
    try:
        deadline = time.monotonic() + 1
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = runtime.pending_native_input(worker, run_id=run['run_id'])
            if pending is None:
                time.sleep(.01)
        assert pending is not None
        monkeypatch.setattr(service.runtime, 'respond_native_input', runtime.respond_native_input, raising=False)
        body = {'action': 'resume', 'idempotencyKey': 'actual-native-operation', 'nativeInput': {
            'version': 1, 'requestId': pending['requestId'],
            'requestFingerprint': pending['requestFingerprint'], 'action': 'accept',
            'content': {'optionId': 'allow-once'}}}
        raw, headers = _signed_headers(body)
        path = f"/v1/work/{accepted['workRef']}/actions"
        first = account_client.post(path, headers=headers, content=raw)
        assert first.status_code == 202, first.text
        assert first.json()['status'] == 'accepted'
        assert first.json()['confirmationPending'] is False
        thread.join(1)
        assert result['option'] == 'allow-once'
        active.clear()
        repeated = account_client.post(path, headers=_signed_headers(body)[1], content=raw)
        assert repeated.json()['idempotentReplay'] is True
        assert repeated.json()['status'] == 'accepted'
        assert len(list(root.glob('*.response'))) == 1
        events = [event for event in store.list_events(worker['worker_id'])
                  if event['run_id'] == run['run_id'] and event['event_type'] == 'worker.native_input_answered']
        assert len(events) == 1
        assert store.get_run(run['run_id'])['state'] == 'running'
        assert len(store.list_runs_for_worker(worker['worker_id'])) == 1
    finally:
        mailbox.cancel_turn()
        mailbox.close()
        thread.join(2)


@pytest.mark.parametrize('mismatch', [None, 'attempt_id', 'sessionId', 'requestId', 'replaced_question'])
def test_live_permission_event_publishes_one_attention_callback(account_client, monkeypatch, mismatch):
    response = account_client.post('/v1/delegations', headers=account_headers(idempotency_key='attention-input'),
        json=delegation_payload_with_origin())
    assert response.status_code == 202, response.text
    service, store = account_client.app.state.service, account_client.app.state.store
    record = store.get_delegation(response.json()['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    run = _truthfully_invoke_run(store, record['current_run_id'], suffix='attention-input')
    worker = store.get_worker(record['worker_id'])
    pending = {'version': 1, 'kind': 'permission', 'requestId': 'question-a',
        'requestFingerprint': 'a' * 64, 'runId': run['run_id'], 'attemptId': run['active_attempt_id'],
        'sessionId': 'session-a', 'expiresAt': '2099-01-01T00:00:00+00:00',
        'mode': 'form', 'message': 'Approve this operation?', 'state': 'pending'}
    calls = []
    def current_pending(*_args, **_kw):
        calls.append(True)
        if mismatch == 'replaced_question' and len(calls) % 2 == 0:
            return {**pending, 'requestId': 'later-question', 'requestFingerprint': 'b' * 64}
        return pending
    monkeypatch.setattr(service.runtime, 'pending_native_input', current_pending, raising=False)
    monkeypatch.setattr(service.executor, 'submit', lambda *_args, **_kw: None)
    observation = {'worker_id': worker['worker_id'], 'run_id': run['run_id'],
        'attempt_id': run['active_attempt_id'], 'provider': 'grok',
        'event': {'event_type': 'provider.native.input.requested', 'payload': {
            'requestId': 'question-a', 'sessionId': 'session-a', 'method': 'session/request_permission'}}}
    if mismatch == 'attempt_id':
        observation['attempt_id'] = 'other-attempt'
    elif mismatch in {'sessionId', 'requestId'}:
        observation['event']['payload'][mismatch] = 'other-identity'
    service._observe_native_event(observation)
    service._observe_native_event(observation)
    callbacks = [row for row in store.list_callback_outbox_for_run(run['run_id'],
                 tenant_id='tenant-a', owner_id='owner-a')
                 if json.loads(row['payload_json'])['event'] == 'run.needs_input']
    assert len(callbacks) == (0 if mismatch else 1)
    if not mismatch:
        payload = json.loads(callbacks[0]['payload_json'])
        assert payload['pending_native_input']['requestFingerprint'] == 'a' * 64
        assert payload['pending_native_input']['kind'] == 'permission'
        assert payload['pending_native_input']['state'] == 'pending'
        assert payload['pending_native_input']['mode'] == 'form'
        assert payload['run_state'] == 'running'
    assert store.get_run(run['run_id'])['state'] == 'running'
    assert store.get_active_host_run_lease_for_run(run['run_id']) is not None


@pytest.mark.parametrize('mismatch', [None, 'attempt', 'session', 'expired_next'])
def test_resolved_permission_publishes_next_overlapping_question(account_client, monkeypatch, tmp_path, mismatch):
    from workers_projects_runtime.grok_projection import native_events
    accepted = account_client.post('/v1/delegations',
        headers=account_headers(idempotency_key='overlapping-permission'),
        json=delegation_payload_with_origin()).json()
    service, store = account_client.app.state.service, account_client.app.state.store
    record = store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    run = _truthfully_invoke_run(store, record['current_run_id'], suffix='overlapping-permission')
    worker = store.get_worker(record['worker_id'])
    runtime = HostGrokBuildRuntime(str(tmp_path / 'overlapping-runtime'))
    active = {'run_id': run['run_id'], 'attempt_id': run['active_attempt_id'],
              'native_session_id': 'native-mission'}
    monkeypatch.setattr(runtime, '_read_active_session', lambda _: active)
    monkeypatch.setattr(runtime, '_read_session_key', lambda _: 'native-mission')
    original_state = runtime.native_control_state
    def ordered_state(*args, **kwargs):
        # Filesystem order is unspecified; retain a deterministic overlapping fixture.
        state = original_state(*args, **kwargs)
        state['pending_requests'].sort(key=lambda item: item['expires_at'])
        return state
    monkeypatch.setattr(runtime, 'native_control_state', ordered_state)
    monkeypatch.setattr(service.runtime, 'pending_native_input', runtime.pending_native_input, raising=False)
    monkeypatch.setattr(service.executor, 'submit', lambda *_args, **_kw: None)
    root = runtime._control_dir(worker, run['run_id'], run['active_attempt_id'])
    events, results = [], []
    def observe(value):
        events.append(value)
        for event in native_events(value):
            if event['event_type'] == 'provider.native.input.resolved':
                if mismatch == 'expired_next':
                    for path in root.glob('*.pending'):
                        pending = read_message(path)
                        if pending['request_id'] != value['request_id']:
                            write_private(path, {**pending, 'expires_at': time.time() - 1})
                elif mismatch == 'session':
                    event['payload']['sessionId'] = 'foreign-session'
            service._observe_native_event({'worker_id': worker['worker_id'], 'run_id': run['run_id'],
                'attempt_id': 'foreign-attempt' if mismatch == 'attempt' and event['event_type'].endswith('resolved')
                    else run['active_attempt_id'], 'provider': 'grok', 'event': event})
    mailbox = ControlMailbox(root, run_id=run['run_id'], attempt_id=run['active_attempt_id'],
        session=NativeSession(), event=observe)
    mailbox.start()
    threads = []
    try:
        for title in ['First operation', 'Second operation']:
            thread = Thread(target=lambda title=title: results.append(mailbox.permission({
                'sessionId': 'native-mission', 'toolCall': {'title': title},
                'options': [{'optionId': 'allow-once', 'kind': 'allow_once'}]})))
            thread.start(); threads.append(thread)
            deadline = time.monotonic() + 1
            while len([event for event in events if event['type'] == 'grok.permission.requested']) < len(threads):
                assert time.monotonic() < deadline
                time.sleep(.01)
        first = runtime.pending_native_input(worker, run_id=run['run_id'])
        assert first['message'] == 'First operation'
        assert runtime.respond_native_input(worker, run_id=run['run_id'], request_id=first['requestId'],
            request_fingerprint=first['requestFingerprint'], action='accept',
            content={'optionId': 'allow-once'})['status'] == 'accepted'
        threads[0].join(1)
        assert not threads[0].is_alive()
        callbacks = [json.loads(row['payload_json']) for row in store.list_callback_outbox_for_run(
            run['run_id'], tenant_id='tenant-a', owner_id='owner-a')
            if json.loads(row['payload_json'])['event'] == 'run.needs_input']
        assert len(callbacks) == (1 if mismatch else 2)
        if mismatch is None:
            second = runtime.pending_native_input(worker, run_id=run['run_id'])
            assert second['message'] == 'Second operation'
            assert {item['pending_native_input']['requestId'] for item in callbacks} == {
                first['requestId'], second['requestId']}
            assert runtime.respond_native_input(worker, run_id=run['run_id'], request_id=second['requestId'],
                request_fingerprint=second['requestFingerprint'], action='accept',
                content={'optionId': 'allow-once'})['status'] == 'accepted'
            threads[1].join(1)
            assert not threads[1].is_alive()
            assert results == ['allow-once', 'allow-once']
            assert len([row for row in store.list_callback_outbox_for_run(run['run_id'],
                tenant_id='tenant-a', owner_id='owner-a')
                if json.loads(row['payload_json'])['event'] == 'run.needs_input']) == 2
        assert store.get_run(run['run_id'])['state'] == 'running'
        assert store.get_active_host_run_lease_for_run(run['run_id']) is not None
    finally:
        mailbox.cancel_turn(); mailbox.close()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()


def test_missing_ack_after_terminal_run_keeps_exact_prior_operation_unknown(account_client, monkeypatch, tmp_path):
    accepted = account_client.post('/v1/delegations',
        headers=account_headers(idempotency_key='missing-ack'),
        json=delegation_payload_with_origin()).json()
    service, store = account_client.app.state.service, account_client.app.state.store
    record = store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    run = _truthfully_invoke_run(store, record['current_run_id'], suffix='missing-ack')
    runtime = HostGrokBuildRuntime(str(tmp_path / 'removed-control-runtime'))
    monkeypatch.setattr(runtime, '_read_active_session', lambda _: None)
    calls = []
    def respond(worker, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {'status': 'pending'}
        return runtime.respond_native_input(worker, **kwargs)
    monkeypatch.setattr(service.runtime, 'respond_native_input', respond, raising=False)
    body = {'action': 'resume', 'idempotencyKey': 'submitted-operation', 'nativeInput': {
        'version': 1, 'requestId': 'request-a', 'requestFingerprint': 'a' * 64,
        'action': 'accept', 'content': {'optionId': 'allow-once'}}}
    path = f"/v1/work/{accepted['workRef']}/actions"
    raw, headers = _signed_headers(body)
    first = account_client.post(path, headers=headers, content=raw)
    assert first.status_code == 202 and first.json()['confirmationPending'] is True
    store.update_run(run['run_id'], state='completed')
    for _ in range(2):
        replay = account_client.post(path, headers=_signed_headers(body)[1], content=raw)
        assert replay.status_code == 202, replay.text
        assert replay.json()['status'] == 'pending'
        assert replay.json()['confirmationPending'] is True
        assert replay.json()['idempotentReplay'] is True
    assert calls[0]['allow_new'] is True
    assert all(call['allow_new'] is False for call in calls[1:])
    with store._connect() as conn:
        action = dict(conn.execute('SELECT * FROM active_work_action_uses').fetchone())
    assert action['status'] == 'pending'
    assert store.get_run(run['run_id'])['state'] == 'completed'
    assert not list((tmp_path / 'removed-control-runtime').rglob('*.request'))
    changed = {**body, 'nativeInput': {**body['nativeInput'], 'content': {'optionId': 'allow-always'}}}
    changed_raw, changed_headers = _signed_headers(changed)
    assert account_client.post(path, headers=changed_headers, content=changed_raw).status_code == 409
    fresh = {**body, 'idempotencyKey': 'unsubmitted-operation'}
    fresh_raw, fresh_headers = _signed_headers(fresh)
    fresh_response = account_client.post(path, headers=fresh_headers, content=fresh_raw)
    assert fresh_response.status_code == 409
    assert fresh_response.json()['detail']['code'] == 'native_input_stale'


@pytest.mark.parametrize('error,status_code', [
    ('native_input_stale', 409), ('native_input_conflict', 409), ('native_input_invalid', 400),
])
def test_prior_pending_does_not_mask_definitive_native_rejection(account_client, monkeypatch, error, status_code):
    accepted = account_client.post('/v1/delegations',
        headers=account_headers(idempotency_key='definitive-native-result'),
        json=delegation_payload_with_origin()).json()
    service, store = account_client.app.state.service, account_client.app.state.store
    record = store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    _truthfully_invoke_run(store, record['current_run_id'], suffix='definitive-native-result')
    calls = []
    def respond(_worker, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {'status': 'pending'}
        raise RuntimeErrorBase(error)
    monkeypatch.setattr(service.runtime, 'respond_native_input', respond, raising=False)
    body = {'action': 'resume', 'idempotencyKey': 'exact-definitive-operation', 'nativeInput': {
        'version': 1, 'requestId': 'request-a', 'requestFingerprint': 'a' * 64,
        'action': 'accept', 'content': {'optionId': 'allow-once'}}}
    raw, headers = _signed_headers(body)
    path = f"/v1/work/{accepted['workRef']}/actions"
    first = account_client.post(path, headers=headers, content=raw)
    assert first.status_code == 202 and first.json()['confirmationPending'] is True
    second = account_client.post(path, headers=_signed_headers(body)[1], content=raw)
    assert second.status_code == status_code, second.text
    assert 'confirmationPending' not in second.json()
    assert calls[1]['allow_new'] is False
