"""Retained result metadata reuses the authenticated coordinator/outbox owners."""
import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_coordinator_application import app_core
from test_coordinator_voice_result_proof import completed_goal
from workers_projects_runtime import coordinator_application as application
from workers_projects_runtime.coordinator import Goal, canonical
from workers_projects_runtime.coordinator_api import install_coordinator_routes


def client_for(core):
    app = FastAPI()
    install_coordinator_routes(app, core,
        lambda request: (request.headers.get('x-test-tenant', 'local'),
                         request.headers.get('x-test-owner', 'owner')),
        lambda *args: None)
    return TestClient(app)


def publish(core, config, *, tenant='local', owner='owner', output='Synthetic private result body.'):
    cid, run = completed_goal(core, config, tenant=tenant, owner=owner, output=output)
    application.queue_self_results(core, core._conversation(tenant, owner, cid))
    return cid, run


def append_goal(core, cid, run, goal_id):
    core.accept_goals('local', 'owner', cid, 'source',
                      [Goal(id=goal_id, text='Make a tiny local page.')])
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_goals SET worker_id=?,run_id=? '
                     'WHERE conversation_id=? AND goal_id=?',
                     (run['worker_id'], run['run_id'], cid, goal_id))
    application.queue_self_results(core, core._conversation('local', 'owner', cid))


def callback_rows(core):
    with core.store._connect() as conn:
        return [dict(row) for row in conn.execute('SELECT rowid,* FROM callback_outbox ORDER BY rowid')]


def test_same_source_results_project_exact_metadata_without_private_transport_or_result(app_core):
    core, _, config = app_core
    cid, run = publish(core, config, output='PRIVATE-RESULT-CANARY')
    append_goal(core, cid, run, 'another')
    with core.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET last_error='PRIVATE-TRANSPORT-CANARY' "
                     "WHERE event_type='coordinator.result_ready'")
    before = callback_rows(core)
    admitted = len(core.provider.received)
    response = client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['conversation_id'] == cid and result['next_cursor'] is None
    assert len(result['publications']) == 2
    assert {item['data']['goal_id'] for item in result['publications']} == {'goal', 'another'}
    for item in result['publications']:
        original = next(row for row in before if row['callback_id'] == item['event_id'])
        payload = json.loads(original['payload_json'])
        assert set(item) == {'event_id', 'type', 'created_at', 'expires_at', 'source_turn_id', 'data'}
        assert item['source_turn_id'] == 'source'
        assert {key: value for key, value in item.items() if key != 'source_turn_id'} == {
            key: payload[key] for key in ('event_id', 'type', 'created_at', 'expires_at', 'data')}
        assert item['data']['run_id'] == run['run_id']
        assert set(item['data']) == {'conversation_id', 'request_ref', 'goal_id', 'run_id',
                                     'result_revision', 'result_digest'}
    assert 'PRIVATE-RESULT-CANARY' not in response.text
    assert 'PRIVATE-TRANSPORT-CANARY' not in response.text
    assert all(row['url'] not in response.text for row in before)
    assert callback_rows(core) == before
    assert len(core.provider.received) == admitted


@pytest.mark.parametrize('tenant,owner', [('local', 'unrelated-owner'), ('unrelated-tenant', 'owner')])
def test_publications_use_existing_authenticated_conversation_scope(app_core, tenant, owner):
    core, _, config = app_core
    cid, _ = publish(core, config)
    response = client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications',
        headers={'x-test-tenant': tenant, 'x-test-owner': owner})
    assert response.status_code == 403
    assert 'publications' not in response.json()


def test_other_owned_conversations_and_other_event_types_are_not_publications(app_core):
    core, _, config = app_core
    cid, _ = publish(core, config)
    other, _ = publish(core, config, output='A different owned result body with a distinct binding.')
    client = client_for(core)
    selected = client.get(f'/v1/coordinator/conversations/{cid}/publications').json()
    foreign = client.get(f'/v1/coordinator/conversations/{other}/publications').json()
    assert selected['publications'] and foreign['publications']
    assert all(item['data']['conversation_id'] == cid for item in selected['publications'])
    foreign_cursor = foreign['publications'][0]['event_id']
    assert client.get(f'/v1/coordinator/conversations/{cid}/publications',
        params={'cursor': foreign_cursor}).status_code == 409
    with core.store._connect() as conn:
        turn = conn.execute("SELECT callback_id FROM callback_outbox WHERE event_type='coordinator.turn_due' "
                            "AND json_extract(payload_json,'$.data.conversation_id')=?", (cid,)).fetchone()
    # A typed event outside the requested publication stream is not a valid cursor.
    if turn:
        assert client.get(f'/v1/coordinator/conversations/{cid}/publications',
            params={'cursor': turn['callback_id']}).status_code == 409
    assert all(item['type'] == 'coordinator.result_ready' for item in selected['publications'])


