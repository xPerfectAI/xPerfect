"""Independent local instances can share one browser without replacing its logins."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from glass_drive_ui.auth_gateway import HumanAuthGateway
from glass_drive_ui import server
from test_server import FakeRuntimeClient

PASSWORD = "synthetic-local-Strong-password-1234567890"


def make_local(monkeypatch, root, namespace, port):
    for key in ("GLASSHIVE_ENTERPRISE_MODE", "WPR_ENTERPRISE_MODE", "GLASSHIVE_TRUST_INBOUND_IDENTITY",
                "GLASSHIVE_LOCAL_HUMAN_ASSERTION", "VIVENTIUM_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "GLASSHIVE_HUMAN_AUTH_MODE": "local_password", "GLASSHIVE_SECURITY_MODE": "local",
        "GLASSHIVE_DEFAULT_OWNER_ID": "existing-owner", "GLASSHIVE_LOCAL_AUTH_NAMESPACE": namespace,
        "GLASSHIVE_AUTH_STATE_PATH": str(root / "auth.sqlite3"),
        "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY": "synthetic-private-throttle-key-1234567890",
        "WPR_API_TOKEN": "synthetic-runtime-secret",
    }.items():
        monkeypatch.setenv(key, value)
    gateway = HumanAuthGateway.from_env()
    gateway.provision_local_owner(password=PASSWORD)
    return TestClient(server.create_app(runtime_client=FakeRuntimeClient()), base_url=f"http://127.0.0.1:{port}")


def cookie_name(base, namespace):
    return base + "_" + sha256(namespace.encode()).hexdigest()[:16]


def unlock(client, namespace):
    page = client.get("/login")
    assert page.status_code == 200
    token = client.cookies[cookie_name("glasshive_login_csrf", namespace)]
    response = client.post("/auth/email/login", json={"password": PASSWORD},
        headers={"Origin": str(client.base_url).rstrip("/"), "X-GlassHive-CSRF": token})
    assert response.status_code == 200, response.text
    return response


def test_two_local_ports_keep_sessions_csrf_and_logout_independent(tmp_path, monkeypatch):
    first = make_local(monkeypatch, tmp_path / "first", "synthetic-first", 18780)
    second = make_local(monkeypatch, tmp_path / "second", "synthetic-second", 28780)
    unlock(first, "synthetic-first")
    second.cookies.update(first.cookies)
    assert second.get("/auth/session").json()["authenticated"] is False
    unlock(second, "synthetic-second")
    first.cookies.update(second.cookies)  # One browser sends same-host cookies across ports.
    for client in (first, second):
        assert client.get("/auth/session").json()["authenticated"] is True
        assert client.get("/", follow_redirects=False).status_code == 200
    assert len([name for name in first.cookies if name.startswith("glasshive_session_")]) == 2

    wrong = second.cookies[cookie_name("glasshive_csrf", "synthetic-first")]
    assert second.post("/auth/logout", headers={"X-GlassHive-CSRF": wrong}).status_code == 403
    right = second.cookies[cookie_name("glasshive_csrf", "synthetic-second")]
    logged_out = second.post("/auth/logout", headers={"X-GlassHive-CSRF": right})
    assert logged_out.status_code == 200
    first.cookies.update(second.cookies)
    assert first.get("/auth/session").json()["authenticated"] is True
    assert second.get("/auth/session").json()["authenticated"] is False


def test_local_cookie_names_persist_across_restart_without_legacy_fallback(tmp_path, monkeypatch):
    client = make_local(monkeypatch, tmp_path, "synthetic-stable", 18780)
    response = unlock(client, "synthetic-stable")
    cookies = response.headers.get_list("set-cookie")
    assert any("HttpOnly" in value and "SameSite=lax" in value for value in cookies)
    assert any("SameSite=strict" in value and "HttpOnly" not in value for value in cookies)
    assert all("Path=/" in value for value in cookies)
    restarted = TestClient(server.create_app(runtime_client=FakeRuntimeClient()), base_url=client.base_url)
    restarted.cookies.update(client.cookies)
    assert restarted.get("/auth/session").json()["authenticated"] is True
    bare = TestClient(server.create_app(runtime_client=FakeRuntimeClient()), base_url=client.base_url)
    # Even a valid token from this same DB under the old ambiguous name is not adopted.
    bare.cookies.set("glasshive_session", client.cookies[cookie_name("glasshive_session", "synthetic-stable")])
    bare.cookies.set("glasshive_csrf", client.cookies[cookie_name("glasshive_csrf", "synthetic-stable")])
    assert bare.get("/auth/session").json()["authenticated"] is False


def test_every_local_html_surface_carries_the_same_nonsecret_cookie_scope(tmp_path, monkeypatch):
    namespace = "synthetic-pages"
    client = make_local(monkeypatch, tmp_path, namespace, 18780)
    unlock(client, namespace)
    suffix = "_" + sha256(namespace.encode()).hexdigest()[:16]
    for path in ("/login", "/", "/conversation", "/confirm-change", "/watch/wrk_1", "/desktop/wrk_1",
                 "/ui/workers/wrk_1/terminal"):
        page = client.get(path)
        assert page.status_code == 200, (path, page.text)
        assert f'name="glasshive-auth-cookie-suffix" content="{suffix}"' in page.text
        assert namespace not in page.text
        assert client.cookies[cookie_name("glasshive_session", namespace)] not in page.text


def test_shared_client_reader_selects_only_its_instance_and_rejects_malformed_values():
    module = (Path(server.STATIC_DIR) / "auth-cookies.js").as_uri()
    code = f"""
      import assert from 'node:assert/strict';
      import {{authCookieName, readAuthCookie}} from {json.dumps(module)};
      const meta={{content:'_first'}};
      globalThis.document={{querySelector:()=>meta,cookie:'glasshive_csrf=legacy; glasshive_csrf_second=other; glasshive_csrf_first=own%20token'}};
      assert.equal(authCookieName('glasshive_csrf'),'glasshive_csrf_first');
      assert.equal(readAuthCookie('glasshive_csrf'),'own token');
      meta.content='_second';assert.equal(readAuthCookie('glasshive_csrf'),'other');
      meta.content='_absent';assert.equal(readAuthCookie('glasshive_csrf'),'');
      meta.content='_first';document.cookie='glasshive_csrf_first=%broken';assert.equal(readAuthCookie('glasshive_csrf'),'');
      meta.content='';document.cookie='glasshive_csrf=shared';assert.equal(readAuthCookie('glasshive_csrf'),'shared');
    """
    result = subprocess.run(["node", "--input-type=module", "--eval", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_worker_service_connection_refusal_has_a_bounded_health_response(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "disabled")
    runtime = FakeRuntimeClient()
    def unavailable():
        raise httpx.ConnectError("synthetic private connection detail")
    runtime.health = unavailable
    app = server.create_app(runtime_client=runtime)
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert "starting or unavailable" in response.json()["detail"]
    assert "private connection" not in response.text
    runtime.health = lambda: {"status": "ok"}
    assert TestClient(app).get("/health").status_code == 200
    def unexpected():
        raise ValueError("unexpected product defect")
    runtime.health = unexpected
    assert TestClient(app, raise_server_exceptions=False).get("/health").status_code == 500
