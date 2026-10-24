from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from test_account_api import (
    account_client, account_headers, delegation_payload_with_origin,
    _truthfully_invoke_run,
)


@pytest.fixture(autouse=True)
def manual_transport_owner(monkeypatch):
    # These recovery-CAS tests dispatch explicitly. Periodic startup/replay must
    # not race the synthetic retained rows or use a real callback receiver.
    from workers_projects_runtime.service import WorkersProjectsService
    monkeypatch.setattr(WorkersProjectsService, "start_background_consumers", lambda self: None)


def retained_result(client):
    service = client.app.state.service
    service.executor.submit = lambda *args, **kwargs: None
    submit_transport = service._submit_callback_delivery
    service._submit_callback_delivery = lambda *args, **kwargs: False
    payload = delegation_payload_with_origin()
    payload["bootstrapBundle"]["callbacks"]["hmac_secret"] = "synthetic-secret"
    accepted = client.post("/v1/delegations", headers=account_headers(
        idempotency_key="retained-terminal-result"), json=payload)
    assert accepted.status_code == 202, accepted.text
    store = client.app.state.store
    work = store.get_delegation(accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    run = _truthfully_invoke_run(store, work["current_run_id"], suffix="callback-recovery")
    run = store.update_run(run["run_id"], state="completed", output_text="Useful retained result.",
                           ended_at=datetime.now(timezone.utc).isoformat())
    worker = store.get_worker(work["worker_id"])
    callback = service._emit_callback(worker, "run.completed", run=run,
                                     message="Useful retained result.", submit_delivery=False)
    store.mark_callback_dead_lettered(callback["callback_id"], attempts=27,
        payload_json=callback["payload_json"], last_error="synthetic unavailable receiver")
    with store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET updated_at = ? WHERE callback_id = ?",
            ((datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(), callback["callback_id"]))
    body = {"originRef": work["origin_ref"], "workRef": work["work_ref"],
            "workerId": work["worker_id"], "runId": run["run_id"],
            "callbackId": callback["callback_id"], "resultRevision": callback["result_revision"],
            "resultDigest": callback["result_digest"]}
    service._submit_callback_delivery = submit_transport
    return store, service, body, callback


def row(store, body):
    with store._connect() as conn:
        return dict(conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?",
                                (body["callbackId"],)).fetchone())


def test_recovery_preserves_history_and_one_owned_delivery_lease(account_client):
    store, service, body, original = retained_result(account_client)
    tasks = []
    service.schedule_transport_executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
    first = account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body)
    assert first.status_code == 202, first.text
    assert first.json() == {"state": "delivering"}
    claimed = row(store, body)
    assert claimed["attempts"] == 27
    assert claimed["payload_json"] == original["payload_json"]
    assert claimed["status"] == "delivering"
    assert claimed["delivery_lease_token"]
    second = account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body)
    assert second.status_code == 200
    assert second.json() == {"state": "unchanged"}
    assert len(tasks) == 1
    assert row(store, body)["delivery_generation"] == claimed["delivery_generation"]


@pytest.mark.parametrize("field", ["owner", "originRef", "resultDigest", "resultRevision"])
def test_recovery_cannot_promote_foreign_or_mismatched_results(account_client, field):
    store, service, body, _ = retained_result(account_client)
    original = row(store, body)
    headers = account_headers(owner_id="owner-b" if field == "owner" else "owner-a")
    changed = dict(body)
    if field == "originRef": changed[field] = "ghi_synthetic_wrong_origin"
    if field == "resultDigest": changed[field] = "sha256:" + "f" * 64
    if field == "resultRevision": changed[field] += 1
    response = account_client.post("/v1/callback-associations/recover", headers=headers, json=changed)
    assert response.status_code in (200, 404), response.text
    assert row(store, body) == original


def test_recovery_rejects_superseded_result_and_unsigned_account(account_client):
    store, service, body, _ = retained_result(account_client)
    store.update_run(body["runId"], output_text="A newer durable result.")
    response = account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body)
    assert response.status_code == 200
    assert response.json() == {"state": "unchanged"}
    assert row(store, body)["status"] == "dead_lettered"
    assert account_client.post("/v1/callback-associations/recover", json=body).status_code == 401