def test_cursor_pages_existing_rows_and_equal_timestamp_appends_once(app_core, monkeypatch):
    core, _, config = app_core
    monkeypatch.setattr(application, 'now', lambda: '2026-01-01T00:00:00+00:00')
    cid, run = publish(core, config)
    append_goal(core, cid, run, 'second')
    client = client_for(core)
    url = f'/v1/coordinator/conversations/{cid}/publications'
    first = client.get(url, params={'limit': 1}).json()
    assert len(first['publications']) == 1 and first['next_cursor']
    # This insertion has the same timestamp; the existing outbox insertion identity owns order.
    append_goal(core, cid, run, 'third')
    pages = [first]
    while pages[-1]['next_cursor']:
        response = client.get(url, params={'limit': 1, 'cursor': pages[-1]['next_cursor']})
        assert response.status_code == 200, response.text
        pages.append(response.json())
    events = [item for page in pages for item in page['publications']]
    assert [item['data']['goal_id'] for item in events] == ['goal', 'second', 'third']
    assert len({item['event_id'] for item in events}) == 3
    assert client.get(url, params={'cursor': events[-1]['event_id']}).json() == {
        'conversation_id': cid, 'publications': [], 'next_cursor': None}
    assert client.get(url, params={'cursor': 'nonexistent-event'}).status_code == 409


@pytest.mark.parametrize('limit', [0, -1, 201])
def test_invalid_page_bound_is_rejected_without_writes(app_core, limit):
    core, _, config = app_core
    cid, _ = publish(core, config)
    before = callback_rows(core)
    assert client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications',
        params={'limit': limit}).status_code == 409
    assert callback_rows(core) == before


def test_expired_rejected_and_superseded_publications_are_only_retained_identity(app_core, monkeypatch):
    core, _, config = app_core

    class OldClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2020, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(application, 'datetime', OldClock)
    cid, original = publish(core, config)
    later = core.store.create_run(original['worker_id'], original['project_id'], 'Owned later result')
    core.store.update_run(later['run_id'], state='completed', output_text=original['output_text'],
                          terminal_result_revision=12)
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_goals SET run_id=? WHERE conversation_id=?', (later['run_id'], cid))
        conn.execute('UPDATE provider_requests SET run_id=? WHERE run_id=?',
                     (later['run_id'], original['run_id']))
    application.queue_self_results(core, core._conversation('local', 'owner', cid))
    with core.store._connect() as conn:
        records = conn.execute("SELECT callback_id FROM callback_outbox WHERE event_type='coordinator.result_ready' "
                               "ORDER BY rowid").fetchall()
        for index, row in enumerate(records):
            conn.execute('UPDATE callback_outbox SET status=?,attempts=9 WHERE callback_id=?',
                         ('expired' if index == 0 else 'rejected', row['callback_id']))
    before = callback_rows(core)
    response = client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications')
    assert response.status_code == 200, response.text
    items = response.json()['publications']
    assert {item['data']['run_id'] for item in items} == {original['run_id'], later['run_id']}
    assert all(item['expires_at'].startswith('2020-') for item in items)
    assert core._goal(cid, 'goal')['run_id'] == later['run_id']
    assert callback_rows(core) == before


