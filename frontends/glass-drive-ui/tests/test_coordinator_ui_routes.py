"""Exercise owner authentication and the narrow conversation proxy boundary."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from glass_drive_ui import server as server_module
from glass_drive_ui.server import create_app
from test_server import (
    FakeRuntimeClient, StreamingFakeClient, _FakeOidcHumanAuth,
    clear_glasshive_ui_env, signed_worker_token,  # noqa: F401
)


@pytest.fixture
def upstream(monkeypatch):
    calls = []

    class Client(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, headers=None, content=None):
            calls.append((method, url, headers or {}, content))
            return httpx.Response(200, json={"saved": True})

    monkeypatch.setattr(server_module.httpx, "AsyncClient", Client)
    return calls


def test_conversation_proxy_keeps_exact_message_and_owner_service_authority(monkeypatch, upstream):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-runtime-service")
    with TestClient(create_app(runtime_client=FakeRuntimeClient())) as client:
        response = client.post(
            "/v1/coordinator/conversations",
            json={"message": "Keep all ten goals.\nUse the supplied files."},
            headers={"X-WPR-Token": "untrusted-caller-token"},
        )
    assert response.json() == {"saved": True}
    method, url, headers, body = upstream[0]
    assert method == "POST" and url == "http://runtime.test/v1/coordinator/conversations"
    assert json.loads(body) == {"message": "Keep all ten goals.\nUse the supplied files."}
    assert headers["X-WPR-Token"] == "synthetic-runtime-service"
    assert "untrusted-caller-token" not in headers.values()


def test_conversation_write_rejects_missing_csrf_and_cross_origin(monkeypatch, upstream):
    auth = _FakeOidcHumanAuth()
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: auth)
    with TestClient(create_app(runtime_client=FakeRuntimeClient())) as client:
        client.cookies.set("glasshive_session", "opaque-session")
        client.cookies.set("glasshive_csrf", "synthetic-csrf")
        route = "/v1/coordinator/conversations"
        assert client.post(route, json={}).status_code == 403
        assert client.post(route, json={}, headers={"Origin": "https://outside.invalid", "X-GlassHive-CSRF": "synthetic-csrf"}).status_code == 403
        assert upstream == []
        accepted = client.post(route, json={}, headers={"Origin": "http://testserver", "X-GlassHive-CSRF": "synthetic-csrf"})
        assert accepted.status_code == 200
    assert len(upstream) == 1


def test_conversation_proxy_rejects_signed_worker_and_unlisted_routes(monkeypatch, upstream):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-link-secret")
    with TestClient(create_app(runtime_client=FakeRuntimeClient())) as client:
        denied = client.get("/v1/coordinator/conversations", params={"gh_token": signed_worker_token("synthetic-link-secret")})
        assert denied.status_code == 403
        assert client.post("/v1/coordinator/other", json={}).status_code == 404
        assert client.delete("/v1/coordinator/conversations").status_code == 404
    assert upstream == []


def test_conversation_page_uses_existing_sign_in_return_path(monkeypatch, upstream):
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: _FakeOidcHumanAuth())
    with TestClient(create_app(runtime_client=FakeRuntimeClient())) as client:
        response = client.get("/conversation?id=coordinator-" + "a" * 32, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?return_to=%2Fconversation")
        client.cookies.set("glasshive_session", "opaque-session")
        assert client.get("/conversation").status_code == 200
    assert upstream == []


def test_conversation_project_choices_use_owner_catalog(monkeypatch):
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: _FakeOidcHumanAuth())
    class RuntimeClient(FakeRuntimeClient):
        def list_projects(self):
            return [
                {"project_id": "prj_1", "title": "Alpha", "origin_surface": ""},
                {"project_id": "prj_2", "title": "Internal run", "origin_surface": "coordinator"},
            ]

    with TestClient(create_app(runtime_client=RuntimeClient())) as client:
        assert client.get("/api/conversation-projects").status_code == 401
        client.cookies.set("glasshive_session", "opaque-session")
        response = client.get("/api/conversation-projects")
        assert response.status_code == 200
        assert response.json() == {"items": [{"project_id": "prj_1", "title": "Alpha"}]}