def test_recovery_failure_keeps_attempts_and_cooldown_then_uses_original_signer(account_client, monkeypatch):
    store, service, body, original = retained_result(account_client)
    tasks = []
    requests = []
    service.schedule_transport_executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
    def post(url, *, content, headers, timeout):
        requests.append((content, headers))
        return httpx.Response(500, request=httpx.Request("POST", url))
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    assert account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body).status_code == 202
    args, kwargs = tasks.pop(); args[0](*args[1:], **kwargs)
    failed = row(store, body)
    assert failed["attempts"] == 28
    assert failed["status"] == "dead_lettered"
    assert len(requests) == 1
    assert failed["payload_json"] == original["payload_json"]
    assert account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body).json() == {"state": "unchanged"}
    assert not tasks
    delivered, headers = requests[0]
    payload = json.loads(delivered)
    assert payload["callback_id"] == body["callbackId"]
    assert payload["result_digest"] == body["resultDigest"]
    worker = store.get_worker(body["workerId"])
    expected = service._callback_headers(service._callback_config_for(worker), payload, delivered)
    assert headers == expected
    assert headers["X-GlassHive-Signature"].startswith("sha256=")


def test_recovered_result_acceptance_is_exact_and_does_not_send_twice(account_client, monkeypatch):
    store, service, body, original = retained_result(account_client)
    tasks = []
    received = []
    service.schedule_transport_executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
    def post(url, *, content, headers, timeout):
        payload = json.loads(content)
        received.append(payload)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "callback_status": "idempotent", "callback_id": payload["callback_id"],
            "run_id": payload["run_id"], "result_revision": payload["result_revision"],
            "result_digest": payload["result_digest"],
            "current_result_revision": payload["result_revision"],
            "current_callback_id": payload["callback_id"],
            "current_result_digest": payload["result_digest"],
        })
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    assert account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body).status_code == 202
    args, kwargs = tasks.pop(); args[0](*args[1:], **kwargs)
    assert row(store, body)["status"] == "http_accepted"
    assert row(store, body)["attempts"] == 28
    assert row(store, body)["payload_json"] == original["payload_json"]
    assert len(received) == 1
    assert received[0]["message"] == "Useful retained result."
    assert account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body).json() == {"state": "unchanged"}
    assert not tasks


def test_publisher_retry_signs_immutable_creation_clock_and_refreshes_send_clock(
    account_client, monkeypatch
):
    store, service, body, original = retained_result(account_client)
    tasks, received = [], []
    service.schedule_transport_executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
    assert account_client.post("/v1/callback-associations/recover",
        headers=account_headers(), json=body).status_code == 202
    args, kwargs = tasks.pop()
    clock = [1_900_000_001]
    monkeypatch.setattr("workers_projects_runtime.service.time.time", lambda: clock[0])
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")

    def post(url, *, content, headers, timeout):
        payload = json.loads(content)
        received.append((payload, content, headers))
        if len(received) == 1:
            clock[0] += 7
            return httpx.Response(500, request=httpx.Request("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "callback_status": "accepted", "callback_id": payload["callback_id"],
            "run_id": payload["run_id"], "result_revision": payload["result_revision"],
            "result_digest": payload["result_digest"],
            "current_result_revision": payload["result_revision"],
            "current_callback_id": payload["callback_id"],
            "current_result_digest": payload["result_digest"],
        })

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    service._deliver_claimed_callback_record(args[1], args[2], args[3], delivery_attempts=2)
    from workers_projects_runtime import schedule_targets
    schedule_targets.release_transport(service, args[4])
    assert len(received) == 2
    assert [item[0]["callback_ts"] for item in received] == [1_900_000_001, 1_900_000_008]
    assert {item[0]["callback_created_at"] for item in received} == {original["created_at"]}
    worker = store.get_worker(body["workerId"])
    callbacks = service._callback_config_for(worker)
    for payload, encoded, headers in received:
        assert headers == service._callback_headers(callbacks, payload, encoded)
        changed = {**payload, "callback_created_at": "2000-01-01T00:00:00+00:00"}
        changed_signature = service._callback_headers(
            callbacks, changed, service._encode_callback_payload(changed)
        )["X-GlassHive-Signature"]
        assert headers["X-GlassHive-Signature"] != changed_signature
    assert row(store, body)["status"] == "http_accepted"
    assert row(store, body)["created_at"] == original["created_at"]
    assert row(store, body)["payload_json"] == original["payload_json"]


