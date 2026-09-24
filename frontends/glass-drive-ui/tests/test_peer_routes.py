import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from glass_drive_ui.peer_routes import install_peer_ui_routes


def test_peer_facade_uses_owner_confirmation_and_keeps_auth_server_side():
    app = FastAPI()
    calls = []

    class Client:
        def __init__(self, human):
            self.human = human

        def _request(self, method, path, json_body=None):
            calls.append((self.human, method, path, json_body))
            return {"items": []}

    install_peer_ui_routes(
        app, lambda request, human_confirmation=False: Client(human_confirmation)
    )
    with TestClient(app) as client:
        assert (
            client.put(
                "/api/workspaces/wsp_synthetic/peer-policy",
                json={"expected_revision": 1},
            ).status_code
            == 200
        )
        assert (
            client.post("/api/peer-grants", json={"source_worker_id": "a"}).status_code
            == 200
        )
        assert client.delete("/api/peer-grants/pg_synthetic").status_code == 200
        assert client.get("/api/workers/a/peers").status_code == 200
        assert (
            client.post(
                "/api/workers/a/peer-messages", json={"message": "Synthetic"}
            ).status_code
            == 200
        )
        assert client.get("/api/peer-workspaces").status_code == 200
    assert [row[0] for row in calls] == [True, True, True, False, False, False]
    assert calls[4][2] == "/v1/workers/a/peer-messages"


def test_peer_facade_never_returns_provider_exception_text():
    app = FastAPI()

    class Client:
        def _request(self, *args, **kwargs):
            response = httpx.Response(
                403,
                json={
                    "detail": {
                        "code": "peer_access_denied",
                        "debug": "private-provider-value",
                    }
                },
                request=httpx.Request("GET", "https://runtime.example.invalid"),
            )
            raise httpx.HTTPStatusError(
                "private-provider-value", request=response.request, response=response
            )

    install_peer_ui_routes(app, lambda request, **kwargs: Client())
    with TestClient(app) as client:
        response = client.get("/api/workers/a/peers")
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "peer_access_denied"}}
    assert "private-provider-value" not in response.text


def test_permission_batch_uses_human_owner_authority_and_preserves_explicit_null():
    app = FastAPI()
    calls = []

    class Client:
        def __init__(self, human):
            self.human = human

        def _request(self, method, path, json_body=None):
            calls.append((self.human, method, path, json_body))
            return {"items": []}

    install_peer_ui_routes(
        app, lambda request, human_confirmation=False: Client(human_confirmation)
    )
    with TestClient(app) as client:
        assert (
            client.get("/api/workers/synthetic/peer-access-options").status_code == 200
        )
        assert (
            client.post(
                "/api/peer-grants/batch", json={"expires_at": None, "direction": "both"}
            ).status_code
            == 200
        )
    assert calls == [
        (False, "GET", "/v1/workers/synthetic/peer-access-options", None),
        (
            True,
            "POST",
            "/v1/peer-grants/batch",
            {"expires_at": None, "direction": "both"},
        ),
    ]
