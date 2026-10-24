from fastapi.testclient import TestClient
import httpx
from glass_drive_ui.server import create_app


class Runtime:
    def __init__(self):
        self.calls = []

    def create_target_schedule(self, payload):
        self.calls.append(("create", payload))
        return {"definition_id": "rsd_synthetic", **payload}

    def webhook_destinations(self):
        return [{"destination_id": "whd_synthetic", "name": "Receiver", "enabled": True, "version": 1}]

    def create_webhook_destination(self, payload):
        self.calls.append(("destination", payload))
        return {"destination_id": "whd_synthetic", "name": payload["name"], "secret_once": "synthetic-once"}

    def update_webhook_destination(self, destination_id, payload):
        self.calls.append(("update", destination_id, payload))
        return {"destination_id": destination_id, **payload}


def test_bff_neutral_target_and_destination_do_not_require_worker():
    runtime = Runtime()
    client = TestClient(create_app(runtime_client=runtime))
    target = {"kind": "webhook", "destination_id": "whd_synthetic", "request_data": {"request_ref": "opaque"}}
    created = client.post("/api/recurring-schedules", json={"target": target, "idempotency_key": "synthetic-create-1", "recurrence_type": "interval", "interval_seconds": 3600})
    assert created.status_code == 201, created.text
    assert runtime.calls[0][1]["target"] == target
    assert "worker_id" not in runtime.calls[0][1]
    configured = client.post("/api/webhook-destinations", json={"name": "Receiver", "url": "https://example.invalid/private", "headers": {"Authorization": "Bearer synthetic"}})
    assert configured.status_code == 201, configured.text
    assert configured.headers["cache-control"] == "no-store"
    assert configured.json()["secret_once"] == "synthetic-once"
    listed = client.get("/api/webhook-destinations")
    assert listed.status_code == 200 and "url" not in listed.json()["items"][0]
    updated = client.patch("/api/webhook-destinations/whd_synthetic", json={"enabled": False})
    assert updated.status_code == 200 and updated.json()["enabled"] is False


def test_bff_private_validation_does_not_echo_credentials_or_accept_url_edits():
    runtime = Runtime()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post("/api/webhook-destinations", json={"secret": "synthetic-private"})
    assert response.status_code == 422 and "synthetic-private" not in response.text
    response = client.patch("/api/webhook-destinations/whd_synthetic", json={"url": "https://private.invalid/"})
    assert response.status_code == 422 and "private.invalid" not in response.text
    assert not runtime.calls


def test_bff_unavailable_destination_preserves_safe_error():
    class Failing(Runtime):
        def create_webhook_destination(self, payload):
            raise httpx.ConnectError("https://secret.invalid/private?token=synthetic")
    client = TestClient(create_app(runtime_client=Failing()))
    response = client.post("/api/webhook-destinations", json={"name": "Receiver", "url": "https://example.invalid/"})
    assert response.status_code == 502
    assert "synthetic" not in response.text and "secret.invalid" not in response.text


def test_bff_single_definition_status_uses_current_runtime_courier_and_no_cache():
    class Current(Runtime):
        def recurring_schedule(self, definition_id):
            self.calls.append(("get", definition_id))
            return {"definition_id": definition_id, "last_outcome": "pending",
                    "last_occurrence_id": "occ_synthetic", "last_occurrence_expires_at": "2035-01-01T00:01:00Z"}
    runtime = Current()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.get("/api/recurring-schedules/rsd_synthetic")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["last_outcome"] == "pending"
    assert response.json()["last_occurrence_expires_at"] == "2035-01-01T00:01:00Z"
    assert runtime.calls == [("get", "rsd_synthetic")]

    from glass_drive_ui.runtime_client import RuntimeClient
    courier = RuntimeClient()
    calls = []
    courier._request = lambda method, path: calls.append((method, path)) or {"state": "pending"}
    assert courier.recurring_schedule("rsd_synthetic")["state"] == "pending"
    assert calls == [("GET", "/v1/recurring-schedules/rsd_synthetic")]


def test_bff_single_definition_failure_preserves_safe_status_without_private_detail():
    class Missing(Runtime):
        def recurring_schedule(self, definition_id):
            response = httpx.Response(404, request=httpx.Request("GET", "https://private.invalid/"))
            raise httpx.HTTPStatusError("synthetic-private-secret", request=response.request, response=response)
    client = TestClient(create_app(runtime_client=Missing()))
    response = client.get("/api/recurring-schedules/rsd_synthetic")
    assert response.status_code == 404
    assert "private" not in response.text and "secret" not in response.text