@pytest.mark.parametrize("legacy_clock", [None, ""])
def test_publisher_legacy_record_does_not_invent_creation_clock(
    account_client, monkeypatch, legacy_clock
):
    store, service, body, original = retained_result(account_client)
    tasks, received = [], []
    service.schedule_transport_executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
    assert account_client.post("/v1/callback-associations/recover",
        headers=account_headers(), json=body).status_code == 202
    args, kwargs = tasks.pop()
    claimed = {**args[2], "created_at": legacy_clock}
    payload = json.loads(claimed["payload_json"])
    payload["callback_created_at"] = "2000-01-01T00:00:00+00:00"
    claimed["payload_json"] = json.dumps(payload)

    def post(url, *, content, headers, timeout):
        delivered = json.loads(content)
        received.append(delivered)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "callback_status": "accepted", "callback_id": delivered["callback_id"],
            "run_id": delivered["run_id"], "result_revision": delivered["result_revision"],
            "result_digest": delivered["result_digest"],
            "current_result_revision": delivered["result_revision"],
            "current_callback_id": delivered["callback_id"],
            "current_result_digest": delivered["result_digest"],
        })

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    args[0](args[1], claimed, args[3], args[4], **kwargs)
    assert len(received) == 1
    assert "callback_created_at" not in received[0]
    assert received[0]["callback_ts"] > 0
    assert row(store, body)["status"] == "http_accepted"
    assert row(store, body)["created_at"] == original["created_at"]


def test_recovery_failure_cannot_overwrite_a_new_delivery_lease(account_client, monkeypatch):
    store, service, body, _ = retained_result(account_client)
    tasks = []
    service.schedule_transport_executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
    assert account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body).status_code == 202
    def post(url, *, content, headers, timeout):
        with store._connect() as conn:
            conn.execute("UPDATE callback_outbox SET delivery_generation = delivery_generation + 1, delivery_lease_token = 'synthetic-new-lease' WHERE callback_id = ?", (body["callbackId"],))
        return httpx.Response(500, request=httpx.Request("POST", url))
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    args, kwargs = tasks.pop(); args[0](*args[1:], **kwargs)
    current = row(store, body)
    assert current["status"] == "delivering"
    assert current["attempts"] == 27
    assert current["delivery_lease_token"] == "synthetic-new-lease"


def retained_stop(client):
    service = client.app.state.service
    service.executor.submit = lambda *args, **kwargs: None
    submit_transport = service._submit_callback_delivery
    service._submit_callback_delivery = lambda *args, **kwargs: False
    accepted = client.post("/v1/delegations", headers=account_headers(
        idempotency_key="retained-work-stop"), json=delegation_payload_with_origin())
    assert accepted.status_code == 202, accepted.text
    store = client.app.state.store
    work = store.get_delegation(accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    response = client.post(f"/v1/work/{work['work_ref']}/actions", headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "stop-retained-once"})
    assert response.status_code == 202, response.text
    service._replay_pending_lifecycle_effects()
    with store._connect() as conn:
        original = dict(conn.execute("SELECT * FROM callback_outbox WHERE run_id = ? AND event_type = 'run.cancelled'",
                                    (work["current_run_id"],)).fetchone())
    assert original["callback_id"].startswith("cb_effect_ope_")
    store.mark_callback_dead_lettered(original["callback_id"], attempts=25,
        payload_json=original["payload_json"], last_error="synthetic receiver conflict")
    with store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET updated_at = ? WHERE callback_id = ?",
            ((datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(), original["callback_id"]))
    detail = client.get(f"/v1/work/{work['work_ref']}", headers=account_headers()).json()
    trace = next(item for item in reversed(detail["callbackDeliveries"])
                 if item["event"] == "run.cancelled" and item["status"] == "dead_lettered")
    body = {"originRef": work["origin_ref"], "workRef": work["work_ref"],
            "workerId": work["worker_id"], "runId": work["current_run_id"],
            **{key: trace[key] for key in ("callbackRef", "payloadSha256", "authoritySha256")}}
    service._submit_callback_delivery = submit_transport
    return store, service, body, original


