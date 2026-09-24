import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glass_drive_ui.member_routes import install_member_ui_routes


def test_member_facade_preserves_default_and_owner_write_boundary():
    calls = []
    app = FastAPI()

    class Client:
        def __init__(self, human):
            self.human = human

        def _request(self, method, path, json_body=None):
            calls.append((self.human, method, path, json_body))
            return {"worker_id": "worker-synthetic"}

    install_member_ui_routes(
        app, lambda request, human_confirmation=False: Client(human_confirmation)
    )
    with TestClient(app) as client:
        assert (
            client.get(
                "/api/execution-workspaces/workspace-synthetic/members"
            ).status_code
            == 200
        )
        assert client.get("/api/member-profiles").status_code == 200
        assert (
            client.post(
                "/api/execution-workspaces/workspace-synthetic/members", json={}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/execution-workspaces/workspace-synthetic/members",
                json={"owner_id": "other"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/execution-workspaces/workspace-synthetic/members",
                json={"model": "invented"},
            ).status_code
            == 422
        )
    assert calls == [
        (False, "GET", "/v1/workspaces/workspace-synthetic/members", None),
        (False, "GET", "/v1/worker-profiles", None),
        (True, "POST", "/v1/workspaces/workspace-synthetic/members", {"profile": ""}),
    ]


def test_member_facade_bounded_runtime_failure():
    app = FastAPI()

    class Client:
        def _request(self, *args, **kwargs):
            response = httpx.Response(
                503,
                json={"private": "provider secret"},
                request=httpx.Request("POST", "https://runtime.invalid"),
            )
            raise httpx.HTTPStatusError(
                "provider secret", request=response.request, response=response
            )

    install_member_ui_routes(app, lambda *args, **kwargs: Client())
    with TestClient(app) as client:
        response = client.post("/api/execution-workspaces/synthetic/members", json={})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "member_runtime_unavailable"}}


def test_member_facade_forwards_admission_controls_and_typed_reason():
    calls = []
    app = FastAPI()

    class Client:
        def _request(self, method, path, json_body=None):
            calls.append((method, path, json_body))
            return {"worker_id": "worker-synthetic"}

    install_member_ui_routes(app, lambda request, human_confirmation=False: Client())
    with TestClient(app) as client:
        response = client.post(
            "/api/execution-workspaces/workspace-synthetic/members",
            json={
                "name": "Claude member",
                "profile": "claude-code",
                "effort": "high",
                "provider_account_policy": "personal_required",
                "provider_account_id": "acct_claude",
            },
        )
    assert response.status_code == 201
    assert calls == [(
        "POST",
        "/v1/workspaces/workspace-synthetic/members",
        {
            "name": "Claude member",
            "profile": "claude-code",
            "effort": "high",
            "provider_account_policy": "personal_required",
            "provider_account_id": "acct_claude",
        },
    )]


def test_member_facade_preserves_safe_typed_admission_error():
    app = FastAPI()

    class Client:
        def _request(self, *args, **kwargs):
            response = httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "shared_provider_account_not_ready",
                        "message": "Reconnect the selected account before adding the member.",
                    }
                },
                request=httpx.Request("POST", "https://runtime.invalid"),
            )
            raise httpx.HTTPStatusError(
                "account unavailable", request=response.request, response=response
            )

    install_member_ui_routes(app, lambda *args, **kwargs: Client())
    with TestClient(app) as client:
        response = client.post(
            "/api/execution-workspaces/synthetic/members",
            json={"profile": "codex-cli"},
        )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "shared_provider_account_not_ready",
            "message": "Reconnect the selected account before adding the member.",
        }
    }
