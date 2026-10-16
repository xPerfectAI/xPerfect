"""Signed current human replies reuse the exact native permission and ACK."""
import json
import time
from datetime import datetime, timezone

import pytest

from test_grok_mission_input import permission, _signed_headers
from test_account_api import account_client, account_headers, delegation_payload_with_origin, _truthfully_invoke_run
from test_native_input_presentation import permission as permission_wire, project
from workers_projects_runtime.grok_control import read_message, write_private
from workers_projects_runtime.profile_runtime import RuntimeErrorBase


def human():
    return {'version': 1, 'actorKind': 'external_user', 'origin': 'interactive',
        'sourceEventId': 'event-synthetic', 'sourceIndex': 0, 'sourceMessageId': 'message-synthetic',
        'authoringRevision': 1, 'logicalTurnId': 'turn-synthetic', 'conversationId': 'conversation-synthetic',
        'receivedAt': int(time.time() * 1000)}


@pytest.fixture
def offered(permission, monkeypatch):
    runtime, worker, active, root, mailbox, result, events = permission
    item = read_message(next(root.glob('*.pending')))
    source = human()
    event = {**events[0], 'observed_at': datetime.fromtimestamp((source['receivedAt'] - 1000) / 1000, timezone.utc).isoformat()}
    path = root / 'synthetic-native-attempt.log'
    path.write_text(json.dumps(event) + '\n')
    monkeypatch.setattr(runtime, '_attempt_transcript', lambda *args: (path, 'launch-synthetic'))
    return runtime, worker, root, result, item, event, path, source, active


def respond(offered, choice='allow-once', **updates):
    runtime, worker, *_ = offered
    pending = runtime.pending_native_input(worker, run_id='run-a')
    if pending is None:
        retained = read_message(next(offered[2].glob('*.response')))['_request']
        pending = {'requestId': retained['request_id'], 'requestFingerprint': retained['request_fingerprint']}
    args = {'run_id': 'run-a', 'request_id': pending['requestId'],
        'request_fingerprint': pending['requestFingerprint'], 'action': 'accept',
        'content': {'optionId': choice}, 'native_human_input': offered[7]}
    return runtime.respond_native_input(worker, **{**args, **updates})


@pytest.mark.parametrize('choice', ['allow-once', 'reject-once'])
def test_current_after_offer_single_operation_reuses_actual_mailbox_ack(offered, choice):
    assert respond(offered, choice)['status'] == 'accepted'
    assert offered[3] == {'option': choice}
    assert respond(offered, choice)['status'] == 'already_accepted'
    root = offered[2]
    assert len(list(root.glob('*.response'))) == 1
    receipt = read_message(next(root.glob('*.response')))
    assert receipt['_request']['native_human_input'] == offered[7]
    with pytest.raises(RuntimeErrorBase, match='native_input_conflict'):
        respond(offered, choice, native_human_input={**offered[7], 'sourceEventId': 'different'})


@pytest.mark.parametrize('mutation', ['allow-always', 'reject-always', 'unknown-kind', 'missing-kind',
    'before', 'equal', 'event-missing', 'event-duplicate', 'event-clock-missing', 'event-clock-naive',
    'event-session', 'event-request', 'claim-replaced', 'actor', 'origin', 'missing-receipt', 'boolean-time', 'boolean-version',
    'wrong-request', 'wrong-fingerprint', 'session', 'run', 'attempt', 'expiry'])