def test_retained_stop_recovers_original_callback_once_through_original_signer(account_client, monkeypatch):
    store, service, body, original = retained_stop(account_client)
    tasks, received = [], []
    service.schedule_transport_executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
    def post(url, *, content, headers, timeout):
        received.append((json.loads(content), headers))
        return httpx.Response(202, request=httpx.Request("POST", url), json={"status": "http_accepted"})
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    response = account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body)
    assert response.status_code == 202, response.text
    assert account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body).json() == {"state": "unchanged"}
    assert len(tasks) == 1
    args, kwargs = tasks.pop(); args[0](*args[1:], **kwargs)
    current = row(store, {"callbackId": original["callback_id"]})
    assert current["status"] == "http_accepted"
    assert current["attempts"] == 26
    assert current["payload_json"] == original["payload_json"]
    assert len(received) == 1
    payload, headers = received[0]
    assert payload["callback_id"] == original["callback_id"]
    assert payload["event"] == "run.cancelled"
    wire = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    worker = store.get_worker(body["workerId"])
    assert headers == service._callback_headers(service._callback_config_for(worker), payload, wire)
    assert account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body).json() == {"state": "unchanged"}
    assert not tasks


@pytest.mark.parametrize("field", ["owner", "originRef", "runId", "payloadSha256", "authoritySha256", "stop_operation", "effect", "run_state"])
def test_retained_stop_recovery_keeps_control_and_payload_fences(account_client, field):
    store, service, body, original = retained_stop(account_client)
    before = row(store, {"callbackId": original["callback_id"]})
    headers = account_headers(owner_id="owner-b" if field == "owner" else "owner-a")
    changed = dict(body)
    if field == "originRef": changed[field] = "ghi_synthetic_other_origin"
    if field == "runId": changed[field] = "run_synthetic_other"
    if field in ("payloadSha256", "authoritySha256"): changed[field] = "sha256:" + "f" * 64
    with store._connect() as conn:
        if field == "stop_operation": conn.execute("UPDATE workers SET work_stop_id = 'other-stop' WHERE worker_id = ?", (body["workerId"],))
        if field == "effect": conn.execute("UPDATE lifecycle_operation_effects SET status = 'pending' WHERE effect_id = ?", (original["callback_id"].removeprefix("cb_effect_"),))
        if field == "run_state": conn.execute("UPDATE runs SET state = 'completed' WHERE run_id = ?", (body["runId"],))
    response = account_client.post("/v1/callback-associations/recover", headers=headers, json=changed)
    assert response.status_code in (200, 404), response.text
    assert row(store, {"callbackId": original["callback_id"]}) == before
    assert account_client.post("/v1/callback-associations/recover", json=body).status_code == 401


