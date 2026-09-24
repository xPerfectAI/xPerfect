from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from test_account_api import (
    account_client, account_headers, delegation_payload_with_origin,
    _truthfully_invoke_run,
)


def retained_result(client):
    service = client.app.state.service
    service.executor.submit = lambda *args, **kwargs: None
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
    return store, service, body, callback


def row(store, body):
    with store._connect() as conn:
        return dict(conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?",
                                (body["callbackId"],)).fetchone())


def test_recovery_preserves_history_and_one_owned_delivery_lease(account_client):
    store, service, body, original = retained_result(account_client)
    tasks = []
    service.executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
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
    service.executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
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
    service.executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
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


def test_recovery_failure_cannot_overwrite_a_new_delivery_lease(account_client, monkeypatch):
    store, service, body, _ = retained_result(account_client)
    tasks = []
    service.executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
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
    return store, service, body, original


def test_retained_stop_recovers_original_callback_once_through_original_signer(account_client, monkeypatch):
    store, service, body, original = retained_stop(account_client)
    tasks, received = [], []
    service.executor.submit = lambda *args, **kwargs: tasks.append((args, kwargs))
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