def test_natural_permission_rejects_unproved_scope_or_order_without_native_effect(offered, mutation, monkeypatch):
    runtime, worker, root, result, item, event, path, source, active = offered
    updates, choice = {}, 'allow-once'
    if mutation in {'allow-always', 'reject-always'}: choice = mutation
    elif mutation in {'unknown-kind', 'missing-kind'}:
        item['request']['options'][0]['kind'] = 'unrecognized' if mutation == 'unknown-kind' else None
        event['request'] = item['request']
        write_private(next(root.glob('*.pending')), item)
    elif mutation in {'before', 'equal'}:
        source['receivedAt'] -= 1001 if mutation == 'before' else 1000
    elif mutation == 'event-missing': event = {}
    elif mutation == 'event-clock-missing': event.pop('observed_at')
    elif mutation == 'event-clock-naive': event['observed_at'] = '2026-01-01T00:00:00'
    elif mutation == 'event-session': event['session_id'] = 'different'
    elif mutation == 'event-request': event['request'] = {'sessionId': 'different'}
    elif mutation == 'claim-replaced':
        claims = iter([(path, 'original'), (path, 'different')])
        monkeypatch.setattr(runtime, '_attempt_transcript', lambda *args: next(claims))
    elif mutation == 'actor': source['actorKind'] = 'worker'
    elif mutation == 'origin': source['origin'] = 'callback'
    elif mutation == 'missing-receipt': source.pop('receivedAt')
    elif mutation == 'boolean-time': source['receivedAt'] = True
    elif mutation == 'boolean-version': source['version'] = True
    elif mutation == 'wrong-request': updates['request_id'] = 'different'
    elif mutation == 'wrong-fingerprint': updates['request_fingerprint'] = '0' * 64
    elif mutation in {'session', 'run', 'attempt'}:
        active[{'session': 'native_session_id', 'run': 'run_id', 'attempt': 'attempt_id'}[mutation]] = 'different'
    elif mutation == 'expiry':
        item['expires_at'] = time.time() - 1
        write_private(next(root.glob('*.pending')), item)
    path.write_text(json.dumps(event) + '\n' + (json.dumps(event) + '\n' if mutation == 'event-duplicate' else ''))
    # Obtain the originally offered identity even when the current state is now stale.
    from workers_projects_runtime.provider_native_input import project_permission_request
    projected = project_permission_request({**item, 'expires_at': time.time() + 60}, run_id='run-a', attempt_id='attempt-a')
    if runtime.pending_native_input(worker, run_id='run-a') is None:
        updates.setdefault('request_id', item['request_id'])
        updates.setdefault('request_fingerprint', projected['requestFingerprint'])
        with pytest.raises(RuntimeErrorBase):
            runtime.respond_native_input(worker, run_id='run-a', action='accept', content={'optionId': choice},
                native_human_input=source, **updates)
    else:
        with pytest.raises(RuntimeErrorBase): respond(offered, choice, **updates)
    assert result == {}
    assert not list(root.glob('*.request')) and not list(root.glob('*.response'))


def test_permission_kind_is_typed_aligned_and_never_inferred_from_labels():
    request = permission_wire()
    request['request']['options'][0].update(optionId='opaque-a', name='No, stop', kind='allow_always')
    request['request']['options'][1].update(optionId='opaque-b', name='Yes, proceed', kind='reject_once')
    choices = project(request)['choices']
    assert choices == [{'value': 'opaque-a', 'label': 'No, stop', 'kind': 'allow_always'},
                       {'value': 'opaque-b', 'label': 'Yes, proceed', 'kind': 'reject_once'}]


@pytest.mark.parametrize('path', ['/workspace/private-parent/shape.svg', '/workspace/private-parent/日本語 shape.svg'])
def test_write_speech_uses_only_typed_safe_basename(path):
    request = permission_wire()
    request['request']['toolCall'] = {'title': 'Write full operation detail', 'kind': 'edit', 'rawInput': {'variant': 'write', 'file_path': path},
        '_meta': {'x.ai/tool': {'version': 1, 'name': 'write', 'kind': 'write', 'read_only': False, 'input': {'path': path}}}}
    projected = project(request)
    assert projected['prompt'] == 'Write ' + path.rsplit('/', 1)[-1] + '\nMay I proceed?'
    assert 'private-parent' not in projected['prompt']
    assert projected['operationTitle'] == 'Write ' + path.rsplit('/', 1)[-1]
    assert request['request']['toolCall']['rawInput']['file_path'] == path


@pytest.mark.parametrize('mutation', ['missing', 'wrong-kind', 'wrong-name', 'wrong-acp-kind', 'read-only', 'mismatch', 'raw-path-mismatch', 'missing-raw-path', 'uri', 'control', 'slash', 'dot', 'long', 'version', 'invalid-label'])
def test_unsupported_write_metadata_retains_original_operation_detail(mutation):
    path = '/workspace/private-parent/shape.svg'
    request = permission_wire()
    call = {'title': 'Original operation', 'kind': 'edit', 'rawInput': {'variant': 'write', 'file_path': path},
        '_meta': {'x.ai/tool': {'version': 1, 'name': 'write', 'kind': 'write', 'read_only': False, 'input': {'path': path}}}}
    native = call['_meta']['x.ai/tool']
    if mutation == 'missing': native['input'].pop('path')
    elif mutation == 'wrong-kind': native['kind'] = 'read'
    elif mutation == 'read-only': native['read_only'] = True
    elif mutation == 'wrong-name': native['name'] = 'other'
    elif mutation == 'wrong-acp-kind': call['kind'] = 'write'
    elif mutation == 'mismatch': call['rawInput']['file_path'] = '/workspace/different.svg'
    elif mutation == 'raw-path-mismatch': call['rawInput']['path'] = '/workspace/different.svg'
    elif mutation == 'missing-raw-path': call['rawInput'].pop('file_path')
    elif mutation == 'version': native['version'] = 2
    elif mutation == 'invalid-label': native['label'] = {'unexpected': 'value'}
    else:
        native['input']['path'] = {'uri': 'https://synthetic.invalid/file.svg', 'control': '/workspace/bad\n.svg',
            'slash': '/workspace/folder/', 'dot': '/workspace/..', 'long': '/workspace/' + 'x' * 161}[mutation]
        call['rawInput']['file_path'] = native['input']['path']
    request['request']['toolCall'] = call
    assert project(request)['prompt'] == 'Original operation\nMay I proceed?'


