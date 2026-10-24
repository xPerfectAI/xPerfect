"""Independent catalogs cannot hide ready accounts or weaken authentication."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from glass_drive_ui.server import create_app
from test_server import FakeRuntimeClient, clear_glasshive_ui_env  # noqa: F401


def rejection(status):
    response = httpx.Response(status, json={"detail": "Synthetic service error"},
                              request=httpx.Request("GET", "https://runtime.invalid/catalog"))
    return httpx.HTTPStatusError("private upstream text", request=response.request, response=response)


@pytest.mark.parametrize("method,section", [
    ("list_provider_accounts", "provider_accounts"),
    ("list_connections", "connections"),
    ("list_library", "library"),
])
def test_catalog_failure_is_independent_and_does_not_leak_upstream_text(method, section):
    runtime = FakeRuntimeClient()

    def failed():
        raise rejection(503)

    setattr(runtime, method, failed)
    response = TestClient(create_app(runtime_client=runtime)).get("/api/control-plane")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload[section] == []
    assert payload["sections"][section] == "unavailable"
    for remaining in {"provider_accounts", "connections", "library"} - {section}:
        assert payload[remaining]
        assert payload["sections"][remaining] == "ready"
    assert payload["me"]["user_id"] == "demo-owner"
    assert "private upstream" not in response.text


@pytest.mark.parametrize("method", ["current_user", "health", "list_provider_accounts", "list_connections", "list_library"])
@pytest.mark.parametrize("status", [401, 403])
def test_any_catalog_auth_failure_is_not_an_empty_or_partial_success(method, status):
    runtime = FakeRuntimeClient()

    def failed():
        raise rejection(status)

    setattr(runtime, method, failed)
    response = TestClient(create_app(runtime_client=runtime)).get("/api/control-plane")
    assert response.status_code == status, response.text
    assert "provider_accounts" not in response.json()


def test_health_outage_preserves_ready_accounts_without_claiming_provider_support():
    runtime = FakeRuntimeClient()

    def failed():
        raise rejection(503)

    runtime.health = failed
    response = TestClient(create_app(runtime_client=runtime)).get("/api/control-plane")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider_accounts"][0]["account_id"] == "acct_1"
    assert payload["sections"]["provider_accounts"] == "ready"
    assert payload["sections"]["runtime_health"] == "unavailable"
    assert payload["current_native_claude"]["available"] is False


def test_independent_native_reads_start_together():
    runtime = FakeRuntimeClient()
    barrier = threading.Barrier(5, timeout=2)
    started = []
    for name in ["current_user", "health", "list_provider_accounts", "list_connections", "list_library"]:
        original = getattr(runtime, name)

        def read(original=original, name=name):
            started.append(name)
            barrier.wait()
            return original()

        setattr(runtime, name, read)
    response = TestClient(create_app(runtime_client=runtime)).get("/api/control-plane")
    assert response.status_code == 200, response.text
    assert len(started) == 5
    assert set(response.json()["sections"].values()) == {"ready"}


def test_accounts_query_finishes_while_resource_library_is_still_blocked():
    runtime = FakeRuntimeClient()
    entered, release = threading.Event(), threading.Event()
    original = runtime.list_library

    def blocked_library():
        entered.set()
        assert release.wait(timeout=5), "test must release the pending library read"
        return original()

    runtime.list_library = blocked_library
    client = TestClient(create_app(runtime_client=runtime))
    with ThreadPoolExecutor(max_workers=2) as pool:
        resources = pool.submit(client.get, "/api/control-plane?sections=resources")
        try:
            assert entered.wait(timeout=2)
            accounts = pool.submit(client.get, "/api/control-plane?sections=accounts").result(timeout=2)
            assert accounts.status_code == 200, accounts.text
            assert accounts.json()["provider_accounts"][0]["account_id"] == "acct_1"
            assert accounts.json()["sections"]["provider_accounts"] == "ready"
            assert accounts.json()["sections"]["library"] == "not_requested"
            assert not resources.done(), "account response must not wait for the Library response"
        finally:
            release.set()
        assert resources.result(timeout=2).status_code == 200


@pytest.mark.parametrize("selection,expected", [
    ("accounts", {"me", "runtime_health", "provider_accounts"}),
    ("resources", {"me", "connections", "library"}),
])
def test_selected_catalog_group_calls_only_its_native_readers(selection, expected):
    runtime = FakeRuntimeClient()
    called = []
    names = {"me": "current_user", "runtime_health": "health", "provider_accounts": "list_provider_accounts",
             "connections": "list_connections", "library": "list_library"}
    for name, method in names.items():
        original = getattr(runtime, method)

        def read(name=name, original=original):
            called.append(name)
            assert name in expected, "unrequested catalog must not be read"
            return original()

        setattr(runtime, method, read)
    response = TestClient(create_app(runtime_client=runtime)).get(f"/api/control-plane?sections={selection}")
    assert response.status_code == 200, response.text
    assert set(called) == expected
    for name in names:
        assert response.json()["sections"][name] == ("ready" if name in expected else "not_requested")
    if selection == "accounts":
        assert "provider_options" in response.json()
    else:
        assert "provider_options" not in response.json()
        assert "provider_accounts" not in response.json()


@pytest.mark.parametrize("selection,method", [("accounts", "list_provider_accounts"), ("resources", "list_library")])
@pytest.mark.parametrize("status", [401, 403])
def test_selected_catalog_groups_preserve_native_auth_failure(selection, method, status):
    runtime = FakeRuntimeClient()

    def failed():
        raise rejection(status)

    setattr(runtime, method, failed)
    response = TestClient(create_app(runtime_client=runtime)).get(f"/api/control-plane?sections={selection}")
    assert response.status_code == status, response.text


def test_unknown_catalog_group_is_rejected_before_native_reads():
    runtime = FakeRuntimeClient()

    def unexpected():
        raise AssertionError("invalid section must not read native state")

    runtime.current_user = unexpected
    response = TestClient(create_app(runtime_client=runtime)).get("/api/control-plane?sections=unknown")
    assert response.status_code == 422


def test_launch_catalog_declares_exact_direct_account_authentication_methods():
    response = TestClient(create_app(runtime_client=FakeRuntimeClient())).get("/api/bootstrap")
    assert response.status_code == 200, response.text
    options = {item["profile"]: item for item in response.json()["new_workspace_options"]}
    for profile in ("direct-openai", "direct-anthropic", "direct-xai"):
        assert options[profile]["account_auth_methods"] == ["api_key"]
    for profile in ("codex-cli", "claude-code", "grok-build", "openclaw-general"):
        assert "account_auth_methods" not in options[profile]
