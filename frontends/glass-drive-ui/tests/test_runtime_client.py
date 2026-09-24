from __future__ import annotations

import uuid

import glass_drive_ui.runtime_client as runtime_client_module
from glass_drive_ui.runtime_client import RuntimeClient


def test_provider_account_capabilities_reads_runtime_registry(monkeypatch):
    client = RuntimeClient("https://runtime.example.invalid")
    monkeypatch.setattr(client, "_request", lambda method, path: {
        "profile_providers": {"grok-build": ["grok", "xai"]}
    } if (method, path) == ("GET", "/v1/provider-accounts/capabilities") else {})
    assert client.provider_account_capabilities() == {"grok-build": ["grok", "xai"]}


def test_runtime_client_refreshes_dynamic_headers_for_every_upstream_request(monkeypatch):
    captured_headers: list[dict[str, str]] = []

    class SyntheticResponse:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class SyntheticHttpClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            _ = method, url
            captured_headers.append(dict(kwargs.get("headers") or {}))
            return SyntheticResponse()

    monkeypatch.setattr(runtime_client_module.httpx, "Client", SyntheticHttpClient)
    scoped = RuntimeClient(
        "https://runtime.example.invalid",
        headers={"X-WPR-Token": "service-token"},
    ).with_headers_factory(
        lambda: {"X-GlassHive-User-Assertion": uuid.uuid4().hex}
    )

    scoped.get_preferences()
    scoped.list_activity()

    assert len(captured_headers) == 2
    assert all(headers["X-WPR-Token"] == "service-token" for headers in captured_headers)
    assert (
        captured_headers[0]["X-GlassHive-User-Assertion"]
        != captured_headers[1]["X-GlassHive-User-Assertion"]
    )


def test_runtime_client_submits_provider_setup_input_only_to_the_scoped_input_route(monkeypatch):
    captured: list[tuple[str, str, dict[str, object]]] = []

    class SyntheticResponse:
        status_code = 200
        content = b'{"status":"connecting","complete":false}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "connecting", "complete": False}

    class SyntheticHttpClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            captured.append((method, url, dict(kwargs)))
            return SyntheticResponse()

    monkeypatch.setattr(runtime_client_module.httpx, "Client", SyntheticHttpClient)
    client = RuntimeClient("https://runtime.example.invalid")

    result = client.submit_provider_account_setup_input(
        "acct_public_safe", "synthetic-browser-code"
    )

    assert result == {"status": "connecting", "complete": False}
    assert captured == [
        (
            "POST",
            "https://runtime.example.invalid/v1/provider-accounts/acct_public_safe/setup/input",
            {
                "json": {"value": "synthetic-browser-code"},
                "headers": None,
            },
        )
    ]


def test_runtime_client_uses_versioned_execution_workspace_routes(monkeypatch):
    captured: list[tuple[str, str, dict[str, object]]] = []

    class SyntheticResponse:
        status_code = 201
        content = b'{"workspace_id":"wsp_synthetic"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"workspace_id": "wsp_synthetic"}

    class SyntheticHttpClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            captured.append((method, url, dict(kwargs)))
            return SyntheticResponse()

    monkeypatch.setattr(runtime_client_module.httpx, "Client", SyntheticHttpClient)
    client = RuntimeClient("https://runtime.example.invalid")

    assert client.create_execution_workspace(
        "project/synthetic", file_placement="member_private"
    ) == {"workspace_id": "wsp_synthetic"}
    assert client.add_execution_workspace_member(
        "workspace/synthetic", {"profile": "codex-cli"}
    ) == {"workspace_id": "wsp_synthetic"}
    assert client.execution_workspace_members("workspace/synthetic") == {
        "workspace_id": "wsp_synthetic"
    }
    assert captured == [
        (
            "POST",
            "https://runtime.example.invalid/v1/projects/project%2Fsynthetic/execution-workspaces",
            {
                "json": {
                    "mode": "shared",
                    "execution_mode": "docker",
                    "file_placement": "member_private",
                },
                "headers": None,
            },
        ),
        (
            "POST",
            "https://runtime.example.invalid/v1/workspaces/workspace%2Fsynthetic/members",
            {"json": {"profile": "codex-cli"}, "headers": None},
        ),
        (
            "GET",
            "https://runtime.example.invalid/v1/workspaces/workspace%2Fsynthetic/members",
            {"json": None, "headers": None},
        ),
    ]


def test_runtime_client_uses_exact_native_control_routes(monkeypatch):
    captured: list[tuple[str, str, dict[str, object]]] = []

    class SyntheticResponse:
        status_code = 200
        content = b'{"status":"queued"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "queued"}

    class SyntheticHttpClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            captured.append((method, url, dict(kwargs)))
            return SyntheticResponse()

    monkeypatch.setattr(runtime_client_module.httpx, "Client", SyntheticHttpClient)
    client = RuntimeClient("https://runtime.example.invalid")
    payload = {
        "run_id": "run-exact",
        "attempt_id": "attempt-exact",
        "action": "permission",
        "request_id": "request-exact",
        "option_id": "allow",
    }

    assert client.native_control_state("worker/exact") == {"status": "queued"}
    assert client.native_control("worker/exact", payload) == {"status": "queued"}
    assert captured == [
        (
            "GET",
            "https://runtime.example.invalid/v1/workers/worker%2Fexact/native-control",
            {"json": None, "headers": None},
        ),
        (
            "POST",
            "https://runtime.example.invalid/v1/workers/worker%2Fexact/native-control",
            {"json": payload, "headers": None},
        ),
    ]
