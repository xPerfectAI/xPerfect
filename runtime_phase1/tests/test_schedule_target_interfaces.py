"""Neutral schedule API/MCP schema and authenticated courier checks."""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from fastmcp import Client
from workers_projects_runtime.api import create_app
from workers_projects_runtime.mcp_server import create_mcp_server


def definition(**fields):
    return {"definition_id": "rsd_synthetic", "tenant_id": fields.get("tenant_id", "local"),
            "owner_id": fields.get("owner_id", "synthetic"), "scheduler_owner": "native",
            "instruction": fields.get("instruction", ""), "recurrence_type": "interval",
            "interval_seconds": 3600, "timezone_name": "UTC", "dst_policy": "next_valid_earliest",
            "next_run_at": "2035-01-01T00:00:00+00:00", "active": True,
            "created_at": "2034-01-01T00:00:00+00:00", "updated_at": "2034-01-01T00:00:00+00:00",
            "target": fields.get("target"), "version": 2}


def test_api_no_dummy_worker_and_exact_current_principal(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub")
    calls = []
    monkeypatch.setattr(app.state.service, "create_target_schedule", lambda **fields: (calls.append(fields), definition(**fields))[1], raising=False)
    client = TestClient(app)
    body = {"target": {"kind": "webhook", "destination_id": "whd_synthetic", "request_data": {"request_ref": "opaque"}},
            "idempotency_key": "synthetic-create-1", "recurrence_type": "interval", "interval_seconds": 3600}
    response = client.post("/v1/recurring-schedules", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["worker_id"] is None and response.json()["project_id"] is None
    assert response.json()["target"] == body["target"] and response.json()["version"] == 2
    assert calls[0]["target"] == body["target"]
    assert not app.state.store.list_projects()
    assert "tenant_id" in calls[0] and "owner_id" in calls[0]
    forbidden = client.post("/v1/recurring-schedules", json={**body, "owner_id": "forged"})
    assert forbidden.status_code == 422
    assert len(calls) == 1


def test_destination_credentials_fail_without_echo_and_never_in_read(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub")
    client = TestClient(app)
    service = app.state.service
    monkeypatch.setattr(service, "create_webhook_destination", lambda **kw: (_ for _ in ()).throw(ValueError(kw["url"])), raising=False)
    monkeypatch.setattr(service, "list_webhook_destinations", lambda **kw: [{"destination_id": "whd_synthetic", "name": "Receiver", "version": 1, "enabled": True}], raising=False)
    bad = client.post("/v1/webhook-destinations", json={"name": "Receiver", "url": "https://secret.invalid/path?key=synthetic-private", "secret": "synthetic-secret"})
    assert bad.status_code == 400
    assert "synthetic-private" not in bad.text and "synthetic-secret" not in bad.text
    malformed = client.post("/v1/webhook-destinations", json={"secret": "synthetic-secret"})
    assert malformed.status_code == 422 and "synthetic-secret" not in malformed.text
    listed = client.get("/v1/webhook-destinations")
    assert listed.status_code == 200
    assert not {"secret", "url", "headers"}.intersection(listed.json()["items"][0])
    changed_url = client.patch("/v1/webhook-destinations/whd_synthetic", json={"url": "https://other.invalid/"})
    assert changed_url.status_code == 422  # fixed destination identity, no URL edits


def test_openapi_covers_discriminated_targets_and_owner_setup(tmp_path):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub")
    spec = app.openapi()
    assert "post" in spec["paths"]["/v1/recurring-schedules"]
    assert "post" in spec["paths"]["/v1/webhook-destinations"]
    schema = spec["components"]["schemas"]["CreateTargetScheduleRequest"]
    assert schema["properties"]["target"]["discriminator"]["propertyName"] == "kind"
    assert "idempotency_key" in schema["required"]
    assert "instruction" not in schema["required"]


def test_mcp_same_neutral_request_and_safe_destination_configuration():
    class Courier:
        def create_target_schedule(self, payload):
            return {"definition_id": "rsd_synthetic", "target": payload["target"]}

        def webhook_destinations(self):
            return [{"destination_id": "whd_synthetic", "name": "Receiver"}]

        def create_webhook_destination(self, payload):
            return {"destination_id": "whd_synthetic", "name": payload["name"]}

        def update_webhook_destination(self, destination_id, payload):
            return {"destination_id": destination_id, **payload}

    async def check():
        async with Client(create_mcp_server(api_client=Courier())) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"schedule_create", "webhook_destinations", "webhook_destination_create", "webhook_destination_update"} <= names
            target = {"kind": "webhook", "destination_id": "whd_synthetic", "request_data": {"request_ref": "opaque"}}
            result = await client.call_tool("schedule_create", {"request": {"target": target, "idempotency_key": "synthetic-create-1", "recurrence_type": "interval", "interval_seconds": 3600}})
            assert result.data["target"] == target
            destinations = await client.call_tool("webhook_destinations", {})
            assert destinations.structured_content["result"][0]["destination_id"] == "whd_synthetic"
    asyncio.run(check())


def test_neutral_launch_authority_uses_the_existing_clean_room_normalizer(monkeypatch):
    from workers_projects_runtime import mcp_server
    calls = []
    monkeypatch.setattr(mcp_server, "canonical_parallel_clean_room_bootstrap", lambda bundle: (calls.append(bundle), bundle)[1])
    authority = {"version": 1, "kind": "native"}
    value = mcp_server._normalize_bootstrap_bundle({"launch_authority": authority})
    assert value["launch_authority"] == authority and calls


def test_workerless_accepted_history_has_safe_status_timings_and_no_null_error(tmp_path, monkeypatch):
    """A real workerless row must survive the API's joined history schema."""
    from datetime import datetime, timedelta, timezone
    from workers_projects_runtime import schedule_targets

    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub")
    service = app.state.service
    try:
        client = TestClient(app)
        destination = client.post("/v1/webhook-destinations", json={
            "name": "Synthetic receiver", "url": "https://receiver.example.invalid/events"})
        assert destination.status_code == 201, destination.text
        created = client.post("/v1/recurring-schedules", json={
            "target": {"kind": "webhook", "destination_id": destination.json()["destination_id"],
                       "request_data": {"request_ref": "opaque"}},
            "idempotency_key": "synthetic-history-create", "recurrence_type": "once",
            "starts_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        assert created.status_code == 201, created.text
        schedule_id = created.json()["definition_id"]
        run_now = client.post(f"/v1/recurring-schedules/{schedule_id}/run-now",
                              json={"idempotency_key": "synthetic-history-now"})
        assert run_now.status_code == 200, run_now.text
        with service.store._connect() as db:
            callback_id = db.execute("SELECT callback_id FROM callback_outbox").fetchone()[0]
        claimed = service.store.claim_pending_callback(callback_id)
        assert claimed
        assert schedule_targets.finish_callback(service.store, claimed, state="accepted", http_status=202,
                                                 timings={"total_ms": 4.2, "sign_ms": 0.1})
        history = client.get(f"/v1/recurring-schedules/{schedule_id}/occurrences")
        assert history.status_code == 200, history.text
        occurrence = history.json()["items"][0]
        assert occurrence["scheduled_run_id"] is None
        assert occurrence["state"] == "accepted" and occurrence["last_error"] == ""
        assert occurrence["http_status"] == 202
        assert occurrence["delivery_expires_at"]
        public = client.get(f"/v1/recurring-schedules/{schedule_id}")
        assert public.status_code == 200, public.text
        assert public.json()["last_outcome"] == "accepted"
        assert public.json()["last_occurrence_id"] == occurrence["occurrence_id"]
        assert public.json()["last_occurrence_expires_at"] == occurrence["delivery_expires_at"]
        assert occurrence["timings_ms"]["total_ms"] == 4.2
        assert not service.store.list_projects()
    finally:
        service.shutdown()
        service.store.close()


def test_destination_conflict_returns_safe_recovery_identity_and_body_limit(tmp_path):
    import base64
    app = create_app(str(tmp_path / "destination-conflict.db"), runtime_backend="stub")
    client = TestClient(app)
    secret = "whsec_" + base64.b64encode(b"synthetic-signing-key-at-least32").decode()
    body = {"idempotency_key": "destination-api-conflict", "name": "Receiver", "url": "https://receiver.example.test/hook",
        "secret": secret, "max_body_bytes": 65536}
    first = client.post("/v1/webhook-destinations", json=body)
    assert first.status_code == 201, first.text
    assert first.json()["max_body_bytes"] == 65536
    assert first.json()["url_sha256"] == __import__("hashlib").sha256(body["url"].encode()).hexdigest()
    assert "url" not in first.json() and "secret" not in first.json()
    assert client.post("/v1/webhook-destinations", json=body).status_code == 201
    conflict = client.post("/v1/webhook-destinations", json={**body, "url": "https://different.example.test/private"})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == {"code": "webhook_destination_create_conflict", "destination_id": first.json()["destination_id"], "changed_fields": ["url"]}
    assert secret not in conflict.text and "different.example" not in conflict.text
    schema = app.openapi()["components"]["schemas"]
    assert schema["CreateWebhookDestinationRequest"]["properties"]["max_body_bytes"]["default"] == 262144
    assert "expected_version" in schema["UpdateRecurringScheduleRequest"]["properties"]
    assert client.patch("/v1/webhook-destinations/" + first.json()["destination_id"], json={"max_body_bytes": 512}).status_code == 422
