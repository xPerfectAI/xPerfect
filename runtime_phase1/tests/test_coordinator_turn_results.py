"""A completed application turn is real result authority without a fabricated goal."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_coordinator_application import app_core, accept, packet
from workers_projects_runtime.coordinator import Dispatch, Goal, canonical
from workers_projects_runtime.coordinator_api import install_coordinator_routes
from workers_projects_runtime.coordinator_application import (
    completed_turn_result, notification_fence, prepare_turn, publications, queue_turn_results,
    turn_result, turn_status,
)
from workers_projects_runtime.schedule_targets import due_callback_records, finish_callback


def complete(core, cid, *, text='Canonical answer: café 🐝', native='Internal native record is different.'):
    accept(core, cid)
    prepared = prepare_turn(core, 'local', 'owner', cid, 'source', packet(cid))
    request = core.store.get_provider_request(prepared['request_id'])
    session = core.store.get_provider_session_by_id(request['session_id'])
    run = core.store.create_run(session['worker_id'], session['project_id'], 'Actual accepted source')
    core.store.update_run(run['run_id'], state='completed', output_text=native, active_attempt_id='synthetic-attempt')
    run = core.store.get_run(run['run_id'])
    response = {'id': request['request_id'], 'object': 'chat.completion',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
        'glasshive': {'request_id': request['request_id']}}
    core.store.update_provider_request(request['request_id'], run_id=run['run_id'], state='completed',
                                       response_json=json.dumps(response))
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET response_json=? WHERE conversation_id=? AND turn_id=?',
                     (canonical(response), cid, 'source'))
    return request, run, response


def notices(core):
    with core.store._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM callback_outbox "
            "WHERE event_type='coordinator.turn_result_ready' ORDER BY rowid")]


def client_for(core):
    app = FastAPI()
    install_coordinator_routes(app, core,
        lambda request: (request.headers.get('x-test-tenant', 'local'), request.headers.get('x-test-owner', 'owner')),
        lambda *args: None)
    return TestClient(app)


def test_existing_refresh_backfills_completed_turn_once_without_goal_or_inference(app_core):
    core, cid, _ = app_core
    request, run, response = complete(core, cid)
    count = len(core.provider.received)
    assert notices(core) == []
    first = core.refresh('local', 'owner', cid, False)
    notice = notices(core)[0]
    event = json.loads(notice['payload_json'])
    assert event['data'] == {'conversation_id': cid, 'request_ref': 'opaque-app-binding', 'turn_id': 'source',
        'request_id': request['request_id'], 'run_id': run['run_id'], 'result_revision': run['terminal_result_revision'],
        'result_digest': 'sha256:' + hashlib.sha256(response['choices'][0]['message']['content'].encode()).hexdigest()}
    assert event['type'] == 'coordinator.turn_result_ready' and notice['worker_id'] is None
    assert first['turns'][0]['has_result'] is True and first['turns'][0]['run_id'] == run['run_id']
    assert first['turns'][0]['result_revision'] == run['terminal_result_revision']
    core.refresh('local', 'owner', cid, False)
    assert notices(core) == [notice]
    assert len(core.provider.received) == count
    assert core.snapshot('local', 'owner', cid)['goals'] == []


def test_concurrent_observation_is_one_immutable_outbox_intent(app_core):
    core, cid, _ = app_core
    complete(core, cid)
    conversation = core._conversation('local', 'owner', cid)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: queue_turn_results(core, conversation), range(8)))
    assert len(notices(core)) == 1
    with core.store._connect() as conn, pytest.raises(sqlite3.IntegrityError, match='snapshot is immutable'):
        conn.execute("UPDATE callback_outbox SET payload_json='{}' WHERE event_type='coordinator.turn_result_ready'")


def test_selected_result_paging_hashes_canonical_response_and_preserves_publisher(app_core):
    core, cid, _ = app_core
    request, run, response = complete(core, cid)
    client = client_for(core)
    url = f'/v1/coordinator/conversations/{cid}/turns/source/result'
    query = {'request_id': request['request_id'], 'run_id': run['run_id'], 'max_chars': 4}
    text, offset = '', 0
    while True:
        result = client.get(url, params={**query, 'offset': offset})
        assert result.status_code == 200, result.text
        page = result.json()
        text += page['output_text']
        assert page['source_turn_id'] == 'source' and page['presentation_eligible'] is True
        assert page['publisher'] == {'owner_id': 'owner', 'conversation_id': cid, 'agent_id': 'coordinator',
            'request_id': request['request_id'], 'message_id': 'source', 'stream_id': 'source',
            'run_id': run['run_id'], 'attempt_id': 'synthetic-attempt'}
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert text == response['choices'][0]['message']['content']
    assert page['sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert page['sha256'] != hashlib.sha256(run['output_text'].encode()).hexdigest()
    assert page['total_chars'] == len(text)
    for headers in [{'x-test-owner': 'foreign'}, {'x-test-tenant': 'foreign'}]:
        assert client.get(url, params=query, headers=headers).status_code == 403
    for changed in [{'run_id': 'different'}, {'request_id': 'different'}, {'offset': 10000}, {'max_chars': 0}]:
        assert client.get(url, params={**query, **changed}).status_code == 409


@pytest.mark.parametrize('kind', ['durable_dispatch', 'same_run_goal', 'worker_results', 'standalone', 'failed', 'deleted_request'])
def test_only_unrepresented_completed_interactive_application_turns_notify(app_core, kind):
    core, cid, config = app_core
    request, run, _ = complete(core, cid)
    if kind == 'durable_dispatch':
        core.accept_goals('local', 'owner', cid, 'source', [Goal(id='goal', text='Make a tiny local page.')])
        core.dispatch('local', 'owner', cid, Dispatch(goal_id='goal', route_id='primary'))
    elif kind == 'same_run_goal':
        core.accept_turn('local', 'owner', cid, 'other-source', 'Another owned objective.')
        core.accept_goals('local', 'owner', cid, 'other-source', [Goal(id='goal', text='Another owned objective.')])
        with core.store._connect() as conn:
            conn.execute('UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=?',
                         (run['worker_id'], run['run_id'], cid))
    elif kind == 'standalone':
        with core.store._connect() as conn:
            conn.execute('UPDATE coordinator_conversations SET config_json=? WHERE conversation_id=?',
                         (canonical(config.model_copy(update={'application': None}).model_dump()), cid))
    elif kind == 'worker_results':
        with core.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET origin='worker_results' WHERE conversation_id=?", (cid,))
    elif kind == 'failed':
        core.store.update_provider_request(request['request_id'], state='failed')
    else:
        with core.store._connect() as conn:
            conn.execute('DELETE FROM provider_requests WHERE request_id=?', (request['request_id'],))
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    assert notices(core) == []


@pytest.mark.parametrize('kind,expected', [('stop', 'cancelled'), ('turn_hold', 'continuity_restore_hold'),
    ('conversation_hold', 'continuity_restore_hold'), ('request_hold', 'continuity_restore_hold'),
    ('goal_dispatched', 'source_changed'), ('digest', 'source_changed'), ('revision', 'source_changed'),
    ('request_changed', 'source_unavailable')])
def test_current_native_authority_fences_delivery_but_keeps_historical_metadata(app_core, kind, expected):
    core, cid, _ = app_core
    request, run, _ = complete(core, cid)
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    record = notices(core)[0]
    assert notification_fence(core.service, record) == ''
    if kind == 'stop':
        with core.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET blocker='cancelled' WHERE conversation_id=?", (cid,))
    elif kind in {'turn_hold', 'conversation_hold'}:
        table = 'coordinator_turns' if kind == 'turn_hold' else 'coordinator_conversations'
        with core.store._connect() as conn:
            conn.execute(f'UPDATE {table} SET restore_hold=1 WHERE conversation_id=?', (cid,))
    elif kind == 'request_hold':
        core.store.update_provider_request(request['request_id'], restore_hold=1)
    elif kind == 'goal_dispatched':
        core.accept_goals('local', 'owner', cid, 'source', [Goal(id='late-goal', text='Make a tiny local page.')])
        core.dispatch('local', 'owner', cid, Dispatch(goal_id='late-goal', route_id='primary'))
    elif kind == 'request_changed':
        with core.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET request_id='' WHERE conversation_id=?", (cid,))
    else:
        payload = json.loads(record['payload_json'])
        payload['data']['result_digest' if kind == 'digest' else 'result_revision'] = 'sha256:' + '0'*64 if kind == 'digest' else 99
        record['payload_json'] = canonical(payload)
    assert notification_fence(core.service, record) == expected
    retained = publications(core, 'local', 'owner', cid)['publications']
    assert len(retained) == 1 and retained[0]['type'] == 'coordinator.turn_result_ready'
    assert retained[0]['source_turn_id'] == 'source' and retained[0]['data']['request_id'] == request['request_id']


def test_same_transport_owner_claims_finishes_and_never_rearms_result_on_observation(app_core):
    core, cid, _ = app_core
    complete(core, cid)
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    record = notices(core)[0]
    assert record['callback_id'] in {row['callback_id'] for row in due_callback_records(core.store, '9999-01-01')}
    assert record['callback_id'] not in {row['callback_id'] for row in core.store.list_pending_callbacks()}
    claimed = core.store.claim_pending_callback(record['callback_id'])
    assert claimed is not None
    assert finish_callback(core.store, claimed, state='rejected', error='synthetic-receiver') is True
    retained = notices(core)[0]
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    assert notices(core) == [retained]
    assert retained['status'] == 'rejected' and retained['expires_at'] == record['expires_at']


def test_only_declared_canonical_file_envelope_is_projected(app_core):
    core, cid, _ = app_core
    request, run, response = complete(core, cid)
    result = completed_turn_result(core, 'local', 'owner', cid, 'source')
    envelope = {**result['publisher'], 'files': [{'filename': 'report.txt', 'sha256': '1'*64}]}
    response['glasshive']['output_files'] = envelope
    core.store.update_provider_request(request['request_id'], response_json=json.dumps(response))
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET response_json=? WHERE conversation_id=?', (canonical(response), cid))
    assert turn_result(core, 'local', 'owner', cid, 'source', request['request_id'], run['run_id'])['output_files'] == envelope
    envelope['owner_id'] = 'foreign'
    core.store.update_provider_request(request['request_id'], response_json=json.dumps(response))
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET response_json=? WHERE conversation_id=?', (canonical(response), cid))
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    assert notices(core) == []


def test_failed_interactive_turn_retains_existing_retry_contract(app_core):
    core, cid, _ = app_core
    request, _, _ = complete(core, cid)
    core.store.update_provider_request(request['request_id'], state='failed', response_json='')
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET response_json='',blocker='failed' WHERE conversation_id=?", (cid,))
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    assert notices(core) == []
    assert turn_status(core, 'local', 'owner', cid, 'source')['actions'] == ['retry']


def test_accepted_unstarted_intent_does_not_hide_a_completed_turn_or_complete_the_goal(app_core):
    core, cid, _ = app_core
    request, run, _ = complete(core, cid)
    core.accept_goals('local', 'owner', cid, 'source', [Goal(id='intent', text='Make a tiny local page.')])
    before = core._goal(cid, 'intent')
    assert all(before[key] == '' for key in ['dispatch_json', 'work_ref', 'worker_id', 'run_id'])
    result = turn_result(core, 'local', 'owner', cid, 'source', request['request_id'], run['run_id'])
    assert result['presentation_eligible'] is True and result['superseded'] is False
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    first = notices(core)
    assert len(first) == 1 and notification_fence(core.service, first[0]) == ''
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    assert notices(core) == first
    assert core._goal(cid, 'intent') == before
    assert core.goal_snapshot('local', 'owner', cid, 'intent')['state'] == 'accepted'


@pytest.mark.parametrize('binding,value', [('work_ref', 'synthetic-work'), ('worker_id', 'synthetic-worker'),
    ('run_id', 'synthetic-run')])
def test_same_source_real_work_binding_keeps_the_goal_result_owner(app_core, binding, value):
    core, cid, _ = app_core
    request, run, _ = complete(core, cid)
    core.accept_goals('local', 'owner', cid, 'source', [Goal(id='owned', text='Make a tiny local page.')])
    with core.store._connect() as conn:
        conn.execute(f'UPDATE coordinator_goals SET {binding}=? WHERE conversation_id=? AND goal_id=?',
                     (value, cid, 'owned'))
    result = turn_result(core, 'local', 'owner', cid, 'source', request['request_id'], run['run_id'])
    assert result['presentation_eligible'] is False and result['superseded'] is True
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    assert notices(core) == []


def test_another_source_dispatch_does_not_own_this_completed_turn(app_core):
    core, cid, _ = app_core
    request, run, _ = complete(core, cid)
    core.accept_turn('local', 'owner', cid, 'other-source', 'Another owned objective.')
    core.accept_goals('local', 'owner', cid, 'other-source', [Goal(id='other', text='Another owned objective.')])
    core.dispatch('local', 'owner', cid, Dispatch(goal_id='other', route_id='primary'))
    assert turn_result(core, 'local', 'owner', cid, 'source', request['request_id'], run['run_id'])['presentation_eligible'] is True
    queue_turn_results(core, core._conversation('local', 'owner', cid))
    assert len(notices(core)) == 1


def test_compact_status_keeps_exact_accepted_message_and_timestamp_for_work_labels(app_core):
    core, cid, _ = app_core
    original = 'Check the accepted source.\nKeep the exact wording and punctuation: café 🐝.'
    core.accept_turn('local', 'owner', cid, 'label-source', original)
    with core.store._connect() as conn:
        accepted = dict(conn.execute('SELECT message,created_at FROM coordinator_turns '
            'WHERE conversation_id=? AND turn_id=?', (cid, 'label-source')).fetchone())
    status = turn_status(core, 'local', 'owner', cid, 'label-source')
    assert status['message'] == original == accepted['message']
    assert status['created_at'] == accepted['created_at']
    assert 'response_json' not in status and 'payload_json' not in status
