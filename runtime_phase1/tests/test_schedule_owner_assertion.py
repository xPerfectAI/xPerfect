"""Shared scheduling routes preserve a verified courier principal, never local fallback."""
from __future__ import annotations

import time
import pytest
from test_account_api import account_client, account_headers, service_assertion, delegation_payload_with_origin


def setup_destination(client):
    response = client.post('/v1/webhook-destinations', headers=account_headers(), json={
        'idempotency_key': 'asserted-destination-one', 'name': 'Synthetic receiver',
        'url': 'https://receiver.example.test/hook', 'max_body_bytes': 65536})
    assert response.status_code == 201, response.text
    return response.json()


def test_verified_courier_principal_reaches_all_shared_schedule_consumers(account_client):
    service, store = account_client.app.state.service, account_client.app.state.store
    destination = setup_destination(account_client)
    assert service.webhook_destinations.get('tenant-a', 'owner-a', destination['destination_id'])
    assert service.webhook_destinations.get('local', 'demo-owner', destination['destination_id']) is None
    listed = account_client.get('/v1/webhook-destinations', headers=account_headers())
    assert [row['destination_id'] for row in listed.json()['items']] == [destination['destination_id']]
    assert account_client.get('/v1/webhook-destinations', headers=account_headers(owner_id='owner-b')).json()['items'] == []
    rotated = account_client.patch('/v1/webhook-destinations/' + destination['destination_id'], headers=account_headers(), json={'name': 'Same owner receiver'})
    assert rotated.status_code == 200, rotated.text
    created = account_client.post('/v1/recurring-schedules', headers=account_headers(), json={
        'target': {'kind': 'webhook', 'destination_id': destination['destination_id'], 'request_data': {'request_ref': 'source-one'}},
        'idempotency_key': 'asserted-schedule-one', 'recurrence_type': 'interval', 'interval_seconds': 3600, 'instruction': 'Future task'})
    assert created.status_code == 201, created.text
    definition = created.json(); path = '/v1/recurring-schedules/' + definition['definition_id']
    assert (definition['tenant_id'], definition['owner_id']) == ('tenant-a', 'owner-a')
    assert account_client.get(path, headers=account_headers()).status_code == 200
    assert account_client.get(path, headers=account_headers(owner_id='owner-b')).status_code == 404
    assert len(account_client.get('/v1/recurring-schedules', headers=account_headers()).json()['items']) == 1
    assert account_client.get('/v1/recurring-schedules', headers=account_headers(owner_id='owner-b')).json()['items'] == []
    changed = account_client.patch(path, headers=account_headers(), json={'expected_version': definition['version'], 'instruction': 'Updated future task'})
    assert changed.status_code == 200, changed.text
    manual = account_client.post(path + '/run-now', headers=account_headers(), json={'idempotency_key': 'asserted-manual-one'})
    assert manual.status_code in {200, 202}, manual.text
    history = account_client.get(path + '/occurrences', headers=account_headers())
    assert history.status_code == 200 and len(history.json()['items']) == 1
    assert history.json()['items'][0]['owner_id'] == 'owner-a'
    paused = account_client.post(path + '/deactivate', headers=account_headers())
    assert paused.status_code == 200, paused.text
    with store._connect() as connection:
        principal = connection.execute("SELECT tenant_id,owner_id FROM callback_outbox WHERE event_type='schedule.due'").fetchone()
    assert tuple(principal) == ('tenant-a', 'owner-a')


@pytest.mark.parametrize('fault', ['forged', 'expired', 'wrong-bearer'])
def test_invalid_supplied_assertion_never_falls_back_to_local_operator(account_client, fault):
    assertion = service_assertion(secret='incorrect-signing-secret') if fault == 'forged' else service_assertion(now=int(time.time())-61) if fault == 'expired' else service_assertion()
    headers = account_headers(assertion=assertion)
    if fault == 'wrong-bearer': headers['Authorization'] = 'Bearer incorrect-token'
    for path in ('/v1/webhook-destinations', '/v1/recurring-schedules', '/v1/workers/unknown/recurring-schedules'):
        response = account_client.get(path, headers=headers)
        assert response.status_code == 401, response.text
    assert account_client.app.state.service.list_webhook_destinations(tenant_id='local', owner_id='demo-owner') == []


def test_shared_schedule_mutations_consume_existing_assertion_nonce(account_client):
    headers = account_headers(assertion=service_assertion(nonce='schedule-owner-nonce-one'))
    body = {'name': 'Synthetic receiver', 'url': 'https://receiver.example.test/hook'}
    assert account_client.post('/v1/webhook-destinations', headers=headers, json=body).status_code == 201
    replay = account_client.post('/v1/webhook-destinations', headers=headers, json=body)
    assert replay.status_code == 409, replay.text
    assert replay.json()['detail']['code'] == 'service_assertion_replayed'


def test_existing_worker_schedule_api_uses_same_verified_courier_owner(account_client, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_RECURRING_SCHEDULE_OWNER', 'native')
    service = account_client.app.state.service
    service.executor.submit = lambda *args, **kwargs: None
    accepted = account_client.post('/v1/delegations', headers=account_headers(idempotency_key='asserted-worker-launch'), json=delegation_payload_with_origin()).json()
    record = account_client.app.state.store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    path = '/v1/workers/' + record['worker_id'] + '/recurring-schedules'
    response = account_client.post(path, headers=account_headers(), json={'instruction': 'Native future task', 'recurrence_type': 'interval', 'interval_seconds': 3600})
    assert response.status_code == 201, response.text
    assert response.json()['owner_id'] == 'owner-a' and response.json()['tenant_id'] == 'tenant-a'
    assert len(account_client.get(path, headers=account_headers()).json()['items']) == 1
    assert account_client.get(path, headers=account_headers(owner_id='owner-b')).status_code == 404