@pytest.mark.parametrize('mode', ['unsigned', 'signed', 'tampered'])
def test_human_attestation_reaches_native_only_through_whole_signed_body(account_client, monkeypatch, mode):
    accepted = account_client.post('/v1/delegations', headers=account_headers(idempotency_key='human-native-fixture'),
        json=delegation_payload_with_origin()).json()
    service, store = account_client.app.state.service, account_client.app.state.store
    record = store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    _truthfully_invoke_run(store, record['current_run_id'], suffix='human-native-fixture')
    calls = []
    monkeypatch.setattr(service.runtime, 'respond_native_input', lambda worker, **kwargs: calls.append(kwargs) or {'status': 'accepted'}, raising=False)
    body = {'action': 'resume', 'idempotencyKey': 'human-native-response', 'nativeInput': {'version': 1,
        'requestId': 'request-synthetic', 'requestFingerprint': 'a' * 64, 'action': 'accept', 'content': {'optionId': 'opaque-a'}},
        'nativeHumanInput': human()}
    raw, headers = _signed_headers(body)
    if mode == 'unsigned': headers = {**account_headers(), 'Content-Type': 'application/json'}
    elif mode == 'tampered':
        body['nativeHumanInput']['receivedAt'] += 1
        raw = json.dumps(body, separators=(',', ':')).encode()
    response = account_client.post('/v1/work/' + accepted['workRef'] + '/actions', headers=headers, content=raw)
    assert response.status_code == (202 if mode == 'signed' else 403), response.text
    assert bool(calls) is (mode == 'signed')
    if mode == 'signed':
        assert calls[0]['native_human_input'] == body['nativeHumanInput']
        repeated = account_client.post('/v1/work/' + accepted['workRef'] + '/actions', headers=_signed_headers(body)[1], content=raw)
        assert repeated.json()['idempotentReplay'] is True and len(calls) == 1


@pytest.mark.parametrize('name,label', [('search_replace', 'Edit'), ('new_native_editor', 'Update')])
def test_typed_native_edit_retains_safe_filename_for_the_exact_current_request(name, label):
    import copy
    import hashlib
    path = '/workspace/private-parent/work-log.md'
    request = permission_wire()
    request['request']['toolCall'] = {'title': f'{label} `{path}`', 'kind': 'edit',
        'rawInput': {'file_path': path, 'old_string': 'private original', 'new_string': 'private change'},
        '_meta': {'x.ai/tool': {'version': 1, 'name': name, 'label': label, 'kind': 'edit',
            'read_only': False, 'input': {'path': path}}}}
    before = copy.deepcopy(request)
    result = project(request)
    assert result['prompt'] == label + ' work-log.md\nMay I proceed?'
    assert result['operationTitle'] == label + ' work-log.md'
    assert 'private-parent' not in str(result) and 'private change' not in str(result)
    assert result['requestFingerprint'] == hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert request == before


@pytest.mark.parametrize('change', ['path-mismatch', 'read-only', 'missing-label', 'nonfile-kind', 'label-control'])
def test_unproved_native_edit_keeps_redacted_original_operation(change):
    path = '/home/synthetic/private-parent/work-log.md'
    request = permission_wire()
    call = {'title': f'Edit `{path}`', 'kind': 'edit', 'rawInput': {'file_path': path},
        '_meta': {'x.ai/tool': {'version': 1, 'name': 'search_replace', 'label': 'Edit',
            'kind': 'edit', 'read_only': False, 'input': {'path': path}}}}
    native = call['_meta']['x.ai/tool']
    if change == 'path-mismatch': call['rawInput']['file_path'] = '/workspace/other.md'
    elif change == 'read-only': native['read_only'] = True
    elif change == 'missing-label': native.pop('label')
    elif change == 'nonfile-kind': native['kind'] = 'execute'
    else: native['label'] = 'Edit\nInjected'
    request['request']['toolCall'] = call
    result = project(request)
    assert result['operationTitle'] == 'Edit `[REDACTED_LOCAL_PATH]`'
    assert result['prompt'] == 'Edit `[REDACTED_LOCAL_PATH]`\nMay I proceed?'