@pytest.mark.parametrize('change', ['principal', 'event_id', 'type', 'request_ref', 'revision', 'extra_data'])
def test_invalid_retained_identity_never_becomes_typed_provenance(app_core, change):
    core, _, config = app_core
    cid, _ = publish(core, config)
    original = next(row for row in callback_rows(core) if row['event_type'] == 'coordinator.result_ready')
    payload = json.loads(original['payload_json'])
    event_id = 'synthetic-invalid-publication'
    payload['event_id'] = event_id
    if change == 'principal':
        payload['principal']['owner_id'] = 'unrelated-owner'
    elif change == 'event_id':
        payload['event_id'] = 'different-event'
    elif change == 'type':
        payload['type'] = 'coordinator.turn_due'
    elif change == 'request_ref':
        payload['data']['request_ref'] = 'different-binding'
    elif change == 'revision':
        payload['data']['result_revision'] = True
    else:
        payload['data']['secret'] = 'PRIVATE-DATA-CANARY'
    with core.store._connect() as conn:
        conn.execute('INSERT INTO callback_outbox '
            '(callback_id,tenant_id,owner_id,event_type,url,payload_json,status,created_at,updated_at,'
            'destination_id,next_attempt_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (event_id, 'local', 'owner', 'coordinator.result_ready', original['url'], canonical(payload),
             'expired', original['created_at'], original['updated_at'], original['destination_id'],
             original['next_attempt_at'], original['expires_at']))
    before = callback_rows(core)
    response = client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications')
    assert response.status_code == 409
    assert 'PRIVATE-DATA-CANARY' not in response.text and 'publications' not in response.json()
    assert callback_rows(core) == before


def test_standalone_conversation_has_no_application_publications(app_core):
    core, _, config = app_core
    cid = core.create('local', 'owner', config.model_copy(update={'application': None}))['conversation_id']
    response = client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications')
    assert response.status_code == 200
    assert response.json() == {'conversation_id': cid, 'publications': [], 'next_cursor': None}


def test_nondefault_tenant_reads_only_its_existing_publication(app_core):
    core, _, config = app_core
    cid, _ = publish(core, config, tenant='synthetic-tenant', owner='synthetic-owner')
    response = client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications',
        headers={'x-test-tenant': 'synthetic-tenant', 'x-test-owner': 'synthetic-owner'})
    assert response.status_code == 200, response.text
    assert len(response.json()['publications']) == 1
    assert response.json()['publications'][0]['source_turn_id'] == 'source'


def test_missing_retained_goal_source_is_unavailable_instead_of_silent_empty(app_core):
    core, _, config = app_core
    cid, _ = publish(core, config)
    with core.store._connect() as conn:
        conn.execute('DELETE FROM coordinator_goals WHERE conversation_id=?', (cid,))
    response = client_for(core).get(f'/v1/coordinator/conversations/{cid}/publications')
    assert response.status_code == 409


def test_worker_cleanup_retains_unbound_coordinator_publications(app_core):
    core, _, config = app_core
    cid, result = publish(core, config)
    client = client_for(core)
    url = f'/v1/coordinator/conversations/{cid}/publications'
    before_projection = client.get(url).json()
    before = [row for row in callback_rows(core) if row['event_type'] == 'coordinator.result_ready']
    assert before and all(row['worker_id'] is None for row in before)
    ordinary = core.store.create_worker(result['project_id'], 'owner', 'Synthetic disposable worker',
        'helper', 'codex-cli', 'synthetic', 'stub', 'synthetic-model', workspace_kind='ephemeral')
    with core.store._connect() as conn:
        conn.execute("UPDATE workers SET state='terminated',updated_at='2020-01-01T00:00:00+00:00' "
                     'WHERE worker_id=?', (ordinary['worker_id'],))
    current = datetime.now(UTC)
    claimed = core.store.claim_ephemeral_workspace_gc(ordinary['worker_id'],
        claim_token='synthetic-gc-claim', updated_before=current.isoformat(),
        now_epoch=current.timestamp(), claim_ttl_s=60)
    assert claimed is not None
    cleaned = core.store.finalize_ephemeral_workspace_gc(ordinary['worker_id'],
        claim_token='synthetic-gc-claim', updated_before=current.isoformat(),
        now_epoch=current.timestamp())
    assert cleaned is not None and core.store.get_worker(ordinary['worker_id']) is None
    after = [row for row in callback_rows(core) if row['event_type'] == 'coordinator.result_ready']
    assert after == before
    assert core._goal(cid, 'goal')['source_turn_id'] == 'source'
    assert client.get(url).json() == before_projection