def _owned_cancellation_fixture(client, *, key):
    service, store = client.app.state.service, client.app.state.store
    service.executor.submit = lambda *args, **kwargs: None
    payload = delegation_payload_with_origin()
    payload['bootstrapBundle']['callbacks']['hmac_secret'] = 'synthetic-secret'
    accepted = client.post('/v1/delegations', headers=account_headers(idempotency_key=key), json=payload)
    assert accepted.status_code == 202, accepted.text
    work = store.get_delegation(accepted.json()['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    return store, service, work, store.get_worker(work['worker_id'])


@pytest.mark.parametrize('state', ['queued', 'running', 'needs_input', 'paused'])
def test_owner_termination_publishes_each_actual_cancelled_run_once(account_client, state):
    store, service, work, worker = _owned_cancellation_fixture(account_client, key='termination-' + state)
    run_id = work['current_run_id']
    if state != 'queued':
        _truthfully_invoke_run(store, run_id, suffix='termination-' + state)
        if state != 'running': store.update_run(run_id, state=state)
    # A pre-existing terminal result must not be overwritten or reclassified.
    prior = store.create_run(worker['worker_id'], worker['project_id'], 'Prior completed result', state='completed')
    old_prior = store.get_run(prior['run_id'])
    result = service.terminate_worker(worker['worker_id'])
    assert result['state'] == 'terminated'
    service._replay_pending_lifecycle_effects()
    service.terminate_worker(worker['worker_id'])
    service._replay_pending_lifecycle_effects()
    with store._connect() as connection:
        callbacks = connection.execute("SELECT * FROM callback_outbox WHERE run_id=? AND event_type='run.cancelled'", (run_id,)).fetchall()
        worker_effects = connection.execute("SELECT status FROM lifecycle_operation_effects WHERE worker_id=? AND effect_kind='callback.worker_terminated'", (worker['worker_id'],)).fetchall()
    assert len(callbacks) == 1
    receipt = dict(callbacks[0]); terminal = store.get_run(run_id)
    body = json.loads(receipt['payload_json'])
    assert body['event'] == 'run.cancelled'
    assert body['run_id'] == run_id
    assert receipt['result_revision'] == terminal['terminal_result_revision'] > 0
    assert receipt['result_digest']
    assert store.get_run(prior['run_id']) == old_prior
    assert worker_effects and all(effect['status'] == 'applied' for effect in worker_effects)


def test_termination_cancellation_sink_waits_for_compute_confirmation(account_client, monkeypatch):
    store, service, work, worker = _owned_cancellation_fixture(account_client, key='termination-failed-compute')
    run_id = work['current_run_id']
    def failed_stop(_worker): raise RuntimeError('Synthetic native compute is still active')
    original = service.runtime.terminate_worker
    monkeypatch.setattr(service.runtime, 'terminate_worker', failed_stop)
    with pytest.raises(RuntimeError, match='still active'):
        service.terminate_worker(worker['worker_id'])
    service._replay_pending_lifecycle_effects()
    assert service._reconcile_terminal_callback_intents() == 0
    with store._connect() as connection:
        assert not connection.execute("SELECT 1 FROM callback_outbox WHERE run_id=? AND event_type='run.cancelled'", (run_id,)).fetchone()
    monkeypatch.setattr(service.runtime, 'terminate_worker', original)
    service.terminate_worker(worker['worker_id'])
    # Skip the retry delay only in this synthetic local fixture.
    with store._connect() as connection:
        connection.execute("UPDATE lifecycle_operation_effects SET next_attempt_at=NULL WHERE worker_id=?", (worker['worker_id'],))
    service._replay_pending_lifecycle_effects()
    with store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM callback_outbox WHERE run_id=? AND event_type='run.cancelled'", (run_id,)).fetchone()[0] == 1


def test_claim_termination_enqueues_all_cancelled_run_sinks_atomically(account_client):
    store, service, work, worker = _owned_cancellation_fixture(account_client, key='termination-exact-claim')
    run_id = work['current_run_id']
    current = store.get_worker(worker['worker_id'])
    claim = store.try_claim_worker_compute_release(worker['worker_id'],
        expected_updated_at=current['updated_at'], expected_last_run_id=current.get('last_run_id') or '',
        expected_state=current['state'], expected_container_id='', owner=service.executor_id,
        ttl_s=30, kind='terminate_worker', target_run_id=run_id, expected_target_started_at='')
    assert claim
    result = store.finalize_worker_termination_claim(worker['worker_id'], claim['token'], claim['epoch'],
        compute_released_at=datetime.now(timezone.utc).isoformat(), runtime_fields={}, error_text='Synthetic owner termination')
    assert result and result['state'] == 'terminated'
    service._replay_pending_lifecycle_effects()
    with store._connect() as connection:
        rows = connection.execute("SELECT * FROM callback_outbox WHERE run_id=? AND event_type='run.cancelled'", (run_id,)).fetchall()
    assert len(rows) == 1
    assert dict(rows[0])['result_digest']
    assert store.finalize_worker_termination_claim(worker['worker_id'], claim['token'], claim['epoch'],
        compute_released_at=datetime.now(timezone.utc).isoformat(), runtime_fields={}, error_text='Duplicate') is None


@pytest.mark.parametrize('canonical_origin_only', [False, True])
@pytest.mark.parametrize('legacy_effect_applied', [False, True])
def test_retained_worker_only_termination_effect_recovers_legacy_cancelled_result(account_client, monkeypatch, canonical_origin_only, legacy_effect_applied):
    store, service, work, worker = _owned_cancellation_fixture(account_client, key='termination-legacy-recovery')
    run_id = work['current_run_id']
    if canonical_origin_only:
        bundle = json.loads(worker['bootstrap_bundle_json'])
        bundle['callbacks'] = {'origin_ref': bundle['callbacks']['origin_ref']}
        store.update_worker(worker['worker_id'], bootstrap_bundle_json=json.dumps(bundle))
        monkeypatch.setenv('GLASSHIVE_EVENTS_WEBHOOK_URL', 'https://callback.example.invalid/events')
        monkeypatch.setenv('GLASSHIVE_EVENTS_HMAC_SECRET', 'synthetic-canonical-signer')
    _truthfully_invoke_run(store, run_id, suffix='legacy-termination')
    store.update_worker(worker['worker_id'], last_run_id=run_id)
    if canonical_origin_only:
        for historical_state in ('completed', 'failed', 'cancelled'):
            historical = store.create_run(worker['worker_id'], worker['project_id'],
                'Synthetic historical result', state=historical_state)
            store.update_run(historical['run_id'], ended_at=datetime.now(timezone.utc).isoformat())
        store.update_worker(worker['worker_id'], last_run_id=run_id)
        store.update_worker_state(worker['worker_id'], 'paused')
        assert store.list_terminal_runs_missing_callback_intent(worker_id=worker['worker_id']) == []
    # Reproduce the former durable split: terminal run and worker-only effect,
    # with no per-run callback sink. No new termination is issued to recover it.
    store.update_run(run_id, state='cancelled', ended_at=datetime.now(timezone.utc).isoformat(), error_text='Synthetic old owner stop')
    store.update_worker_state(worker['worker_id'], 'terminating')
    store.complete_worker_termination(worker['worker_id'])
    if canonical_origin_only:
        # A global restart scan must not revive retained historical origins;
        # the existing exact worker termination effect owns this repair.
        assert run_id not in {
            row['run_id'] for row in store.list_terminal_runs_missing_callback_intent()
        }
        assert [row['run_id'] for row in store.list_terminal_runs_missing_callback_intent(
            worker_id=worker['worker_id']
        )] == [run_id]
    released_at = store.get_worker(worker['worker_id'])['compute_released_at']
    if legacy_effect_applied:
        # Exact retained prior defect: worker-only effect was retired before
        # canonical origin-only callback recovery was supported.
        with store._connect() as connection:
            connection.execute("UPDATE lifecycle_operation_effects SET status='applied' WHERE worker_id=?", (worker['worker_id'],))
        for _ in range(2): service.terminate_worker(worker['worker_id'])
    else:
        for _ in range(2): service._replay_pending_lifecycle_effects()
    assert store.get_worker(worker['worker_id'])['compute_released_at'] == released_at
    with store._connect() as connection:
        rows = connection.execute("SELECT * FROM callback_outbox WHERE run_id=? AND event_type='run.cancelled'", (run_id,)).fetchall()
        effects = connection.execute("SELECT status FROM lifecycle_operation_effects WHERE worker_id=? AND effect_kind='callback.worker_terminated'", (worker['worker_id'],)).fetchall()
    assert len(rows) == 1 and dict(rows[0])['result_digest']
    assert dict(rows[0])['result_revision'] == store.get_run(run_id)['terminal_result_revision']
    assert all(effect['status'] == 'applied' for effect in effects)
    assert store.list_terminal_runs_missing_callback_intent(worker_id=worker['worker_id']) == []


@pytest.mark.parametrize('mode', ['active', 'retry', 'dead', 'wrong_run', 'wrong_attempt', 'reused_pid', 'wrong_worker'])
def test_exact_host_worker_termination_preserves_target_after_legacy_split(account_client, monkeypatch, tmp_path, mode):
    import os, subprocess
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
    store, service, work, worker = _owned_cancellation_fixture(account_client, key='exact-host-close-' + mode)
    worker_id, run_id = worker['worker_id'], work['current_run_id']
    store.update_worker(worker_id, execution_mode='host', workspace_root=str(tmp_path / 'workspace'),
                        workspace_dir=str(tmp_path / 'workspace'), last_run_id=run_id)
    native = HostCodexCliRuntime(str(tmp_path / 'native'))
    process = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
    unrelated = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
    try:
        claimed = store.claim_next_queued_run(worker_id, executor_id=service.executor_id)
        assert claimed and claimed['run_id'] == run_id
        lease = store.acquire_host_run_lease(runtime_family='codex', lane='mission', tenant_id='tenant-a',
            owner_id='owner-a', worker_id=worker_id, run_id=run_id, executor_id=service.executor_id,
            conversation_limit=2, mission_limit=64, account_mission_limit=64, tenant_mission_limit=64, lease_ttl_s=300)
        assert store.admit_claimed_run(run_id, lease_id=lease['lease_id'], executor_id=service.executor_id)
        invoked = store.mark_run_runtime_invoked(run_id, lease_id=lease['lease_id'], executor_id=service.executor_id)
        identity = native._process_start_identity(process.pid)
        session_name = native._session_name_for_run_id(run_id)
        assert store.confirm_host_run_start(worker_id=worker_id, run_id=run_id,
            run_started_at=invoked['runtime_invoked_at'], lease_id=lease['lease_id'], startup_token=lease['startup_token'],
            executor_id=service.executor_id, identity_kind='host_process', pid=process.pid,
            process_group=os.getpgid(process.pid), process_start_identity=identity, container_id='', session_id=session_name)
        run = store.get_run(run_id)
        root = native._run_root(worker_id, run_id); root.mkdir(parents=True, exist_ok=True)
        (root / 'stdout.log').write_text(''); (root / 'stderr.log').write_text('')
        session = {'session_name': session_name, 'run_id': run_id, 'attempt_id': run['active_attempt_id'],
            'stdout_path': str(root / 'stdout.log'), 'stderr_path': str(root / 'stderr.log'), 'exit_path': str(root / 'exit_code'),
            'model': 'synthetic-model', 'process_pid': process.pid, 'process_group': os.getpgid(process.pid),
            'process_start_identity': identity, 'started_at': datetime.now(timezone.utc).isoformat()}
        if mode != 'active':
            # Exact former product split: cancelled result + ended lease while
            # its original native process/session still waits for permission.
            store.update_run(run_id, state='cancelled', ended_at=datetime.now(timezone.utc).isoformat(), error_text='Synthetic old stop')
            store.release_host_run_lease(lease['lease_id'], executor_id=service.executor_id, reason='synthetic legacy split')
            store.update_worker_state(worker_id, 'termination_failed')
        else: store.update_worker_state(worker_id, 'running')
        if mode == 'dead': process.terminate(); process.wait(timeout=3)
        elif mode == 'wrong_run': session['run_id'] = 'run-unrelated'
        elif mode == 'wrong_attempt': session['attempt_id'] = 'attempt-unrelated'
        elif mode == 'reused_pid': session['process_start_identity'] = 'ps-lstart:synthetic-old-incarnation'
        elif mode == 'wrong_worker':
            other = store.create_worker(worker['project_id'], 'owner-a', 'Other', 'assistant', 'codex-cli', '', 'stub', 'synthetic', tenant_id='tenant-a')
            foreign = store.create_run(other['worker_id'], worker['project_id'], 'Other owned worker', state='completed')
            store.update_worker(worker_id, last_run_id=foreign['run_id'])
        native._write_active_session(worker_id, session)
        monkeypatch.setattr(service.runtime, 'terminate_worker', native.terminate_worker)
        if mode in {'wrong_run', 'wrong_attempt', 'wrong_worker', 'reused_pid'}:
            with pytest.raises(Exception, match='identity|ownership'):
                service.terminate_worker(worker_id)
            assert process.poll() is None
        else:
            result = service.terminate_worker(worker_id)
            assert result['state'] == 'terminated' and result['compute_released_at']
            process.wait(timeout=3); assert process.poll() is not None
            assert not native._active_session_meta_path(worker_id).exists()
        assert unrelated.poll() is None
    finally:
        for child in [process, unrelated]:
            if child.poll() is None: child.terminate()
            child.wait(timeout=3)


def test_capacity_pressure_is_retryable_and_does_not_claim_or_rewrite_retained_result(account_client):
    store, service, body, original = retained_result(account_client)
    with service._schedule_transport_lock:
        service._schedule_transport_active.update({
            ("tenant-a", "owner-a", "synthetic-recipient", "active-1"),
            ("tenant-a", "owner-a", "synthetic-recipient", "active-2"),
        })
    try:
        response = account_client.post("/v1/callback-associations/recover", headers=account_headers(), json=body)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert response.json()["detail"]["code"] == "callback_transport_busy"
        assert row(store, body)["status"] == "dead_lettered"
        assert row(store, body)["payload_json"] == original["payload_json"]
        assert row(store, body)["attempts"] == 27
    finally:
        with service._schedule_transport_lock:
            service._schedule_transport_active.clear()
