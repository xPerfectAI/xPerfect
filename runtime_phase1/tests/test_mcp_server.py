from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlsplit
from fastmcp import Client
from fastmcp.exceptions import ToolError
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from workers_projects_runtime import mcp_server, runtime_env
from workers_projects_runtime.api import create_app
from workers_projects_runtime.bootstrap import sign_bootstrap_source_path
from workers_projects_runtime.mcp_server import create_mcp_server
from workers_projects_runtime.models import CreateDelegationRequest
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.signed_links import resolve_signed_link_ref


def _fake_runtime_for_profile(profile: str) -> str:
    return "openclaw" if profile.startswith("openclaw") else profile


def _delegation_identity_assertion(identity: dict, secret: str) -> str:
    canonical = json.dumps(
        {
            "call_identity_digest": str(identity.get("call_identity_digest") or ""),
            "goal_digest": str(identity.get("goal_digest") or ""),
            "idempotency_key": str(identity.get("idempotency_key") or ""),
            "objective_ordinal": int(identity.get("objective_ordinal")),
            "source_event_id": str(identity.get("source_event_id") or ""),
            "version": int(identity.get("version")),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        b"viventium.delegation-identity.v1\0" + canonical,
        hashlib.sha256,
    ).hexdigest()


def _scoped_delegation_identity_assertion(
    identity: dict,
    secret: str,
    *,
    tenant_id: str,
    owner_id: str,
) -> str:
    canonical = json.dumps(
        {
            "identity": {
                "call_identity_digest": str(identity.get("call_identity_digest") or ""),
                "goal_digest": str(identity.get("goal_digest") or ""),
                "idempotency_key": str(identity.get("idempotency_key") or ""),
                "launch_payload_digest": str(identity.get("launch_payload_digest") or ""),
                "objective_ordinal": int(identity.get("objective_ordinal")),
                "source_event_id": str(identity.get("source_event_id") or ""),
                "version": int(identity.get("version")),
            },
            "owner_id": owner_id,
            "tenant_id": tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        b"viventium.delegation-identity.v2\0" + canonical,
        hashlib.sha256,
    ).hexdigest()


def _launch_payload_digest(payload: dict) -> str:
    canonical = json.dumps(
        {
            "alias": str(payload.get("alias") or "").strip(),
            "backend": str(payload.get("backend") or "").strip(),
            "bootstrap_profile": str(
                payload.get("bootstrap_profile") or payload.get("bootstrapProfile") or ""
            ).strip(),
            "connected_account_content_intent": bool(
                payload.get("connected_account_content_intent", False)
            ),
            "effort": str(payload.get("effort") or "").strip(),
            "execution_mode": str(
                payload.get("execution_mode") or payload.get("executionMode") or ""
            ).strip(),
            "expose_diagnostics": bool(payload.get("expose_diagnostics", False)),
            "goal": str(payload.get("goal") or "").strip(),
            "instruction": str(payload.get("instruction") or "").strip(),
            "owner_id": str(payload.get("owner_id") or "").strip(),
            "profile": str(payload.get("profile") or "").strip(),
            "project_id": str(payload.get("project_id") or "").strip(),
            "require_callback": bool(payload.get("require_callback", False)),
            "reuse_existing_workspace": bool(
                payload.get("reuse_existing_workspace", False)
            ),
            "title": str(payload.get("title") or "").strip(),
            "worker_name": str(
                payload.get("worker_name") or payload.get("workerName") or ""
            ).strip(),
            "worker_role": str(
                payload.get("worker_role") or payload.get("workerRole") or ""
            ).strip(),
            "workspace_root": str(
                payload.get("workspace_root") or payload.get("workspaceRoot") or ""
            ).strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _patch_host_runtime_requirements_ok(monkeypatch):
    monkeypatch.setattr(mcp_server, "host_runtime_requirement_issue", lambda _profile, _runtime_name: None)


def _configure_enterprise_mcp_oauth(monkeypatch):
    """Keep non-OAuth enterprise tests on the production fail-closed contract."""

    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", "https://glasshive.example.invalid/mcp")


class FakeApiClient:
    def workspace_catalog(self, **kwargs):
        return {"items": [], "next_cursor": None}

    def list_projects(self, owner_id: str | None = None):
        items = [
            {"project_id": "prj_123", "owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"},
            {"project_id": "prj_999", "owner_id": "other", "title": "Other", "goal": "Ignore me"},
        ]
        if owner_id:
            return [item for item in items if item["owner_id"] == owner_id]
        return items

    def create_project(self, *, owner_id: str | None, title: str, goal: str, default_worker_profile: str = "codex-cli"):
        return {
            "project_id": "prj_new",
            "owner_id": owner_id,
            "title": title,
            "goal": goal,
            "default_worker_profile": default_worker_profile,
        }

    def get_project(self, project_id: str):
        return {"project_id": project_id, "owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"}

    def get_preferences(self):
        return {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": "",
            "codex_reasoning_effort": "",
            "claude_effort": "",
            "openclaw_effort": "",
            "updated_at": "",
        }

    def update_preferences(self, payload: dict):
        return {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": payload.get("default_worker_profile", ""),
            "codex_reasoning_effort": payload.get("codex_reasoning_effort", ""),
            "claude_effort": payload.get("claude_effort", ""),
            "openclaw_effort": payload.get("openclaw_effort", ""),
            "updated_at": "2026-05-24T00:00:00+00:00",
        }

    def provider_accounts(self):
        return [
            {
                "account_id": "acct_codex_ready",
                "provider": "codex",
                "display_name": "My Codex",
                "status": "ready",
                "is_default": True,
            },
            {
                "account_id": "acct_claude_ready",
                "provider": "claude",
                "display_name": "My Claude",
                "status": "ready",
                "is_default": True,
            },
        ]

    def list_project_runs(self, project_id: str):
        return [{"run_id": "run_123", "project_id": project_id, "state": "completed"}]

    def list_project_events(self, project_id: str):
        return [{"event_id": "evt_123", "project_id": project_id, "event_type": "run.completed"}]

    def list_workers(self, project_id: str):
        return [{"worker_id": "wrk_123", "project_id": project_id, "profile": "openclaw-general", "state": "ready"}]

    def find_worker_by_alias_across_projects(
        self,
        *,
        owner_id: str | None,
        alias: str,
        execution_mode: str | None = None,
    ):
        scoped = mcp_server._request_scoped_alias(alias)
        for project in self.list_projects(owner_id):
            for worker in self.list_workers(project["project_id"]):
                if worker.get("state") == "terminated":
                    continue
                if execution_mode and worker.get("execution_mode") and worker.get("execution_mode") != execution_mode:
                    continue
                if worker.get("alias") == scoped:
                    return {"project": project, "worker": worker}
        return None

    def create_worker(
        self,
        *,
        project_id: str,
        owner_id: str | None,
        name: str,
        role: str,
        profile: str = "codex-cli",
        backend: str = "openclaw",
        execution_mode: str = "docker",
        resource_class: str = "standard",
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        start_synchronously: bool = True,
        workspace_kind: str | None = None,
    ):
        return {
            "worker_id": "wrk_new",
            "project_id": project_id,
            "owner_id": owner_id,
            "name": name,
            "role": role,
            "profile": profile,
            "backend": backend,
            "execution_mode": execution_mode,
            "resource_class": resource_class,
            "runtime": _fake_runtime_for_profile(profile),
            "alias": alias,
            "workspace_root": workspace_root,
            "state": "ready",
            "bootstrap_bundle": bootstrap_bundle,
            "start_synchronously": start_synchronously,
            "workspace_kind": workspace_kind,
        }

    def find_or_resume_worker(self, **kwargs):
        payload = self.create_worker(**kwargs)
        payload["worker_id"] = "wrk_resumed"
        return payload

    def get_worker(self, worker_id: str):
        return {
            "worker_id": worker_id,
            "project_id": "prj_123",
            "tenant_id": "tenant-alpha",
            "owner_id": "demo-owner",
            "profile": "openclaw-general",
            "state": "ready",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_123",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "ready",
            },
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "project_runs": [{"run_id": "run_123"}],
        }

    def list_artifacts(self, worker_id: str):
        return {
            "items": [
                {
                    "path": "index.html",
                    "name": "index.html",
                    "size": 128,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=index.html",
                },
                {
                    "path": ".codex/config.toml",
                    "name": "config.toml",
                    "size": 64,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=.codex/config.toml",
                },
                {
                    "path": "tmp/chrome-user-data/Default/Default/Extensions/fdpohaocaechififmbbbbbknoalclacl/8.6_0/capture/index.html",
                    "name": "index.html",
                    "size": 197,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=tmp/chrome-user-data/Default/Default/Extensions/fdpohaocaechififmbbbbbknoalclacl/8.6_0/capture/index.html",
                },
                {
                    "path": "uploads/source.txt.metadata.json",
                    "name": "source.txt.metadata.json",
                    "size": 32,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=uploads/source.txt.metadata.json",
                },
            ]
        }

    def worker_runs(self, worker_id: str):
        return [{"run_id": "run_123", "worker_id": worker_id, "state": "completed"}]

    def worker_events(self, worker_id: str):
        return [{"event_id": "evt_123", "worker_id": worker_id, "event_type": "worker.ready"}]

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
        continuation_context: dict | None = None,
    ):
        return {
            "run_id": "run_assign",
            "worker_id": worker_id,
            "instruction": instruction,
            "effort": effort or "",
            "state": "queued",
            "bootstrap_bundle": bootstrap_bundle,
            "continuation_context": continuation_context,
        }

    def send_message(self, worker_id: str, message: str):
        return {"run_id": "run_msg", "worker_id": worker_id, "instruction": message, "state": "queued"}

    def schedule_run(self, worker_id: str, instruction: str, *, run_at: str | None = None, schedule_text: str | None = None, delay_seconds: int | None = None, bootstrap_bundle: dict | None = None):
        return {
            "schedule_id": "sch_123",
            "worker_id": worker_id,
            "project_id": "prj_123",
            "instruction": instruction,
            "schedule_text": schedule_text or "",
            "run_at": run_at or "2026-05-23T19:00:00+00:00",
            "state": "pending",
            "delay_seconds": delay_seconds,
        }

    def worker_schedules(self, worker_id: str, include_done: bool = False):
        return [{"schedule_id": "sch_123", "worker_id": worker_id, "state": "pending", "include_done": include_done}]

    def get_schedule(self, schedule_id: str):
        return {"schedule_id": schedule_id, "worker_id": "wrk_123", "state": "pending"}

    def lifecycle(self, worker_id: str, action: str):
        return {"worker_id": worker_id, "state": "ready", "action": action}

    def update_workspace(self, worker_id: str, payload: dict):
        return {"worker_id": worker_id, **payload}

    def desktop_action(self, worker_id: str, action: str, url: str | None = None):
        return {"worker_id": worker_id, "action": action, "url": url, "view_url": "http://127.0.0.1:62310/?autoconnect=1"}

    def takeover(self, worker_id: str):
        return {"supported": True, "url": f"http://127.0.0.1:8766/ui/workers/{worker_id}/view", "mode": "workstation-desktop"}

    def get_run(self, run_id: str):
        return {"run_id": run_id, "worker_id": "wrk_123", "project_id": "prj_123", "state": "completed", "output_text": "OK"}

    def metrics(self):
        return {"projects": 1, "workers": 1, "runs": 2, "queued_runs": 0, "active_runs": 0, "events": 3}


class TrackingApiClient(FakeApiClient):
    def __init__(self):
        self.calls: list[str] = []
        self.create_project_payloads: list[dict] = []
        self.create_worker_payloads: list[dict] = []
        self.find_or_resume_payloads: list[dict] = []
        self.assign_run_payloads: list[dict] = []
        self.schedule_run_payloads: list[dict] = []
        self.sent_messages: list[dict] = []
        self.update_workspace_payloads: list[dict] = []

    def list_projects(self, owner_id: str | None = None):
        self.calls.append("list_projects")
        return super().list_projects(owner_id)

    def list_workers(self, project_id: str):
        self.calls.append("list_workers")
        return super().list_workers(project_id)

    def create_project(self, **kwargs):
        self.calls.append("create_project")
        self.create_project_payloads.append(kwargs)
        return super().create_project(**kwargs)

    def find_or_resume_worker(self, **kwargs):
        self.calls.append("find_or_resume_worker")
        self.find_or_resume_payloads.append(kwargs)
        return super().find_or_resume_worker(**kwargs)

    def create_worker(self, **kwargs):
        self.create_worker_payloads.append(kwargs)
        return super().create_worker(**kwargs)

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
        continuation_context: dict | None = None,
    ):
        self.calls.append("assign_run")
        self.assign_run_payloads.append({"worker_id": worker_id, "instruction": instruction, "effort": effort or "", "bootstrap_bundle": bootstrap_bundle, "continuation_context": continuation_context})
        return super().assign_run(worker_id, instruction, effort=effort, bootstrap_bundle=bootstrap_bundle, continuation_context=continuation_context)

    def send_message(self, worker_id: str, message: str):
        self.calls.append("send_message")
        self.sent_messages.append({"worker_id": worker_id, "message": message})
        return super().send_message(worker_id, message)

    def update_workspace(self, worker_id: str, payload: dict):
        self.calls.append("update_workspace")
        self.update_workspace_payloads.append({"worker_id": worker_id, **payload})
        return super().update_workspace(worker_id, payload)

    def schedule_run(self, worker_id: str, instruction: str, *, run_at: str | None = None, schedule_text: str | None = None, delay_seconds: int | None = None, bootstrap_bundle: dict | None = None):
        self.schedule_run_payloads.append(
            {
                "worker_id": worker_id,
                "instruction": instruction,
                "run_at": run_at,
                "schedule_text": schedule_text,
                "delay_seconds": delay_seconds,
                "bootstrap_bundle": bootstrap_bundle,
            }
        )
        return super().schedule_run(
            worker_id,
            instruction,
            run_at=run_at,
            schedule_text=schedule_text,
            delay_seconds=delay_seconds,
            bootstrap_bundle=bootstrap_bundle,
        )


class PreferenceApiClient(TrackingApiClient):
    def __init__(self):
        super().__init__()
        self.preference_payloads: list[dict] = []
        self.preferences: dict = {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": "codex-cli",
            "codex_reasoning_effort": "xhigh",
            "claude_effort": "",
            "openclaw_effort": "",
            "updated_at": "2026-05-24T00:00:00+00:00",
        }

    def get_preferences(self):
        return dict(self.preferences)

    def update_preferences(self, payload: dict):
        self.preference_payloads.append(payload)
        self.preferences = {**self.preferences, **payload}
        return dict(self.preferences)


class RecordingWorkersProjectsApiClient(mcp_server.WorkersProjectsApiClient):
    def __init__(self):
        super().__init__(base_url="http://glasshive.example.test", api_token="")
        self.requests: list[dict] = []

    def _request(self, method: str, path: str, *, json_body: dict | None = None):
        path = self._validated_request_path(path)
        self.requests.append({"method": method, "path": path, "json_body": json_body})
        if path.endswith("/runs") or path.endswith("/events") or path.endswith("/workers") or "/schedules" in path:
            return {"items": []}
        return {"ok": True}


class SharedMemberTrackingApiClient(FakeApiClient):
    def __init__(self):
        super().__init__()
        self.shared_member_calls: list[dict] = []

    def add_execution_workspace_member(self, workspace_id: str, **kwargs):
        self.shared_member_calls.append({"workspace_id": workspace_id, **kwargs})
        return {"workspace_id": workspace_id, "profile": kwargs.get("profile")}


def test_api_client_omitted_shared_profile_is_left_for_project_default():
    api = RecordingWorkersProjectsApiClient()

    api.add_execution_workspace_member("wsp_default", name="Project default")
    assert api.requests[-1]["json_body"] == {"name": "Project default", "role": "member"}

    api.add_execution_workspace_member("wsp_explicit", profile="grok-build")
    assert api.requests[-1]["json_body"]["profile"] == "grok-build"


def test_shared_member_mcp_tool_does_not_invent_codex_default():
    api = SharedMemberTrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            await client.call_tool(
                "workspace_shared_add_member",
                {"workspace_id": "wsp_default", "name": "Project default"},
            )

    asyncio.run(scenario())
    assert api.shared_member_calls[0]["profile"] is None


def test_runtime_client_create_delegation_sends_atomic_headers(monkeypatch):
    captured: dict = {}

    class CapturingResponseClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def request(self, method, url, *, json=None, headers=None):
            captured.update(
                {
                    "method": method,
                    "url": url,
                    "json": json,
                    "headers": dict(headers or {}),
                }
            )
            request = httpx.Request(method, url)
            return httpx.Response(
                201,
                json={"workRef": "work_atomic_1", "state": "queued"},
                request=request,
            )

    monkeypatch.setattr(mcp_server.httpx, "Client", CapturingResponseClient)
    monkeypatch.setattr(mcp_server, "_require_enterprise_mcp_service_auth", lambda _headers: None)
    monkeypatch.setattr(mcp_server, "_request_headers", lambda: {})
    monkeypatch.setattr(mcp_server, "load_viventium_runtime_env", lambda _required: None)
    monkeypatch.setattr(mcp_server, "mint_service_assertion", lambda *_args, **_kwargs: "assertion-test")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", "secret-test")
    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_AUTH_MODE", raising=False)
    client = mcp_server.WorkersProjectsApiClient(
        base_url="http://glasshive.example.test",
        api_token="api-test-token",
    )

    result = client.create_delegation(
        tenant_id="tenant-alpha",
        owner_id="owner-alpha",
        idempotency_key="delegation-key-1",
        payload={"title": "Atomic mission"},
    )

    assert result == {"workRef": "work_atomic_1", "state": "queued"}
    assert captured == {
        "method": "POST",
        "url": "http://glasshive.example.test/v1/delegations",
        "json": {"title": "Atomic mission"},
        "headers": {
            "Authorization": "Bearer api-test-token",
            mcp_server.SERVICE_ASSERTION_HEADER: "assertion-test",
            "Idempotency-Key": "delegation-key-1",
        },
    }


@pytest.mark.parametrize("status,code,retryable", [
    (503, "host_capacity", True),
    (429, "provider_rate_limit", True),
    (409, "delegation_idempotency_conflict", False),
    (403, "owner_scope_denied", False),
])
def test_runtime_client_preserves_typed_account_rejection(monkeypatch, status, code, retryable):
    response = httpx.Response(
        status,
        json={"detail": {"code": code, "message": "The request is not accepted."}},
        headers={"Retry-After": "4"},
        request=httpx.Request("POST", "http://runtime.test/v1/delegations"),
    )
    class ResponseClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def request(self, *args, **kwargs): return response

    monkeypatch.setattr(mcp_server.httpx, "Client", ResponseClient)
    monkeypatch.setattr(mcp_server, "_require_enterprise_mcp_service_auth", lambda _: None)
    monkeypatch.setattr(mcp_server, "_request_headers", lambda: {})
    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_AUTH_MODE", raising=False)
    client = mcp_server.WorkersProjectsApiClient(base_url="http://runtime.test", api_token="")
    with pytest.raises(mcp_server.GlassHiveBlockedError) as caught:
        client._request("POST", "/v1/delegations", json_body={})
    result = mcp_server._blocked_dispatch_result(
        caught.value.payload, profile="codex-cli", execution_mode="host",
    )
    assert result["status"] == "blocked"
    assert result["failure_class"] == code
    assert result["failure_retryable"] is retryable
    assert result["retry_after"] == "4"
    assert not result["callback_ready"]
    assert not result["view_steer_url"]


def test_runtime_client_preserves_safe_closed_workspace_detail(monkeypatch):
    class ClosedResponseClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def request(self, method, url, *, json=None, headers=None):
            request = httpx.Request(method, url)
            return httpx.Response(
                409,
                json={"detail": "Workspace is closed; create a new workspace for new work"},
                request=request,
            )

    monkeypatch.setattr(mcp_server.httpx, "Client", ClosedResponseClient)
    monkeypatch.setattr(mcp_server, "_require_enterprise_mcp_service_auth", lambda _headers: None)
    monkeypatch.setattr(mcp_server, "_request_headers", lambda: {})
    client = mcp_server.WorkersProjectsApiClient(
        base_url="http://glasshive.example.test",
        api_token="",
    )

    with pytest.raises(mcp_server.GlassHiveApiError) as exc_info:
        client.assign_run("wrk_closed", "Do not strand this work")

    assert exc_info.value.status_code == 409
    assert str(exc_info.value) == "Workspace is closed; create a new workspace for new work"


def test_worker_message_tool_surfaces_closed_workspace_recovery():
    class ClosedWorkspaceApi(FakeApiClient):
        def send_message(self, worker_id: str, message: str):
            raise mcp_server.GlassHiveApiError(
                409,
                "Workspace is closed; create a new workspace for new work",
            )

    server = create_mcp_server(api_client=ClosedWorkspaceApi())

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="closed; create a new workspace"):
                await client.call_tool(
                    "worker_message",
                    {"worker_id": "wrk_closed", "message": "Do not strand this work"},
                )

    asyncio.run(scenario())


class PollingApiClient(FakeApiClient):
    def __init__(self, states: list[str]):
        self.states = list(states)
        self.get_run_calls = 0

    def get_run(self, run_id: str):
        self.get_run_calls += 1
        state = self.states[min(self.get_run_calls - 1, len(self.states) - 1)]
        return {
            "run_id": run_id,
            "worker_id": "wrk_poll",
            "project_id": "prj_poll",
            "state": state,
            "output_text": "Poll completed" if state == "completed" else "",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_poll",
                "state": "ready",
                "owner_id": "demo-owner",
            },
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "project_runs": [],
        }


class ReadyArtifactsPollingApiClient(PollingApiClient):
    def __init__(self, states: list[str], *, modified_at: float | None = None):
        super().__init__(states)
        self.modified_at = modified_at

    def list_artifacts(self, worker_id: str):
        items = [
            {
                "path": "out/report/final-report.pdf",
                "name": "final-report.pdf",
                "size": 4096,
                "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=out/report/final-report.pdf",
            },
            {
                "path": "out/data/source-ledger.csv",
                "name": "source-ledger.csv",
                "size": 1024,
                "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=out/data/source-ledger.csv",
            },
        ]
        if self.modified_at is not None:
            for item in items:
                item["modified_at"] = self.modified_at
        return {"items": items}


class RememberedDispatchApiClient(TrackingApiClient):
    def __init__(self, *, tenant_id: str = "local"):
        super().__init__()
        self.tenant_id = tenant_id
        self.workers: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}

    def find_or_resume_worker(self, **kwargs):
        payload = super().find_or_resume_worker(**kwargs)
        payload.update(
            {
                "tenant_id": self.tenant_id,
                "owner_id": kwargs.get("owner_id"),
                "project_id": kwargs.get("project_id"),
                "last_run_id": "",
            }
        )
        self.workers[payload["worker_id"]] = payload
        return payload

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
        continuation_context: dict | None = None,
    ):
        payload = super().assign_run(
            worker_id,
            instruction,
            effort=effort,
            bootstrap_bundle=bootstrap_bundle,
            continuation_context=continuation_context,
        )
        payload.update(
            {
                "tenant_id": self.tenant_id,
                "project_id": self.workers.get(worker_id, {}).get("project_id") or "prj_new",
                "state": "completed",
                "output_text": "remembered dispatch completed",
                "error_text": "",
            }
        )
        self.runs[payload["run_id"]] = payload
        if worker_id in self.workers:
            self.workers[worker_id]["last_run_id"] = payload["run_id"]
        return payload

    def get_run(self, run_id: str):
        return self.runs[run_id]

    def get_worker(self, worker_id: str):
        return self.workers[worker_id]

    def worker_live(self, worker_id: str):
        worker = self.get_worker(worker_id)
        run_id = str(worker.get("last_run_id") or "")
        run = self.runs.get(run_id)
        runs = [run] if run else []
        return {
            "worker": worker,
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "runs": runs,
            "project_runs": runs,
        }


class RetryableFailureApiClient(FakeApiClient):
    def __init__(self):
        self.assigned: list[dict[str, object]] = []

    def get_run(self, run_id: str):
        if run_id == "run_retryable_failed":
            return {
                "run_id": "run_retryable_failed",
                "worker_id": "wrk_retry",
                "project_id": "prj_retry",
                "tenant_id": "local",
                "state": "failed",
                "queued_at": "2026-05-25T10:00:00+00:00",
                "instruction": "Build the requested research workbook and report.",
                "output_text": "",
                "error_text": "codex-cli exited with code 1: provider failed",
                "failure_class": "provider_rate_limited",
                "failure_retryable": True,
                "failure_user_message": "The provider rate-limited the worker before it finished.",
                "failure_recommended_recovery": "Use workspace_continue to resume the same workspace.",
                "failure_diagnostic_summary": "response.failed: Too Many Requests",
                "continuation_context_json": "{}",
            }
        return {
            "run_id": run_id,
            "worker_id": "wrk_retry",
            "project_id": "prj_retry",
            "tenant_id": "local",
            "state": "queued",
            "queued_at": "2026-05-25T10:05:00+00:00",
            "instruction": self.assigned[-1]["instruction"] if self.assigned else "",
            "continuation_context_json": (
                json.dumps(self.assigned[-1].get("continuation_context") or {})
                if self.assigned
                else "{}"
            ),
            "output_text": "",
            "error_text": "",
        }

    def get_worker(self, worker_id: str):
        payload = super().get_worker(worker_id)
        payload.update({"worker_id": worker_id, "project_id": "prj_retry", "tenant_id": "local", "profile": "codex-cli", "state": "ready"})
        return payload

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_retry",
                "state": "ready",
                "owner_id": "demo-owner",
                "last_run_id": "run_retryable_failed",
            },
            "runtime_details": {},
            "project_runs": [],
        }

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
        continuation_context: dict | None = None,
    ):
        self.assigned.append(
            {
                "worker_id": worker_id,
                "instruction": instruction,
                "effort": effort or "",
                "bootstrap_bundle": bootstrap_bundle,
                "continuation_context": continuation_context,
            }
        )
        return {
            "run_id": "run_continued",
            "worker_id": worker_id,
            "project_id": "prj_retry",
            "tenant_id": "local",
            "state": "queued",
            "instruction": instruction,
            "effort": effort or "",
            "bootstrap_bundle": bootstrap_bundle,
            "continuation_context": continuation_context,
            "continuation_context_json": json.dumps(continuation_context or {}),
        }


class EnterpriseRetryableFailureApiClient(RetryableFailureApiClient):
    def __init__(
        self,
        *,
        run_tenant_id: str = "tenant-alpha",
        worker_tenant_id: str = "tenant-alpha",
        worker_owner_id: str = "user-a",
        new_run_tenant_id: str = "tenant-alpha",
        previous_state: str = "failed",
    ):
        super().__init__()
        self.run_tenant_id = run_tenant_id
        self.worker_tenant_id = worker_tenant_id
        self.worker_owner_id = worker_owner_id
        self.new_run_tenant_id = new_run_tenant_id
        self.previous_state = previous_state

    def get_run(self, run_id: str):
        payload = super().get_run(run_id)
        if run_id == "run_retryable_failed":
            payload["tenant_id"] = self.run_tenant_id
            payload["state"] = self.previous_state
        return payload

    def get_worker(self, worker_id: str):
        payload = super().get_worker(worker_id)
        payload.update(
            {
                "tenant_id": self.worker_tenant_id,
                "owner_id": self.worker_owner_id,
            }
        )
        return payload

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
        continuation_context: dict | None = None,
    ):
        payload = super().assign_run(
            worker_id,
            instruction,
            effort=effort,
            bootstrap_bundle=bootstrap_bundle,
            continuation_context=continuation_context,
        )
        payload["tenant_id"] = self.new_run_tenant_id
        return payload


class StaleRequestedRunApiClient(FakeApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_old_failed":
            return {
                "run_id": "run_old_failed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "failed",
                "queued_at": "2026-05-24T10:00:00+00:00",
                "output_text": "",
                "error_text": "Older failed run",
            }
        return {
            "run_id": "run_new_completed",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "completed",
            "queued_at": "2026-05-24T10:05:00+00:00",
            "output_text": "Latest artifact is ready",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_stale",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "ready",
                "last_run_id": "run_new_completed",
            },
            "runs": [
                {
                    "run_id": "run_new_completed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "completed",
                    "queued_at": "2026-05-24T10:05:00+00:00",
                },
                {
                    "run_id": "run_old_failed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "failed",
                    "queued_at": "2026-05-24T10:00:00+00:00",
                },
            ],
            "runtime_details": {},
        }


class HealDuringStatusApiClient(FakeApiClient):
    def __init__(self):
        self.healed = False
        self.get_run_calls = 0

    def get_run(self, run_id: str):
        self.get_run_calls += 1
        state = "completed" if self.healed else "running"
        return {
            "run_id": run_id,
            "worker_id": "wrk_heal",
            "project_id": "prj_heal",
            "state": state,
            "queued_at": "2026-06-25T20:44:48+00:00",
            "output_text": "Recovered result is ready" if state == "completed" else "",
            "error_text": "",
        }

    def get_worker(self, worker_id: str):
        self.healed = True
        return {
            "worker_id": worker_id,
            "project_id": "prj_heal",
            "tenant_id": "tenant-alpha",
            "owner_id": "demo-owner",
            "state": "ready",
            "last_run_id": "run_heal",
        }

    def worker_live(self, worker_id: str):
        self.healed = True
        return {
            "worker": self.get_worker(worker_id),
            "runs": [self.get_run("run_heal")],
            "project_runs": [self.get_run("run_heal")],
            "runtime_details": {},
        }


class ManyArtifactsApiClient(FakeApiClient):
    def list_artifacts(self, worker_id: str):
        return {
            "items": [
                {
                    "path": f"deliverables/report_{index}.pdf",
                    "name": f"report_{index}.pdf",
                    "size": 1024 + index,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=deliverables/report_{index}.pdf",
                }
                for index in range(7)
            ]
        }


class OutputReferencedManyArtifactsApiClient(FakeApiClient):
    def get_run(self, run_id: str):
        return {
            "run_id": run_id,
            "worker_id": "wrk_123",
            "project_id": "prj_123",
            "state": "completed",
            "output_text": (
                "FINAL REPORT:\n"
                "Deliverable PDF: [example_client_midtown_3bed_report.pdf]"
                "(/workspace/project/report/example_client_midtown_3bed_report.pdf)\n"
                "Editable HTML: [example_client_midtown_3bed_report.html]"
                "(/workspace/project/report/example_client_midtown_3bed_report.html)"
            ),
            "error_text": "",
        }

    def list_artifacts(self, worker_id: str):
        paths = [
            "data/midtown_3bed_active_listings.csv",
            "data/midtown_3bed_portal_checks.csv",
            "data/midtown_3bed_sold_references.csv",
            "data/midtown_3bed_source_register.csv",
            "data/midtown_3bed_market_snapshot.json",
            "report/example_client_midtown_3bed_report.html",
            "report/example_client_midtown_3bed_report.pdf",
        ]
        return {
            "items": [
                {
                    "path": path,
                    "name": path.rsplit("/", 1)[-1],
                    "size": 2048 + index,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path={path}",
                }
                for index, path in enumerate(paths)
            ]
        }


class OlderLastRunApiClient(StaleRequestedRunApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_new_completed":
            return {
                "run_id": "run_new_completed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "completed",
                "queued_at": "2026-05-24T10:05:00+00:00",
                "output_text": "Requested run is the newest result",
                "error_text": "",
            }
        return {
            "run_id": "run_old_failed",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "failed",
            "queued_at": "2026-05-24T10:00:00+00:00",
            "output_text": "",
            "error_text": "Older failed run",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_stale",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "ready",
                "last_run_id": "run_old_failed",
            },
            "runs": [
                {
                    "run_id": "run_new_completed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "completed",
                    "queued_at": "2026-05-24T10:05:00+00:00",
                },
                {
                    "run_id": "run_old_failed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "failed",
                    "queued_at": "2026-05-24T10:00:00+00:00",
                },
            ],
            "runtime_details": {},
        }


class TerminalRequestedNewerRunningApiClient(StaleRequestedRunApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_old_failed":
            return {
                "run_id": "run_old_failed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "failed",
                "queued_at": "2026-05-24T10:00:00+00:00",
                "output_text": "",
                "error_text": "Requested run failed",
            }
        return {
            "run_id": "run_new_running",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "running",
            "queued_at": "2026-05-24T10:05:00+00:00",
            "output_text": "",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_stale",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "running",
                "last_run_id": "run_new_running",
            },
            "runs": [
                {
                    "run_id": "run_new_running",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "running",
                    "queued_at": "2026-05-24T10:05:00+00:00",
                },
                {
                    "run_id": "run_old_failed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "failed",
                    "queued_at": "2026-05-24T10:00:00+00:00",
                },
            ],
            "runtime_details": {},
        }

    def list_artifacts(self, worker_id: str):
        return {
            "items": [
                {
                    "path": "deliverables/stale-prior-result.pdf",
                    "name": "stale-prior-result.pdf",
                    "size": 4096,
                    "modified_at": 1_704_067_200.0,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=deliverables/stale-prior-result.pdf",
                }
            ]
        }


class InterruptedRequestedResumeApiClient(StaleRequestedRunApiClient):
    def __init__(self, latest_states: list[str] | None = None):
        self.latest_states = list(latest_states or ["running"])
        self.latest_get_run_calls = 0

    def _current_latest_state(self) -> str:
        index = min(max(self.latest_get_run_calls, 0), len(self.latest_states) - 1)
        return self.latest_states[index]

    def get_run(self, run_id: str):
        if run_id == "run_old_interrupted":
            return {
                "run_id": "run_old_interrupted",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "interrupted",
                "queued_at": "2026-05-24T10:00:00+00:00",
                "ended_at": "2026-05-24T10:04:41+00:00",
                "output_text": "",
                "error_text": "Requested run was interrupted for resume",
            }
        self.latest_get_run_calls += 1
        state = self.latest_states[min(self.latest_get_run_calls - 1, len(self.latest_states) - 1)]
        return {
            "run_id": "run_resume",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": state,
            "queued_at": "2026-05-24T10:05:00+00:00",
            "output_text": "Resumed result is ready" if state == "completed" else "",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        latest_state = self._current_latest_state()
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_stale",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "running" if latest_state == "running" else "ready",
                "last_run_id": "run_resume",
            },
            "runs": [
                {
                    "run_id": "run_resume",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": latest_state,
                    "queued_at": "2026-05-24T10:05:00+00:00",
                },
                {
                    "run_id": "run_old_interrupted",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "interrupted",
                    "queued_at": "2026-05-24T10:00:00+00:00",
                },
            ],
            "runtime_details": {},
        }


class MixedTimezoneRunOrderApiClient(StaleRequestedRunApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_old_failed":
            return {
                "run_id": "run_old_failed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "failed",
                "queued_at": "2026-05-24T09:30:00+02:00",
                "output_text": "",
                "error_text": "Older failed run",
            }
        return {
            "run_id": "run_new_completed",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "completed",
            "queued_at": "2026-05-24T08:00:00+00:00",
            "output_text": "Newer mixed-timezone artifact is ready",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        payload = super().worker_live(worker_id)
        for item in payload["runs"]:
            if item["run_id"] == "run_old_failed":
                item["queued_at"] = "2026-05-24T09:30:00+02:00"
            else:
                item["queued_at"] = "2026-05-24T08:00:00+00:00"
        return payload


def test_configured_default_worker_profile_fails_loud_when_not_allowed(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "openclaw-general")

    with pytest.raises(RuntimeError, match="GLASSHIVE_DEFAULT_WORKER_PROFILE"):
        mcp_server._configured_default_worker_profile()


def test_configured_default_worker_profile_prefers_codex_when_allowed(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", raising=False)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "openclaw-general,codex-cli")

    assert mcp_server._configured_default_worker_profile() == "codex-cli"


def test_project_create_reads_default_worker_profile_at_call_time(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            await client.call_tool(
                "project_create",
                {
                    "title": "Use deployment default",
                    "goal": "Project should use the current default worker.",
                },
            )

    asyncio.run(scenario())
    assert api.create_project_payloads[-1]["default_worker_profile"] == "claude-code"


def test_default_execution_mode_prefers_glasshive_env_alias(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    assert mcp_server._default_execution_mode() == "host"


def test_worker_message_rejects_blank_worker_id_before_api_call():
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="worker_id is required"):
                await client.call_tool("worker_message", {"worker_id": " ", "message": "Steer this run."})

    asyncio.run(scenario())
    assert api.sent_messages == []
    assert "send_message" not in api.calls


def test_api_client_rejects_blank_and_path_shaped_ids_before_http():
    api = RecordingWorkersProjectsApiClient()

    with pytest.raises(ValueError, match="worker_id is required"):
        api.worker_live(" ")
    with pytest.raises(ValueError, match="worker_id must be a simple id"):
        api.get_worker("../wrk_bad")
    with pytest.raises(ValueError, match="project_id must be a simple id"):
        api.list_project_runs("prj_bad/runs")
    with pytest.raises(ValueError, match="run_id must be a simple id"):
        api.get_run("run_bad?debug=true")
    with pytest.raises(ValueError, match="schedule_id must be a simple id"):
        api.get_schedule("sch_bad#fragment")
    with pytest.raises(ValueError, match="supported worker lifecycle"):
        api.lifecycle("wrk_ok", "../terminate")

    assert api.requests == []


def test_api_client_uses_validated_ids_for_low_level_worker_paths():
    api = RecordingWorkersProjectsApiClient()

    api.assign_run("wrk_ok-1", "Do the work.")
    api.worker_schedules("wrk_ok-1", include_done=True)
    api.get_project("prj_ok.1")

    assert [request["path"] for request in api.requests] == [
        "/v1/workers/wrk_ok-1/assign",
        "/v1/workers/wrk_ok-1/schedules?include_done=true",
        "/v1/projects/prj_ok.1",
    ]


def test_api_client_request_guard_rejects_empty_or_relative_path_segments():
    api = RecordingWorkersProjectsApiClient()

    with pytest.raises(ValueError, match="invalid empty or relative segment"):
        api._request("GET", "/v1/workers//live")
    with pytest.raises(ValueError, match="invalid empty or relative segment"):
        api._request("GET", "/v1/workers/../live")
    with pytest.raises(ValueError, match="absolute local route"):
        api._request("GET", "v1/workers/wrk_1/live")

    api._request("GET", "/v1/workers/wrk_ok/schedules?include_done=true")
    assert api.requests == [
        {"method": "GET", "path": "/v1/workers/wrk_ok/schedules?include_done=true", "json_body": None}
    ]


def _tool_json(result) -> object:
    if result.structured_content is not None:
        return result.structured_content
    assert result.content, "Expected text content from MCP tool call"
    return json.loads(result.content[0].text)


def _assert_link_ref_url(url: str, *, prefix: str, kind: str) -> dict:
    assert url.startswith(prefix)
    assert "/v1/signed-links/" not in url
    assert "gh_token=" not in url
    ref_id = urlsplit(url).path.rsplit("/", 1)[-1]
    record = resolve_signed_link_ref(ref_id)
    assert record is not None
    assert record["kind"] == kind
    assert record["payload"]["kind"] == kind
    return record


def _callback_headers() -> dict[str, str]:
    return {
        "X-Viventium-User-Id": "user_public_safe",
        "X-Viventium-Conversation-Id": "conv_public_safe",
        "X-Viventium-Parent-Message-Id": "user_msg_public_safe",
        "X-Viventium-Message-Id": "assistant_msg_public_safe",
    }


def test_server_instructions_advertise_mcp_owned_usage_contract(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_HOST_MENTION_CODEX", "@codex")
    monkeypatch.setenv("WPR_HOST_MENTION_CLAUDE", "@claude")
    monkeypatch.setenv("WPR_HOST_MENTION_OPENCLAW", "@openclaw")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = server.instructions.lower()

    for phrase in [
        "one glasshive tool",
        "make one call when one call can complete it",
        "never enumerate or summarize the tool catalog",
        "check or wait only when the user asks",
        "without inventing plans, success criteria, tool results, or extra workflow",
        "exact callable tool id shown by the host",
        "real chrome/browser",
        "desktop",
        "local projects",
        "installed clis",
        "workspace_launch",
    ]:
        assert phrase in instructions
    assert "tool_search" not in instructions
    assert len(server.instructions) < 1_000


def test_server_instructions_reflect_disabled_host_workers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = server.instructions.lower()

    assert "host-native workers are disabled" in instructions
    assert "configured default 'docker'" in instructions
    assert "do not request execution_mode='host'" in instructions


def test_enterprise_mcp_http_auth_middleware_gates_transport_requests(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")

    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(mcp_server.EnterpriseMcpHttpAuthMiddleware)
    client = TestClient(app)
    good_headers = {
        "X-GlassHive-Service-Token": "service-token",
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-a",
    }

    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"X-WPR-Token": "wrong"}).status_code == 401
    assert client.post("/mcp", headers={"X-WPR-Token": "service-token"}).status_code == 401
    assert client.post(
        "/mcp",
        headers={
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
        },
    ).status_code == 401
    assert client.post("/mcp", headers={**good_headers, "X-GlassHive-Tenant-Id": "tenant-beta"}).status_code == 401
    assert client.post("/mcp", headers=good_headers).status_code == 200
    assert client.post("/mcp", headers={**good_headers, "Authorization": "Bearer service-token"}).status_code == 200


def test_local_mcp_http_auth_middleware_requires_distinct_service_credential(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "mcp-service-token")

    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(mcp_server.McpHttpAuthMiddleware)
    client = TestClient(app)

    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer provider-token"}).status_code == 401
    assert client.post("/mcp", headers={"X-WPR-Token": "mcp-service-token"}).status_code == 200
    assert client.post("/mcp", headers={"Authorization": "Bearer mcp-service-token"}).status_code == 200


def test_enterprise_mcp_requires_service_authentication(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")

    with pytest.raises(PermissionError):
        mcp_server._require_enterprise_mcp_service_auth(
            {
                "x-viventium-tenant-id": "tenant",
                "x-viventium-user-id": "forged-user",
            }
        )

    mcp_server._require_enterprise_mcp_service_auth(
        {
            "x-wpr-token": "service-token",
            "x-viventium-tenant-id": "tenant-alpha",
            "x-viventium-user-id": "user-a",
        }
    )
    mcp_server._require_enterprise_mcp_service_auth(
        {
            "x-glasshive-service-token": "service-token",
            "x-glasshive-tenant-id": "tenant-alpha",
            "x-glasshive-user-id": "user-a",
        }
    )
    mcp_server._require_enterprise_mcp_service_auth({"authorization": "Bearer service-token"})

    with pytest.raises(PermissionError, match="tenant assertion"):
        mcp_server._require_enterprise_mcp_identity_assertion(
            {
                "x-wpr-token": "service-token",
                "x-viventium-tenant-id": "tenant-beta",
                "x-viventium-user-id": "user-a",
            }
        )
    with pytest.raises(PermissionError, match="user assertion"):
        mcp_server._require_enterprise_mcp_identity_assertion(
            {
                "x-wpr-token": "service-token",
                "x-viventium-tenant-id": "tenant-alpha",
            }
        )
    mcp_server._require_enterprise_mcp_identity_assertion(
        {
            "x-wpr-token": "service-token",
            "x-viventium-tenant-id": "tenant-alpha",
            "x-viventium-user-id": "user-a",
        }
    )


def test_enterprise_owner_and_alias_accept_generic_glasshive_identity_headers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
        },
    )

    assert mcp_server._request_owner_id("spoofed-owner") == "user-a"
    assert mcp_server._request_scoped_alias("Shared Workspace") == "tenant-alpha--user-a--shared-workspace"


def test_enterprise_worker_delegate_once_rejects_before_preflight_without_service_auth(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "worker_delegate_once",
                    {
                        "title": "No auth",
                        "instruction": "This should not reach callback preflight or create work.",
                        "profile": "codex-cli",
                    },
                )

    asyncio.run(scenario())


def test_mcp_transport_security_allows_configured_public_host(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_URL", "http://glasshive.localtest.me:8877/mcp")
    server = create_mcp_server(host="127.0.0.1", port=8877, api_client=FakeApiClient())

    settings = server.settings.transport_security
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "glasshive.localtest.me:8877" in settings.allowed_hosts
    assert "glasshive.localtest.me:*" in settings.allowed_hosts


def test_mcp_transport_security_keeps_unknown_hosts_closed(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_URL", "http://glasshive.localtest.me:8877/mcp")
    server = create_mcp_server(host="127.0.0.1", port=8877, api_client=FakeApiClient())

    settings = server.settings.transport_security
    assert settings is not None
    assert "evil.example.com:8877" not in settings.allowed_hosts


def test_mcp_server_exposes_tools_and_resources(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            tools_by_name = {tool.name: tool for tool in tools}
            assert "project_create" in tool_names
            assert "workspace_launch" in tool_names
            assert "workspace_schedule" in tool_names
            assert "worker_delegate_once" in tool_names
            assert "worker_schedule" in tool_names
            assert "worker_schedules" in tool_names
            assert "worker_takeover" in tool_names
            assert "workspace_artifacts" in tool_names
            assert "workspace_artifact_download" in tool_names
            assert "worker_find_or_resume" in tool_names
            assert "workspace_preferences_get" in tool_names
            assert "workspace_preferences_set" in tool_names
            for read_only_name in (
                "workspace_preferences_get",
                "workspace_list",
                "workspace_template_list",
                "worker_accounts_list",
                "connections_list",
                "library_list",
                "workspace_capabilities_list",
                "workspace_activity",
                "worker_recurring_schedules",
                "worker_recurring_schedule_occurrences",
                "workspace_status",
                "workspace_artifacts",
            ):
                annotations = tools_by_name[read_only_name].annotations
                assert annotations is not None
                assert annotations.readOnlyHint is True
                assert annotations.destructiveHint is False

            created = await client.call_tool(
                "project_create",
                {"owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"},
            )
            created_payload = _tool_json(created)
            assert created_payload["project_id"] == "prj_new"

            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Codex Host",
                    "role": "coding",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["worker_id"] == "wrk_resumed"
            assert worker_payload["execution_mode"] == "host"
            assert worker_payload["profile"] == "codex-cli"
            assert worker_payload["backend"] == "codex-cli"

            takeover = await client.call_tool("worker_takeover", {"worker_id": "wrk_123"})
            takeover_payload = _tool_json(takeover)
            assert takeover_payload["takeover"]["supported"] is True
            assert takeover_payload["operator_url"] == (
                "http://127.0.0.1:8780/watch/wrk_123?surface=desktop&project_id=prj_123"
            )
            assert takeover_payload["watch_url"] == takeover_payload["operator_url"]
            assert takeover_payload["worker_url"] == takeover_payload["operator_url"]
            assert takeover_payload["view_url"] == takeover_payload["operator_url"]
            assert takeover_payload["direct_desktop_url"].startswith("http://127.0.0.1:")
            assert takeover_payload["runtime_takeover_url"].endswith("/ui/workers/wrk_123/view")

            live_resource = await client.read_resource("wpr://workers/wrk_123/live")
            assert live_resource[0].text is not None
            live_payload = json.loads(live_resource[0].text)
            assert live_payload["worker"]["worker_id"] == "wrk_123"

    asyncio.run(scenario())

def test_workspace_artifacts_returns_signed_download_links(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            listed = await client.call_tool("workspace_artifacts", {"worker_id": "wrk_123"})
            payload = _tool_json(listed)
            assert payload["status"] == "ok"
            assert payload["items"][0]["path"] == "index.html"
            assert all(item["path"] != ".codex/config.toml" for item in payload["items"])
            assert all(not item["path"].startswith("tmp/chrome-user-data/") for item in payload["items"])
            assert all(not item["path"].startswith("uploads/") for item in payload["items"])
            _assert_link_ref_url(
                payload["items"][0]["signed_open_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_open",
            )
            _assert_link_ref_url(
                payload["items"][0]["signed_download_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_download",
            )
            assert "127.0.0.1" not in payload["items"][0]["signed_open_url"]
            assert "127.0.0.1" not in payload["items"][0]["signed_download_url"]
            assert payload["items"][0]["default_url"] == payload["items"][0]["signed_download_url"]
            assert payload["items"][0]["default_link_kind"] == "download"
            assert payload["items"][0]["default_link_label"] == "Download file"
            assert "Use relevant signed_download_url/default_url values" in payload["next_action_guidance"]

            listed_without_downloads = await client.call_tool(
                "workspace_artifacts",
                {"worker_id": "wrk_123", "include_download_links": False},
            )
            open_default_payload = _tool_json(listed_without_downloads)
            assert open_default_payload["download_links_signed"] is False
            assert "signed_download_url" not in open_default_payload["items"][0]
            assert (
                open_default_payload["items"][0]["default_url"]
                == open_default_payload["items"][0]["signed_open_url"]
            )
            assert open_default_payload["items"][0]["default_link_kind"] == "open"
            assert open_default_payload["items"][0]["default_link_label"] == "Open GlassHive file"

            download = await client.call_tool(
                "workspace_artifact_download",
                {"worker_id": "wrk_123", "path": "index.html"},
            )
            download_payload = _tool_json(download)
            assert download_payload["status"] == "ok"
            _assert_link_ref_url(
                download_payload["signed_open_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_open",
            )
            _assert_link_ref_url(
                download_payload["signed_download_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_download",
            )
            assert download_payload["path"] == "index.html"
            assert "Use signed_download_url as the default user-facing file link" in download_payload["next_action_guidance"]

    asyncio.run(scenario())


def test_workspace_launch_uses_saved_profile_and_effort_preferences(monkeypatch):
    api = PreferenceApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            saved = await client.call_tool(
                "workspace_preferences_set",
                {"default_worker_profile": "codex-cli", "codex_reasoning_effort": "xhigh"},
            )
            assert _tool_json(saved)["default_worker_profile"] == "codex-cli"

            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a synthetic QA marker",
                    "success_criteria": "Marker exists",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(launched)
            assert payload["profile"] == "codex-cli"
            assert payload["effort"] == "xhigh"
            bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
            assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "xhigh"
            assigned = api.assign_run_payloads[-1]
            assert assigned["effort"] == "xhigh"
            assert assigned["bootstrap_bundle"]["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "xhigh"

    asyncio.run(scenario())


def test_workspace_launch_persists_owner_selected_provider_account(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a private reusable analysis workspace",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "provider_account_policy": "personal_required",
                    "provider_account_id": "acct_codex_ready",
                    "expose_diagnostics": True,
                },
            )
            assert _tool_json(launched)["status"] == "dispatched"

    asyncio.run(scenario())

    selection = api.find_or_resume_payloads[-1]["bootstrap_bundle"]["provider_account"]
    assert selection == {
        "policy": "personal_required",
        "account_id": "acct_codex_ready",
    }
    assert api.assign_run_payloads[-1]["bootstrap_bundle"]["provider_account"] == selection


def test_workspace_launch_personal_required_fails_without_ready_account(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class NoReadyAccountApi(TrackingApiClient):
        def provider_accounts(self):
            return []

    api = NoReadyAccountApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="requires a ready selected or default"):
                await client.call_tool(
                    "workspace_launch",
                    {
                        "description": "Create a private workspace",
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                        "provider_account_policy": "personal_required",
                    },
                )

    asyncio.run(scenario())
    assert api.find_or_resume_payloads == []


def test_workspace_launch_personal_preferred_falls_back_when_default_is_not_ready(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class PendingDefaultAccountApi(TrackingApiClient):
        def provider_accounts(self):
            return [
                {
                    "account_id": "acct_codex_pending",
                    "provider": "codex",
                    "display_name": "Pending Codex",
                    "status": "pending",
                    "is_default": True,
                }
            ]

    api = PendingDefaultAccountApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a private workspace with a safe fallback",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "provider_account_policy": "personal_preferred",
                    "expose_diagnostics": True,
                },
            )
            assert _tool_json(launched)["status"] == "dispatched"

    asyncio.run(scenario())
    assert api.find_or_resume_payloads[-1]["bootstrap_bundle"]["provider_account"] == {
        "policy": "personal_preferred"
    }


def test_workspace_launch_accepts_saved_codex_none_effort(monkeypatch):
    api = PreferenceApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            saved = await client.call_tool(
                "workspace_preferences_set",
                {"default_worker_profile": "codex-cli", "codex_reasoning_effort": "none"},
            )
            assert _tool_json(saved)["codex_reasoning_effort"] == "none"

            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a synthetic QA marker with no Codex reasoning effort.",
                    "success_criteria": "Marker exists",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(launched)
            assert payload["profile"] == "codex-cli"
            assert payload["effort"] == "none"
            bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
            assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "none"
            assigned = api.assign_run_payloads[-1]
            assert assigned["effort"] == "none"
            assert assigned["bootstrap_bundle"]["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "none"

    asyncio.run(scenario())


def test_workspace_launch_projects_saved_claude_max_effort(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    api = PreferenceApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            saved = await client.call_tool(
                "workspace_preferences_set",
                {"default_worker_profile": "claude-code", "claude_effort": "max"},
            )
            assert _tool_json(saved)["default_worker_profile"] == "claude-code"

            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a synthetic Claude QA marker",
                    "success_criteria": "Marker exists",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(launched)
            assert payload["profile"] == "claude-code"
            assert payload["effort"] == "max"
            bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
            assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"
            assigned = api.assign_run_payloads[-1]
            assert assigned["effort"] == "max"
            assert assigned["bootstrap_bundle"]["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"

    asyncio.run(scenario())


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_workspace_launch_projects_native_claude_effort(monkeypatch, effort):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "claude-code")
    api = PreferenceApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            saved = await client.call_tool(
                "workspace_preferences_set",
                {"default_worker_profile": "claude-code", "claude_effort": effort},
            )
            assert _tool_json(saved)["claude_effort"] == effort

            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a synthetic Claude QA marker",
                    "success_criteria": "Marker exists",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(launched)
            assert payload["effort"] == effort
            bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
            assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == effort

    asyncio.run(scenario())


def test_workspace_artifact_download_rejects_traversal_before_signing(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            for path in (
                "../runtime_phase1.db",
                "/etc/passwd",
                "outputs/../secret.txt",
                "outputs\\.git/config",
                "tmp/chrome-user-data/Default/Default/Extensions/fdpohaocaechififmbbbbbknoalclacl/8.6_0/capture/index.html",
                "uploads/source.txt.metadata.json",
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(
                        "workspace_artifact_download",
                        {"worker_id": "wrk_123", "path": path},
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", ["librechat", "glasshive"])
def test_workspace_status_returns_view_steer_link_for_web_mcp_surfaces(monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-GlassHive-Surface": surface})
    api_client = PollingApiClient(["completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_poll", "worker_id": "wrk_poll"},
            )
            payload = _tool_json(status)
            record = _assert_link_ref_url(
                payload["view_steer_url"],
                prefix="https://glasshive.example.test/r/",
                kind="worker_view",
            )
            assert "watch/wrk_poll" in record["target_url"]
            assert "gh_token=" in record["target_url"]
            assert payload["view_steer"]["include_in_response"] is True

    asyncio.run(scenario())


def test_workspace_status_preserves_truthful_close_failure_state(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")

    class CloseFailureApiClient(PollingApiClient):
        def worker_live(self, worker_id: str):
            payload = super().worker_live(worker_id)
            payload["worker"].update(
                {"state": "terminated", "close_state": "termination_failed"}
            )
            return payload

    server = create_mcp_server(api_client=CloseFailureApiClient(["cancelled"]))

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_poll", "worker_id": "wrk_poll"},
            )
            assert _tool_json(status)["worker_state"] == "termination_failed"

    asyncio.run(scenario())


def test_workspace_wait_prefers_newer_worker_run_over_stale_failed_run(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    api_client = StaleRequestedRunApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_failed",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 0,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "completed"
            assert payload["run_state"] == "completed"
            assert payload["output_text"] == "Latest artifact is ready"
            assert "run_id" not in payload
            assert "worker_id" not in payload
            assert "requested_run_id" not in payload
            assert "next_action_guidance" not in payload
            _assert_link_ref_url(
                payload["artifact_links"]["items"][0]["signed_open_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_open",
            )
            _assert_link_ref_url(
                payload["artifact_links"]["items"][0]["signed_download_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_download",
            )
            assert (
                payload["artifact_links"]["items"][0]["default_url"]
                == payload["artifact_links"]["items"][0]["signed_download_url"]
            )

            diagnostics = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_failed",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 0,
                    "include_diagnostics": True,
                },
            )
            diagnostic_payload = _tool_json(diagnostics)
            assert diagnostic_payload["requested_run_stale"] is True
            assert diagnostic_payload["requested_run_id"] == "run_old_failed"
            assert diagnostic_payload["requested_run_state"] == "failed"
            assert diagnostic_payload["run_id"] == "run_new_completed"
            assert diagnostic_payload["latest_run_id"] == "run_new_completed"

    asyncio.run(scenario())


def test_workspace_wait_refetches_run_after_live_heals_completion(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    api_client = HealDuringStatusApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_heal",
                    "worker_id": "wrk_heal",
                    "timeout_seconds": 0,
                    "include_diagnostics": True,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "completed"
            assert payload["terminal"] is True
            assert payload["run_state"] == "completed"
            assert payload["output_text"] == "Recovered result is ready"
            assert payload["requested_run_state"] == "completed"
            assert api_client.get_run_calls >= 2

    asyncio.run(scenario())


def test_workspace_wait_compacts_artifact_links_unless_diagnostics(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=ManyArtifactsApiClient())

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_123",
                    "worker_id": "wrk_123",
                    "timeout_seconds": 0,
                },
            )
            payload = _tool_json(waited)
            links = payload["artifact_links"]
            assert payload["status"] == "completed"
            assert links["count"] == 7
            assert links["visible_item_count"] == 5
            assert links["remaining_item_count"] == 2
            assert links["truncated"] is True
            assert links["full_inventory_available"] is True
            assert links["full_inventory_tool"] == "workspace_artifacts"
            assert len(links["items"]) == 5
            assert "worker_id" not in links
            assert "next_action_guidance" not in links
            _assert_link_ref_url(
                links["items"][0]["signed_download_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_download",
            )
            assert links["items"][0]["default_url"] == links["items"][0]["signed_download_url"]

            diagnostics = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_123",
                    "worker_id": "wrk_123",
                    "timeout_seconds": 0,
                    "include_diagnostics": True,
                },
            )
            diagnostic_payload = _tool_json(diagnostics)
            diagnostic_links = diagnostic_payload["artifact_links"]
            assert len(diagnostic_links["items"]) == 7
            assert diagnostic_links["worker_id"] == "wrk_123"
            assert "next_action_guidance" in diagnostic_links

            listed = await client.call_tool("workspace_artifacts", {"worker_id": "wrk_123"})
            list_payload = _tool_json(listed)
            assert len(list_payload["items"]) == 7
            assert "next_action_guidance" in list_payload

    asyncio.run(scenario())


def test_workspace_wait_compact_links_prioritize_output_referenced_deliverables(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=OutputReferencedManyArtifactsApiClient())

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_123",
                    "worker_id": "wrk_123",
                    "timeout_seconds": 0,
                },
            )
            payload = _tool_json(waited)
            paths = [item["path"] for item in payload["artifact_links"]["items"]]
            assert paths[:2] == [
                "report/example_client_midtown_3bed_report.pdf",
                "report/example_client_midtown_3bed_report.html",
            ]
            for item in payload["artifact_links"]["items"][:2]:
                _assert_link_ref_url(
                    item["signed_download_url"],
                    prefix="https://glasshive.example.test/v1/link-refs/",
                    kind="artifact_download",
                )

    asyncio.run(scenario())


def test_workspace_status_failed_run_surfaces_partial_artifact_links(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=RetryableFailureApiClient())

    async def scenario():
        async with Client(server) as client:
            checked = await client.call_tool(
                "workspace_status",
                {
                    "run_id": "run_retryable_failed",
                    "worker_id": "wrk_retry",
                    "include_live": False,
                },
            )
            payload = _tool_json(checked)
            assert payload["terminal"] is True
            assert payload["run_state"] == "failed"
            assert payload["failure_class"] == "provider_rate_limited"
            _assert_link_ref_url(
                payload["artifact_links"]["items"][0]["signed_open_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_open",
            )
            _assert_link_ref_url(
                payload["artifact_links"]["items"][0]["signed_download_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_download",
            )
            assert payload["artifact_links"]["items"][0]["default_link_kind"] == "download"
            assert "next_action_guidance" not in payload
            assert "next_action_guidance" not in payload["artifact_links"]

    asyncio.run(scenario())


def test_workspace_wait_compares_mixed_timezone_run_timestamps(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=MixedTimezoneRunOrderApiClient())

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_failed",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 0,
                    "include_diagnostics": True,
                },
            )
            payload = _tool_json(waited)
            assert payload["requested_run_stale"] is True
            assert payload["run_id"] == "run_new_completed"
            assert payload["output_text"] == "Newer mixed-timezone artifact is ready"

    asyncio.run(scenario())


def test_workspace_status_does_not_mark_requested_run_stale_when_last_run_id_is_older(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=OlderLastRunApiClient())

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_new_completed", "worker_id": "wrk_stale", "include_diagnostics": True},
            )
            payload = _tool_json(status)
            assert payload["requested_run_stale"] is False
            assert payload["run_id"] == "run_new_completed"
            assert payload["latest_run_id"] == "run_new_completed"
            assert payload["run_state"] == "completed"
            assert payload["output_text"] == "Requested run is the newest result"

    asyncio.run(scenario())


def test_workspace_wait_follows_newer_running_run_instead_of_stale_terminal_requested_run(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=TerminalRequestedNewerRunningApiClient())

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_failed",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 1,
                    "include_diagnostics": True,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "still_running"
            assert payload["timed_out"] is True
            assert payload["attempts"] == 1
            assert payload["requested_run_stale"] is True
            assert payload["run_id"] == "run_new_running"
            assert payload["run_state"] == "running"
            assert payload["latest_run_id"] == "run_new_running"
            assert payload["latest_run_state"] == "running"
            assert payload["artifact_links"] is None
            assert payload["recommended_next_tool"] == "workspace_wait"

    asyncio.run(scenario())


def test_workspace_status_follows_active_resume_after_interrupted_requested_run(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=InterruptedRequestedResumeApiClient(["running"]))

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {
                    "run_id": "run_old_interrupted",
                    "worker_id": "wrk_stale",
                    "include_diagnostics": True,
                },
            )
            payload = _tool_json(status)
            assert payload["terminal"] is False
            assert payload["requested_run_stale"] is True
            assert payload["requested_run_state"] == "interrupted"
            assert payload["run_id"] == "run_resume"
            assert payload["run_state"] == "running"
            assert payload["latest_run_id"] == "run_resume"
            assert payload["latest_run_state"] == "running"

    asyncio.run(scenario())


def test_workspace_wait_continues_through_interrupted_requested_run_resume(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_POLL_INTERVAL_SEC", "1")
    server = create_mcp_server(api_client=InterruptedRequestedResumeApiClient(["running", "completed"]))

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_interrupted",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 2.5,
                    "poll_interval_seconds": 1,
                    "include_diagnostics": True,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "completed"
            assert payload["timed_out"] is False
            assert payload["attempts"] == 2
            assert payload["requested_run_stale"] is True
            assert payload["requested_run_id"] == "run_old_interrupted"
            assert payload["requested_run_state"] == "interrupted"
            assert payload["run_id"] == "run_resume"
            assert payload["run_state"] == "completed"
            assert payload["output_text"] == "Resumed result is ready"
            assert payload["artifact_links"]["items"][0]["default_link_kind"] == "download"

    asyncio.run(scenario())


def test_enterprise_view_steer_url_does_not_fall_back_to_unsigned(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)

    url = mcp_server._signed_view_steer_url(
        {
            "worker_id": "wrk_local",
            "project_id": "prj_local",
            "tenant_id": "local",
            "owner_id": "owner-a",
        },
        "prj_local",
        "librechat",
    )

    assert url is None


def test_mcp_never_mints_new_view_or_artifact_refs_for_terminated_worker(
    tmp_path,
    monkeypatch,
):
    link_ref_state = tmp_path / "link-refs.sqlite3"
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(link_ref_state))
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    worker = {
        "worker_id": "wrk_terminated",
        "project_id": "prj_terminated",
        "tenant_id": "local",
        "owner_id": "owner-a",
        "state": "terminated",
    }

    assert mcp_server._signed_view_steer_url(worker, worker["project_id"], "librechat") is None
    assert mcp_server._signed_artifact_open_url(worker, "outputs/report.txt") is None
    assert mcp_server._signed_artifact_download_url(worker, "outputs/report.txt") is None
    assert not link_ref_state.exists()


def test_enterprise_mcp_refuses_non_http_transport(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    with pytest.raises(RuntimeError, match="streamable-http"):
        mcp_server._require_enterprise_mcp_transport("stdio")
    with pytest.raises(RuntimeError, match="streamable-http"):
        mcp_server._require_enterprise_mcp_transport("sse")
    mcp_server._require_enterprise_mcp_transport("streamable-http")


@pytest.mark.parametrize("surface", ["telegram", "voice", "unknown-surface"])
def test_worker_takeover_omits_operator_url_for_non_web_surface(monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": surface})
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            takeover = await client.call_tool("worker_takeover", {"worker_id": "wrk_123"})
            payload = _tool_json(takeover)
            assert payload["operator_url"] is None
            assert payload["watch_url"] is None
            assert payload["operator_url_available"] is False
            assert payload["operator_url_surface"] == surface
            assert payload["view_url"] is None
            assert payload["runtime_takeover_url"] is None
            assert payload["direct_desktop_url"] is None
            assert payload["terminal_url"] is None
            assert payload["worker_url"] is None
            assert payload["takeover"]["url_available"] is False
            assert "url" not in payload["takeover"]
            serialized = json.dumps(payload)
            assert "127.0.0.1" not in serialized
            assert "localhost" not in serialized
            assert "noVNC" not in serialized

    asyncio.run(scenario())


def test_worker_delegate_once_creates_resumes_and_runs_without_listing(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    _patch_host_runtime_requirements_ok(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host Page Title QA",
                    "goal": "Open a local page and report the title.",
                    "instruction": "Open the local QA page and reply with the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Page Title QA\n\nReport the page title.\n",
                        }
                    },
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["callback_ready"] is True
            assert payload["callback_delivery_deadline_seconds"] == 870
            record = _assert_link_ref_url(
                payload["view_steer_url"],
                prefix="http://127.0.0.1:8780/r/",
                kind="worker_view",
            )
            assert "watch/wrk_resumed" in record["target_url"]
            assert "gh_token=" in record["target_url"]
            assert payload["view_steer"]["include_in_response"] is True
            assert payload["view_steer"]["url"] == payload["view_steer_url"]
            assert payload["view_steer"]["label"] == "View / Steer Host Page Title QA"
            assert payload["view_steer"]["link_kind"] == "mission_control"
            assert payload["view_steer"]["state"] == "nonterminal"
            assert payload["result_tools"]["status"] == "workspace_status"
            assert payload["result_tools"]["wait"] == "workspace_wait"
            assert payload["completion_wait_timeout_seconds"] == 45
            assert "do not ask the user to say 'keep waiting'" in payload["completion_polling_guidance"]
            assert "user_status" not in payload
            assert "pre_wait_user_update" not in payload
            assert "follow_up_context" not in payload
            assert "acknowledgement_guidance" not in payload
            assert "main_agent_next_action" not in payload
            assert "delegation_audit" not in payload
            assert "project_id" not in payload
            assert "worker_id" not in payload
            assert "run_id" not in payload
            assert "alias" not in payload

    asyncio.run(scenario())

    assigned_instruction = api_client.assign_run_payloads[-1]["instruction"]
    assert "Open the local QA page and reply with the page title." in assigned_instruction
    assert "host-side responsibilities" in assigned_instruction
    assert "do not mark the workspace blocked" in assigned_instruction
    assert "report only blockers observable from inside this worker workspace" in assigned_instruction
    assert assigned_instruction.count("Host-side GlassHive orchestration checks") == 1


def test_worker_delegate_once_uses_existing_atomic_delegation_receipt(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "owner-alpha",
            "X-Viventium-Message-Id": "message-alpha",
            "X-Viventium-Surface": "telegram",
        },
    )

    class AtomicApi(TrackingApiClient):
        def __init__(self):
            super().__init__()
            self.delegations = []

        def create_delegation(self, **kwargs):
            self.calls.append("create_delegation")
            self.delegations.append(kwargs)
            return {
                "workRef": "work_atomic_1",
                "state": "queued",
                "viewRef": "/r/ghr_atomic_1",
                "resourceClass": "light",
                "idempotentReplay": False,
            }

    api = AtomicApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            delegated = _tool_json(
                await client.call_tool(
                    "worker_delegate_once",
                    {
                        "title": "Atomic mission",
                        "goal": "Finish the mission",
                        "instruction": "Create the requested HTML artifact.",
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                        "resource_class": "light",
                    },
                )
            )
        assert delegated["status"] == "dispatched"
        assert delegated["work_ref"] == "work_atomic_1"
        assert delegated["resource_class"] == "light"

    asyncio.run(scenario())

    assert api.calls == ["create_delegation"]
    assert api.delegations[0]["tenant_id"] == "tenant-alpha"
    assert api.delegations[0]["owner_id"] == "owner-alpha"
    assert api.delegations[0]["payload"]["originSurface"] == "telegram"
    assert api.delegations[0]["idempotency_key"].startswith("ghd_")


def test_worker_delegate_once_blocks_missing_host_cli_before_api_calls(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/tmp/glasshive-missing-codex")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Unavailable host Codex",
                    "instruction": "Run a host Codex task.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "blocked"
            assert payload["failure_class"] == "runtime_dependency_missing"
            assert payload["failure_retryable"] is False
            assert "did not start" in payload["acknowledgement_guidance"]
            assert payload["view_steer_url"] is None
            assert payload["view_steer"]["include_in_acknowledgement"] is False

    asyncio.run(scenario())
    assert api_client.calls == []


def test_worker_delegate_once_defers_host_version_check_to_reserved_service(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(
        mcp_server,
        "host_runtime_requirement_issue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transport must not execute host CLI version checks")
        ),
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Recover to sandbox",
                    "instruction": "Use GlassHive to complete a simple public-safe task.",
                    "profile": "codex-cli",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["follow_up_context"]["run_id"] == "run_assign"
            assert payload["execution_mode"] == "host"
            assert "runtime_recovery" not in payload

    asyncio.run(scenario())
    assert api_client.find_or_resume_payloads[-1]["execution_mode"] == "host"
    assert api_client.assign_run_payloads[-1]["worker_id"] == "wrk_resumed"


def test_workspace_launch_recovers_explicit_host_when_host_workers_disabled(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a public-safe market scan.",
                    "success_criteria": "The report is created in the workspace.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(launched)
            assert payload["status"] == "dispatched"
            assert payload["execution_mode"] == "docker"
            assert payload["runtime_recovery"]["from_execution_mode"] == "host"
            assert payload["runtime_recovery"]["to_execution_mode"] == "docker"

    asyncio.run(scenario())
    assert api_client.find_or_resume_payloads[-1]["execution_mode"] == "docker"
    assert api_client.assign_run_payloads[-1]["worker_id"] == "wrk_resumed"


def test_worker_delegate_once_defers_explicit_host_version_check_to_service(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(
        mcp_server,
        "host_runtime_requirement_issue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transport must not execute host CLI version checks")
        ),
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Explicit host stays blocked",
                    "instruction": "Run a host Codex task.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())
    assert api_client.find_or_resume_payloads[-1]["execution_mode"] == "host"
    assert api_client.assign_run_payloads[-1]["worker_id"] == "wrk_resumed"


def test_workspace_schedule_defers_host_version_check_to_reserved_service(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(
        mcp_server,
        "host_runtime_requirement_issue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transport must not execute host CLI version checks")
        ),
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Run later on host",
                    "success_criteria": "The scheduled host run is accepted only if the runtime is available.",
                    "schedule_text": "in 20 minutes",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(scheduled)
            assert payload["status"] == "scheduled"

    asyncio.run(scenario())
    assert api_client.find_or_resume_payloads[-1]["execution_mode"] == "host"
    assert api_client.schedule_run_payloads[-1]["schedule_text"] == "in 20 minutes"


def test_workspace_schedule_default_host_does_not_run_transport_cli_check(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(
        mcp_server,
        "host_runtime_requirement_issue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transport must not execute host CLI version checks")
        ),
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Run later using the available runtime",
                    "success_criteria": "The scheduled run is accepted using safe recovery.",
                    "schedule_text": "in 20 minutes",
                    "profile": "codex-cli",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(scheduled)
            assert payload["status"] == "scheduled"
            assert payload["execution_mode"] == "host"
            assert "runtime_recovery" not in payload

    asyncio.run(scenario())
    assert api_client.find_or_resume_payloads[-1]["execution_mode"] == "host"
    assert api_client.schedule_run_payloads[-1]["schedule_text"] == "in 20 minutes"


def test_worker_schedule_defers_host_version_check_to_reserved_service(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(
        mcp_server,
        "host_runtime_requirement_issue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transport must not execute host CLI version checks")
        ),
    )

    class HostWorkerApiClient(TrackingApiClient):
        def get_worker(self, worker_id: str):
            payload = super().get_worker(worker_id)
            payload.update({"profile": "codex-cli", "execution_mode": "host"})
            return payload

    api_client = HostWorkerApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "worker_schedule",
                {
                    "worker_id": "wrk_host",
                    "instruction": "Run later on host.",
                    "schedule_text": "in 20 minutes",
                },
            )
            payload = _tool_json(scheduled)
            assert payload["state"] == "pending"
            assert payload["worker_id"] == "wrk_host"

    asyncio.run(scenario())
    assert api_client.schedule_run_payloads[-1]["schedule_text"] == "in 20 minutes"


def test_worker_schedule_and_workspace_schedule_are_glasshive_native(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "worker_schedule",
                {
                    "worker_id": "wrk_123",
                    "instruction": "Create scheduled-proof.txt",
                    "schedule_text": "in 20 minutes",
                },
            )
            scheduled_payload = _tool_json(scheduled)
            assert scheduled_payload["schedule_id"] == "sch_123"
            assert scheduled_payload["schedule_text"] == "in 20 minutes"

            workspace = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Check the workspace later",
                    "success_criteria": "A scheduled run is accepted",
                    "schedule_text": "in 20 minutes",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "expose_diagnostics": True,
                },
            )
            workspace_payload = _tool_json(workspace)
            assert workspace_payload["status"] == "scheduled"
            assert workspace_payload["schedule_id"] == "sch_123"
            assert workspace_payload["callback_ready"] is True

    asyncio.run(scenario())
    assert api_client.calls == ["create_project", "find_or_resume_worker"]
    assert api_client.find_or_resume_payloads[0]["start_synchronously"] is False
    assert api_client.find_or_resume_payloads[0]["workspace_kind"] == "named"


def test_worker_delegate_once_exposes_diagnostics_only_when_requested(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    _patch_host_runtime_requirements_ok(monkeypatch)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    server = create_mcp_server(api_client=TrackingApiClient())

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Diagnostic host task",
                    "instruction": "Run a diagnostic host task.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(delegated)
            assert payload["project_id"] == "prj_new"
            assert payload["worker_id"] == "wrk_resumed"
            assert payload["run_id"] == "run_assign"
            assert payload["execution_mode"] == "host"
            assert payload["alias"].startswith("codex-cli-diagnostic-host-task-")
            assert payload["alias"] != "codex-cli-diagnostic-host-task"
            assert payload["submitted_instruction"].startswith("Run a diagnostic host task.")
            assert "host-side responsibilities" in payload["submitted_instruction"]
            assert "report only blockers observable from inside this worker workspace" in payload["submitted_instruction"]

    asyncio.run(scenario())


def test_worker_delegate_once_dispatches_without_callback_by_default(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    _patch_host_runtime_requirements_ok(monkeypatch)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host Page Title QA",
                    "instruction": "Open the local QA page and reply with the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["callback_ready"] is False
            assert payload["callback_delivery"] == "not_configured_standalone_polling_available"
            assert payload["result_tools"]["status"] == "workspace_status"
            assert payload["result_tools"]["wait"] == "workspace_wait"
            assert "follow_up_context" not in payload
            assert "user_status" not in payload
            assert "acknowledgement_guidance" not in payload
            assert "main_agent_next_action" not in payload
            assert payload["missing_callback_fields"] == []

    asyncio.run(scenario())
    assert api_client.calls == ["create_project", "find_or_resume_worker", "assign_run"]


def test_worker_delegate_once_can_require_callback_for_host_apps(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    _patch_host_runtime_requirements_ok(monkeypatch)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host callback-required QA",
                    "instruction": "Only dispatch when host callback context is present.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "require_callback": True,
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "blocked"
            assert payload["callback_ready"] is False
            assert "conversation_id" in payload["missing_callback_fields"]

    asyncio.run(scenario())
    assert api_client.calls == []


def test_workspace_status_and_wait_are_standalone_mcp_followup_tools(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_POLL_INTERVAL_SEC", "1")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = PollingApiClient(["queued", "running", "completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_poll", "worker_id": "wrk_poll"},
            )
            status_payload = _tool_json(status)
            assert status_payload["mode"] == "non_blocking"
            assert status_payload["terminal"] is False
            assert status_payload["run_state"] == "queued"
            assert status_payload["worker_state"] == "ready"
            assert status_payload["view_steer_url"].startswith(
                "http://127.0.0.1:8780/watch/wrk_poll?surface=desktop&project_id=prj_poll"
            )

            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 1,
                    "poll_interval_seconds": 0.01,
                },
            )
            waited_payload = _tool_json(waited)
            assert waited_payload["mode"] == "blocking_wait"
            assert waited_payload["status"] == "completed"
            assert waited_payload["terminal"] is True
            assert waited_payload["output_text"] == "Poll completed"
            assert waited_payload["attempts"] == 2

    asyncio.run(scenario())
    assert api_client.get_run_calls == 3


def test_workspace_wait_enforces_configured_poll_interval_floor(monkeypatch):
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_POLL_INTERVAL_SEC", "7")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(mcp_server.asyncio, "sleep", fake_sleep)
    api_client = PollingApiClient(["running", "completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            waited_payload = _tool_json(waited)
            assert waited_payload["status"] == "completed"
            assert waited_payload["attempts"] == 2

    asyncio.run(scenario())
    assert sleep_calls == [7.0]


def test_workspace_wait_default_polling_backs_off_without_host_parameters():
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=1, base_interval=5.0, adaptive=True) == 5.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=7, base_interval=5.0, adaptive=True) == 10.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=13, base_interval=5.0, adaptive=True) == 20.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=19, base_interval=5.0, adaptive=True) == 30.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=19, base_interval=5.0, adaptive=False) == 5.0


def test_workspace_wait_rejects_non_finite_timing_inputs(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="poll_interval_seconds must be a finite number"):
                await client.call_tool(
                    "workspace_wait",
                    {
                        "run_id": "run_poll",
                        "worker_id": "wrk_poll",
                        "timeout_seconds": 0,
                        "poll_interval_seconds": "NaN",
                    },
                )

            with pytest.raises(ToolError, match="timeout_seconds must be a finite number"):
                await client.call_tool(
                    "workspace_wait",
                    {
                        "run_id": "run_poll",
                        "worker_id": "wrk_poll",
                        "timeout_seconds": "Infinity",
                    },
                )

            with pytest.raises(ToolError, match="poll_interval_seconds must be greater than 0"):
                await client.call_tool(
                    "workspace_wait",
                    {
                        "run_id": "run_poll",
                        "worker_id": "wrk_poll",
                        "timeout_seconds": 0,
                        "poll_interval_seconds": 0,
                    },
                )

    asyncio.run(scenario())


def test_workspace_wait_resolves_same_conversation_recent_launch_when_ids_are_omitted(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Surface": "web",
            "X-Viventium-User-Id": "qa-user",
            "X-Viventium-Conversation-Id": "conv-recent-dispatch",
        },
    )
    api_client = RememberedDispatchApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a public-safe marker file.",
                    "success_criteria": "The marker file exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                },
            )
            launch_payload = _tool_json(launched)
            assert launch_payload["status"] == "dispatched"
            assert launch_payload["result_tools"]["wait"] == "workspace_wait"
            assert "follow_up_context" not in launch_payload

            waited = await client.call_tool(
                "workspace_wait",
                {
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0.01,
                },
            )
            waited_payload = _tool_json(waited)
            assert waited_payload["status"] == "completed"
            assert waited_payload["output_text"] == "remembered dispatch completed"
            assert "run_id" not in waited_payload
            assert "worker_id" not in waited_payload
            assert "resolved_from_recent_dispatch" not in waited_payload

            status = await client.call_tool("workspace_status", {})
            status_payload = _tool_json(status)
            assert status_payload["output_text"] == "remembered dispatch completed"
            assert "run_id" not in status_payload

            diagnostic_status = await client.call_tool("workspace_status", {"include_diagnostics": True})
            diagnostic_payload = _tool_json(diagnostic_status)
            assert diagnostic_payload["run_id"] == "run_assign"
            assert diagnostic_payload["worker_id"] == "wrk_resumed"
            assert diagnostic_payload["resolved_from_recent_dispatch"] is True

    asyncio.run(scenario())


def test_enterprise_launch_without_conversation_id_is_not_remembered(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_MCP_DIAGNOSTIC_PAYLOADS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = RememberedDispatchApiClient(tenant_id="tenant-alpha")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Enterprise scoped marker.",
                    "success_criteria": "The marker exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "expose_diagnostics": True,
                },
            )
            launch_payload = _tool_json(launched)
            assert launch_payload["status"] == "dispatched"

            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_wait", {"timeout_seconds": 0})

            explicit_status = await client.call_tool(
                "workspace_status",
                {
                    "run_id": launch_payload["follow_up_context"]["run_id"],
                    "worker_id": launch_payload["follow_up_context"]["worker_id"],
                    "include_diagnostics": True,
                },
            )
            assert _tool_json(explicit_status)["run_id"] == "run_assign"

    asyncio.run(scenario())


def test_enterprise_launch_without_conversation_id_returns_explicit_follow_up_ids(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "codex",
        },
    )
    api_client = RememberedDispatchApiClient(tenant_id="tenant-alpha")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Standalone enterprise marker.",
                    "success_criteria": "The marker exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                },
            )
            launch_payload = _tool_json(launched)
            assert launch_payload["status"] == "dispatched"
            assert launch_payload["follow_up_context"] == {
                "project_id": "prj_new",
                "worker_id": "wrk_resumed",
                "run_id": "run_assign",
                "run_state": "completed",
                "status_tool": "workspace_status",
                "blocking_wait_tool": "workspace_wait",
                "completion_wait_timeout_seconds": 45,
                "live_tool": "worker_live",
                "takeover_tool": "worker_takeover",
                "artifact_tool": "workspace_artifacts",
                "artifact_download_tool": "workspace_artifact_download",
            }
            assert "submitted_instruction" not in launch_payload
            assert "delegation_audit" not in launch_payload

            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": launch_payload["follow_up_context"]["run_id"],
                    "worker_id": launch_payload["follow_up_context"]["worker_id"],
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0.01,
                },
            )
            assert _tool_json(waited)["status"] == "completed"

    asyncio.run(scenario())


def test_enterprise_diagnostic_payloads_are_suppressed_without_opt_in(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Conversation-Id": "conv-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = RememberedDispatchApiClient(tenant_id="tenant-alpha")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Enterprise compact marker.",
                    "success_criteria": "The marker exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "expose_diagnostics": True,
                },
            )
            launch_payload = _tool_json(launched)
            assert launch_payload["status"] == "dispatched"
            assert "follow_up_context" not in launch_payload
            assert "worker_id" not in launch_payload
            assert "run_id" not in launch_payload

            status = await client.call_tool(
                "workspace_status",
                {
                    "run_id": "run_assign",
                    "worker_id": "wrk_resumed",
                    "include_diagnostics": True,
                },
            )
            status_payload = _tool_json(status)
            assert status_payload["status"] == "ok"
            assert status_payload["terminal"] is True
            assert "run_id" not in status_payload
            assert "worker_id" not in status_payload
            assert "worker_live" not in status_payload

    asyncio.run(scenario())


def test_enterprise_recent_launch_fallback_is_scoped_by_user_and_conversation(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_MCP_DIAGNOSTIC_PAYLOADS_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    current_headers = {
        "X-GlassHive-Service-Token": "service-token",
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-a",
        "X-GlassHive-Conversation-Id": "conv-a",
        "X-GlassHive-Surface": "web",
    }
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: current_headers)
    api_client = RememberedDispatchApiClient(tenant_id="tenant-alpha")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        nonlocal current_headers
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Enterprise scoped marker.",
                    "success_criteria": "The marker exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                },
            )
            assert _tool_json(launched)["status"] == "dispatched"

            same_user = await client.call_tool(
                "workspace_wait",
                {"timeout_seconds": 0, "poll_interval_seconds": 0.01, "include_diagnostics": True},
            )
            assert _tool_json(same_user)["resolved_from_recent_dispatch"] is True

            current_headers = {
                **current_headers,
                "X-GlassHive-User-Id": "user-b",
            }
            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_wait", {"timeout_seconds": 0})

            current_headers = {
                **current_headers,
                "X-GlassHive-User-Id": "user-a",
                "X-GlassHive-Conversation-Id": "conv-b",
            }
            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_status", {})

            current_headers = {
                key: value
                for key, value in current_headers.items()
                if key != "X-GlassHive-Conversation-Id"
            }
            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_wait", {"timeout_seconds": 0})

    asyncio.run(scenario())


def test_workspace_status_surfaces_retryable_failure_metadata(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    server = create_mcp_server(api_client=RetryableFailureApiClient())

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_retryable_failed", "worker_id": "wrk_retry"},
            )
            payload = _tool_json(status)
            assert payload["terminal"] is True
            assert payload["run_state"] == "failed"
            assert payload["failure_class"] == "provider_rate_limited"
            assert payload["failure_retryable"] is True
            assert "rate-limited" in payload["failure_user_message"]
            assert "workspace_continue" in payload["failure_recommended_recovery"]
            assert "next_action_guidance" not in payload

    asyncio.run(scenario())


def test_workspace_continue_queues_same_workspace_recovery(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = RetryableFailureApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            continued = await client.call_tool(
                "workspace_continue",
                {
                    "run_id": "run_retryable_failed",
                    "continuation_goal": "Continue and finish the workbook from current partial files.",
                    "effort": "medium",
                },
            )
            payload = _tool_json(continued)
            assert payload["status"] == "queued"
            assert payload["previous_failure_class"] == "provider_rate_limited"
            assert payload["effort"] == "medium"
            assert "previous_run_id" not in payload
            assert "run" not in payload
            assert "continuation_instruction_preview" not in payload
            assert payload["result_tools"]["status"] == "workspace_status"
            assert payload["view_steer_url"].startswith("http://127.0.0.1:8780/watch/wrk_retry")

    asyncio.run(scenario())
    assert len(api_client.assigned) == 1
    instruction = api_client.assigned[0]["instruction"]
    assert "Prior run task context:" in instruction
    assert "Build the requested research workbook and report." in instruction
    assert "Previous failure classification:" in instruction
    assert "provider_rate_limited" in instruction
    assert "current files" in instruction
    assert api_client.assigned[0]["effort"] == "medium"
    assert api_client.assigned[0]["continuation_context"] == {
        "version": 1,
        "base_instruction": "Build the requested research workbook and report.",
        "guidance": [
            "Continue and finish the workbook from current partial files."
        ],
    }


def test_workspace_continue_exposes_raw_details_only_with_diagnostics(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = RetryableFailureApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            continued = await client.call_tool(
                "workspace_continue",
                {
                    "run_id": "run_retryable_failed",
                    "continuation_goal": "Continue and finish the workbook from current partial files.",
                    "include_diagnostics": True,
                },
            )
            payload = _tool_json(continued)
            assert payload["previous_run_id"] == "run_retryable_failed"
            assert payload["run"]["run_id"] == "run_continued"
            assert "Prior run task context:" in payload["continuation_instruction_preview"]

    asyncio.run(scenario())


def test_workspace_continue_does_not_nest_previous_continue_wrappers(monkeypatch):
    class NestedContinuationApiClient(RetryableFailureApiClient):
        def get_run(self, run_id: str):
            payload = super().get_run(run_id)
            if run_id == "run_retryable_failed":
                payload["instruction"] = (
                    "Continue this GlassHive workspace from its current files, browser state, notes, and partial outputs.\n\n"
                    "Preserve the original user request, success criteria, response format, and any files already available in the workspace.\n\n"
                    "Original task:\nBuild the requested research workbook and report.\n\n"
                    "Previous failure classification:\n- class: provider_response_failed\n"
                    "- retryable: True\n\n"
                    "Continuation request:\nResume the original task from current files."
                )
                payload["continuation_context_json"] = json.dumps(
                    {
                        "version": 1,
                        "base_instruction": (
                            "Build the requested research workbook and report."
                        ),
                        "guidance": [
                            "Resume the original task from current files."
                        ],
                    }
                )
            return payload

    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = NestedContinuationApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            await client.call_tool(
                "workspace_continue",
                {
                    "run_id": "run_retryable_failed",
                    "continuation_goal": "Finish from the existing files.",
                },
            )

    asyncio.run(scenario())
    instruction = api_client.assigned[0]["instruction"]
    assert instruction.count("Prior run task context:") == 1
    assert instruction.count("Continuation context:") == 1
    assert "Build the requested research workbook and report." in instruction
    assert "Resume the original task from current files." in instruction
    assert "Finish from the existing files." in instruction
    assert api_client.assigned[0]["continuation_context"] == {
        "version": 1,
        "base_instruction": "Build the requested research workbook and report.",
        "guidance": [
            "Resume the original task from current files.",
            "Finish from the existing files.",
        ],
    }


def test_workspace_continue_rejects_mismatched_worker_id(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = RetryableFailureApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="worker_id must match"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed", "worker_id": "wrk_other"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_workspace_continue_rejects_active_previous_run(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = EnterpriseRetryableFailureApiClient(previous_state="running")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="only for terminal"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_enterprise_workspace_continue_rechecks_tenant_and_owner_scope(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(worker_owner_id="user-b")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_enterprise_workspace_continue_rejects_cross_tenant_previous_run(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(run_tenant_id="tenant-beta")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated tenant"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_enterprise_workspace_status_rechecks_tenant_and_owner_scope(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(worker_owner_id="user-b")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool(
                    "workspace_status",
                    {"run_id": "run_retryable_failed", "worker_id": "wrk_retry"},
                )

    asyncio.run(scenario())


def test_enterprise_workspace_artifacts_rechecks_owner_scope(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(worker_owner_id="user-b")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool("workspace_artifacts", {"worker_id": "wrk_retry"})
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool(
                    "workspace_artifact_download",
                    {"worker_id": "wrk_retry", "path": "index.html"},
                )

    asyncio.run(scenario())


def test_workspace_continue_rejects_previous_run_without_worker_scope(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = RetryableFailureApiClient()

    def get_run_without_worker(run_id: str):
        payload = RetryableFailureApiClient.get_run(api_client, run_id)
        if run_id == "run_retryable_failed":
            payload["worker_id"] = ""
        return payload

    api_client.get_run = get_run_without_worker  # type: ignore[method-assign]
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="previous run to include a worker_id"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed", "worker_id": "wrk_retry"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_workspace_wait_returns_timeout_without_callback(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "still_running"
            assert payload["timed_out"] is True
            assert payload["terminal"] is False
            assert payload["wait_again_recommended"] is True
            assert payload["completion_still_pending"] is True
            assert payload["do_not_ask_user_to_keep_waiting"] is True
            assert payload["recommended_next_tool"] == "workspace_wait"
            assert "Do not ask the user to say 'keep waiting'" in payload["recommended_next_action"]
            assert "next_action_guidance" not in payload

    asyncio.run(scenario())
    assert api_client.get_run_calls == 1


def test_workspace_wait_progress_timeout_does_not_block_result(monkeypatch):
    class HangingProgressContext:
        async def report_progress(self, **_kwargs):
            await asyncio.Event().wait()

    class CancellationResistantProgressContext:
        async def report_progress(self, **_kwargs):
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(60)

    monkeypatch.setenv("WPR_MCP_PROGRESS_NOTIFY_TIMEOUT_SEC", "0.001")

    async def scenario():
        await asyncio.wait_for(
            mcp_server._report_workspace_wait_progress(
                HangingProgressContext(),
                elapsed_seconds=1.0,
                timeout_seconds=2.0,
                attempts=1,
                status="still_running",
                run_state="running",
            ),
            timeout=0.5,
        )
        await asyncio.wait_for(
            mcp_server._report_workspace_wait_progress(
                CancellationResistantProgressContext(),
                elapsed_seconds=1.0,
                timeout_seconds=2.0,
                attempts=1,
                status="still_running",
                run_state="running",
            ),
            timeout=0.5,
        )

    asyncio.run(scenario())


def test_workspace_wait_returns_deliverable_ready_when_artifacts_exist(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_DELIVERABLE_READY_QUIET_SEC", "0")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = ReadyArtifactsPollingApiClient(["running"], modified_at=time.time())
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            api_client.modified_at = time.time()
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "deliverable_ready"
            assert payload["terminal"] is True
            assert payload["run_state"] == "running"
            assert payload["worker_finalization_pending"] is True
            assert payload["completion_class"] == "deliverables_ready_worker_finalizing"
            assert payload["wait_deadline_reached"] is False
            assert payload["timed_out"] is False
            links = payload["artifact_links"]
            assert links["ready_before_run_terminal"] is True
            assert links["count"] == 2
            assert links["items"][0]["path"] == "out/report/final-report.pdf"
            assert links["items"][0]["default_link_kind"] == "download"
            _assert_link_ref_url(
                links["items"][0]["signed_download_url"],
                prefix="https://glasshive.example.test/v1/link-refs/",
                kind="artifact_download",
            )
            assert "Deliver the signed artifact links" in payload["recommended_next_action"]

    asyncio.run(scenario())


def test_workspace_wait_returns_fresh_deliverable_ready_before_timeout(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_DELIVERABLE_READY_QUIET_SEC", "0")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = ReadyArtifactsPollingApiClient(["running"], modified_at=time.time())
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            api_client.modified_at = time.time()
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 30,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "deliverable_ready"
            assert payload["timed_out"] is False
            assert payload["worker_finalization_pending"] is True
            assert payload["artifact_links"]["count"] == 2

    asyncio.run(scenario())
    assert api_client.get_run_calls == 1


def test_workspace_wait_default_timeout_is_chat_safe(monkeypatch):
    monkeypatch.delenv("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", raising=False)
    monkeypatch.delenv("WPR_MCP_BLOCKING_WAIT_MAX_SEC", raising=False)

    assert mcp_server._blocking_wait_default_seconds() == 45
    assert mcp_server._blocking_wait_max_seconds() == 45


def test_workspace_wait_clamps_explicit_timeout_to_chat_safe_max(monkeypatch):
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_MAX_SEC", "0")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_POLL_INTERVAL_SEC", "1")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 240,
                    "poll_interval_seconds": 1,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "still_running"
            assert payload["timed_out"] is True
            assert payload["attempts"] == 1

    asyncio.run(scenario())
    assert api_client.get_run_calls == 1


def test_workspace_wait_uses_configured_default_timeout_when_omitted(monkeypatch):
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", "0")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_MAX_SEC", "900")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "still_running"
            assert payload["timed_out"] is True
            assert payload["terminal"] is False

    asyncio.run(scenario())
    assert api_client.get_run_calls == 1


def test_workspace_wait_long_task_timeout_env_is_capped(monkeypatch):
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", "2700")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_MAX_SEC", "3600")
    assert mcp_server._blocking_wait_default_seconds() == 2700
    assert mcp_server._blocking_wait_max_seconds() == 3600

    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", "5000")
    assert mcp_server._blocking_wait_default_seconds() == 3600


def test_worker_delegate_once_merges_upload_headers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", "{}")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    files = [
        {
            "file_id": "file-999",
            "filename": "brief.txt",
            "text": "Synthetic upload brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            **_callback_headers(),
            "X-Viventium-Request-Files": encoded_files,
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host File QA",
                    "instruction": "Read the attached brief and summarize it.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())
    worker_payload = api_client.find_or_resume_payloads[0]
    bundle = worker_payload["bootstrap_bundle"]
    assert bundle["project_definition"].startswith("# Host File QA")
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["content"] == "Synthetic upload brief."


def test_worker_delegate_once_materializes_explicit_uploaded_files(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "ignored-by-test-client",
                    "title": "Upload Roundtrip QA",
                    "instruction": "Read uploads/client_upload.txt and write upload_roundtrip_result.txt.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [
                        {
                            "file_id": "file-explicit-1",
                            "filename": "client_upload.txt",
                            "text": "CLIENT_UPLOAD_SMOKE_20260524\n",
                            "source": "librechat_model_context",
                            "context": "message_attachment",
                        }
                    ],
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    assert bundle["glasshive_upload_context"]["tool_uploaded_files"][0]["file_id"] == "file-explicit-1"
    assert bundle["viventium_upload_context"]["tool_uploaded_files"][0]["filename"] == "client_upload.txt"
    assert bundle["files"][0]["path"] == "uploads/client_upload.txt"
    assert bundle["files"][0]["content"] == "CLIENT_UPLOAD_SMOKE_20260524\n"
    assert "## Attached workspace files" in bundle["project_definition"]
    assert "`uploads/client_upload.txt`" in bundle["project_definition"]
    assert "Do not ask the user to re-attach" in bundle["project_definition"]


def test_uploaded_file_text_prefers_owner_scoped_binary_when_available(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = (
        uploads_root
        / "user-123"
        / "f3e753c4-44d9-48b5-8e0b-934b7e5f2c4a__Synthetic_Client_Brief_Source.pdf"
    )
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"%PDF-1.7\nsynthetic pdf bytes\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "PDF redaction QA",
                    "instruction": "Redact the attached PDF and return a PDF.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [
                        {
                            "file_id": "f3e753c4-44d9-48b5-8e0b-934b7e5f2c4a",
                            "filename": "Synthetic Client Brief Source.pdf",
                            "text": "extracted text is not a substitute for the original PDF",
                        }
                    ],
                },
            )
            assert _tool_json(delegated)["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    projected = bundle["files"][0]
    assert projected["path"] == "uploads/Synthetic-Client-Brief-Source.pdf"
    assert projected["source_path"] == str(upload_path)
    assert "content" not in projected
    assert projected["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_uploaded_file_text_does_not_cross_owner_boundary(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    other_upload = uploads_root / "other-user" / "uuid__same-name.pdf"
    other_upload.parent.mkdir(parents=True)
    other_upload.write_bytes(b"%PDF-1.7\nother user's file\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Cross-owner upload QA",
                    "instruction": "Use the attached file.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [{"filename": "same name.pdf", "text": "visible model text only"}],
                },
            )
            assert _tool_json(delegated)["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    projected = bundle["files"][0]
    assert projected["path"] == "uploads/same-name.pdf.metadata.json"
    manifest = json.loads(projected["content"])
    assert manifest["source_status"] == "original_bytes_unavailable"
    assert manifest["extracted_text_available"] is True
    assert "visible model text only" not in projected["content"]
    assert "source_path" not in projected
    assert "substituting extracted text" in bundle["project_definition"]


def test_binary_upload_text_without_source_reports_blocker(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    (uploads_root / "user-123").mkdir(parents=True)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Missing PDF bytes QA",
                    "instruction": "Redact the uploaded PDF and return a PDF.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [{"filename": "missing source.pdf", "text": "extracted text only"}],
                },
            )
            assert _tool_json(delegated)["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    projected = bundle["files"][0]
    assert projected["path"] == "uploads/missing-source.pdf.metadata.json"
    assert "source_path" not in projected
    assert "missing source.pdf.txt" not in json.dumps(bundle)
    manifest = json.loads(projected["content"])
    assert manifest["source_status"] == "original_bytes_unavailable"
    assert manifest["extracted_text_available"] is True
    assert "extracted text only" not in projected["content"]
    assert "metadata/blocker manifests" in bundle["project_definition"]


def test_upload_owner_id_with_path_separators_is_rejected(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    escaped_file = uploads_root / "other-user" / "brief.pdf"
    escaped_file.parent.mkdir(parents=True)
    escaped_file.write_bytes(b"%PDF-1.7\nother user's file\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    entry = mcp_server._project_upload_file_entry(
        {"filename": "brief.pdf", "text": "visible model text only"},
        1,
        tenant_id="tenant-alpha",
        owner_id="../other-user",
    )

    assert entry is not None
    assert entry["path"] == "uploads/brief.pdf.metadata.json"
    assert "source_path" not in entry


@pytest.mark.parametrize("enterprise_mode", [False, True])
def test_virtual_upload_source_enforces_asserted_owner_in_every_mode(
    monkeypatch,
    tmp_path,
    enterprise_mode,
):
    uploads_root = tmp_path / "uploads"
    own_upload = uploads_root / "owner-a" / "brief.txt"
    other_upload = uploads_root / "owner-b" / "brief.txt"
    own_upload.parent.mkdir(parents=True)
    other_upload.parent.mkdir(parents=True)
    own_upload.write_text("owner a")
    other_upload.write_text("owner b")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true" if enterprise_mode else "false")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))

    assert mcp_server._trusted_virtual_upload_source(
        "/uploads/owner-a/brief.txt",
        owner_id="owner-a",
    ) == str(own_upload)
    assert mcp_server._trusted_virtual_upload_source(
        "/uploads/owner-b/brief.txt",
        owner_id="owner-a",
    ) == ""


def test_worker_tools_use_configured_default_execution_mode(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Codex Host",
                    "role": "coding",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["execution_mode"] == "host"

    asyncio.run(scenario())


def test_worker_tools_omitted_profile_use_configured_default(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", raising=False)
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            created = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Default Codex Worker",
                    "role": "coding",
                },
            )
            created_payload = _tool_json(created)
            assert created_payload["profile"] == "codex-cli"
            assert created_payload["runtime"] == "codex-cli"

            resumed = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Default Codex Worker",
                    "role": "coding",
                    "alias": "default-codex",
                },
            )
            resumed_payload = _tool_json(resumed)
            assert resumed_payload["profile"] == "codex-cli"
            assert resumed_payload["runtime"] == "codex-cli"

    asyncio.run(scenario())

    assert api.create_worker_payloads[0]["profile"] == "codex-cli"
    assert api.find_or_resume_payloads[0]["profile"] == "codex-cli"


def test_worker_create_accepts_structured_bootstrap_bundle_files_mapping(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Browser QA\n\nOpen a local page and report the title.\n",
                            "notes/context.md": "Synthetic QA context.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "# Host Browser QA\n\nOpen a local page and report the title.\n"
            assert bundle["files"] == [
                {
                    "scope": "workspace",
                    "path": "project-definition.md",
                    "content": "# Host Browser QA\n\nOpen a local page and report the title.\n",
                },
                {
                    "scope": "workspace",
                    "path": "notes/context.md",
                    "content": "Synthetic QA context.\n",
                },
            ]

    asyncio.run(scenario())


def test_worker_create_merges_structured_bootstrap_bundle_with_upload_headers(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    files = [
        {
            "file_id": "file-789",
            "filename": "brief.txt",
            "text": "Synthetic upload brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host File QA",
                    "role": "Read the uploaded brief on the host worker.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host File QA\n\nRead the attached brief.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"].startswith("# Host File QA\n\nRead the attached brief.\n")
            assert "## Attached workspace files" in bundle["project_definition"]
            assert "`uploads/brief.txt`" in bundle["project_definition"]
            paths = [item["path"] for item in bundle["files"]]
            assert paths == ["project-definition.md", "uploads/brief.txt"]
            assert bundle["files"][1]["content"] == "Synthetic upload brief."

    asyncio.run(scenario())


def test_worker_find_or_resume_keeps_json_string_bootstrap_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "alias": "host-browser-qa",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": json.dumps(
                        {
                            "project_definition": "Open a local page and report the title.",
                            "files": [{"scope": "workspace", "path": "notes/context.md", "content": "QA context.\n"}],
                        }
                    ),
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "Open a local page and report the title."
            assert bundle["files"][0]["path"] == "notes/context.md"

    asyncio.run(scenario())


def test_worker_find_or_resume_accepts_structured_bootstrap_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "alias": "host-browser-qa",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Browser QA\n\nReport the page title.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "# Host Browser QA\n\nReport the page title.\n"
            assert bundle["files"][0]["path"] == "project-definition.md"

    asyncio.run(scenario())


def test_normalize_bootstrap_bundle_ignores_malformed_files_value():
    bundle = mcp_server._normalize_bootstrap_bundle({"project_definition": "Do the work.", "files": "oops"})

    assert bundle == {"project_definition": "Do the work.", "files": []}


def test_worker_tool_schemas_advertise_host_native_execution(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool.model_dump() for tool in await client.list_tools()}
            worker_create = tools["worker_create"]
            worker_resume = tools["worker_find_or_resume"]
            desktop_action = tools["worker_desktop_action"]
            worker_delegate = tools["worker_delegate_once"]
            workspace_launch = tools["workspace_launch"]

            for tool in (worker_create, worker_resume):
                description = tool["description"]
                assert "execution_mode='host'" in description
                schema = tool["inputSchema"]["properties"]
                execution_schema = schema["execution_mode"]
                assert execution_schema["anyOf"][0]["enum"] == ["docker", "host"]
                assert "real computer/session" in execution_schema["description"]
                assert "codex-cli" in schema["profile"]["description"]
                bootstrap_schema = schema["bootstrap_bundle_json"]
                bootstrap_types = {
                    variant.get("type")
                    for variant in bootstrap_schema.get("anyOf", [])
                    if isinstance(variant, dict)
                }
                assert {"string", "object", "null"}.issubset(bootstrap_types)

            action_schema = desktop_action["inputSchema"]["properties"]["action"]
            assert action_schema["enum"] == [
                "terminal",
                "files",
                "browser",
                "focus_browser",
                "codex",
                "claude",
                "openclaw",
            ]
            for tool in (worker_delegate, workspace_launch):
                upload_schema = tool["inputSchema"]["properties"]["uploaded_files"]
                upload_description = upload_schema["description"].lower()
                assert "attached/uploaded files" in upload_description
                assert "chat host does not project upload metadata" in upload_description

    asyncio.run(scenario())


def test_workspace_launch_uses_documented_ui_fields_without_low_level_chain(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Build a small evidence file from the uploaded report.",
                    "success_criteria": (
                        "The workspace creates summary.txt and reports where it is. "
                        "The host chat verifies View / Steer link visibility and wait/status polling cadence."
                    ),
                    "context": "Use the attached file and keep the workspace resumable.",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"
            minimal = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Summarize the provided public-safe note.",
                    "context": "No distinct acceptance criteria were supplied.",
                    "require_callback": False,
                },
            )
            assert _tool_json(minimal)["status"] == "dispatched"

    asyncio.run(scenario())

    assert api.calls == [
        "create_project",
        "find_or_resume_worker",
        "assign_run",
        "create_project",
        "find_or_resume_worker",
        "assign_run",
    ]
    assert api.find_or_resume_payloads[0]["profile"] == "codex-cli"
    explicit_instruction = api.assign_run_payloads[0]["instruction"]
    assert "Explicit success criteria:" in explicit_instruction
    assert "Treat explicit success criteria as hard acceptance gates." in explicit_instruction
    assert "The host chat verifies View / Steer link visibility" in explicit_instruction
    assert "host-side responsibilities" in explicit_instruction
    assert "do not mark the workspace blocked" in explicit_instruction
    assert "report only blockers observable from inside this worker workspace" in explicit_instruction
    minimal_instruction = api.assign_run_payloads[1]["instruction"]
    assert "Default completion check:" not in minimal_instruction
    assert "Satisfy the user's request as stated, preserving explicit constraints." not in minimal_instruction
    assert "Treat explicit success criteria as hard acceptance gates." not in minimal_instruction
    assert "No distinct acceptance criteria were supplied." in minimal_instruction


def test_workspace_launch_can_favorite_the_created_workspace_in_the_same_call(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Personal research workspace\nKeep this workspace reusable.",
                    "favorite": True,
                    "provider_account_policy": "personal_required",
                    "require_callback": False,
                },
            )
            assert _tool_json(result)["status"] == "dispatched"

    asyncio.run(scenario())

    assert api.update_workspace_payloads == [
        {"worker_id": "wrk_resumed", "favorite": True}
    ]
    assert api.calls.index("update_workspace") < api.calls.index("assign_run")
    assert api.find_or_resume_payloads[0]["name"] == "Personal research workspace"


def test_workspace_launch_returns_structured_quota_block_with_reuse_options(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class QuotaBlockedApi(TrackingApiClient):
        def find_or_resume_worker(self, **kwargs):
            self.calls.append("find_or_resume_worker")
            self.find_or_resume_payloads.append(kwargs)
            raise mcp_server.GlassHiveBlockedError(
                {
                    "status": "blocked",
                    "failure_class": "glasshive_worker_quota_exceeded",
                    "failure_retryable": 1,
                    "failure_user_message": "GlassHive did not start a new workspace because capacity is full.",
                    "failure_recommended_recovery": "Use one of `available_workspace_options` if it fits, or wait.",
                    "failure_diagnostic_summary": "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER=3",
                    "available_workspace_options": [
                        {
                            "project_id": "prj_existing",
                            "worker_id": "wrk_existing",
                            "project_title": "Existing research workspace",
                            "workspace_name": "Research",
                            "alias": "research",
                            "state": "ready",
                            "profile": "codex-cli",
                            "execution_mode": "docker",
                        }
                    ],
                    "acknowledgement_guidance": (
                        "Explain that GlassHive capacity is full. Do not claim a workspace is running."
                    ),
                    "main_agent_next_action": (
                        "Review `available_workspace_options` and pick/reuse one that matches the user's task. "
                        "Do not suggest switching profile or sandbox mode as the fix for this quota."
                    ),
                }
            )

    api = QuotaBlockedApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Do a research pass.",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "blocked"
            assert payload["failure_class"] == "glasshive_worker_quota_exceeded"
            assert payload["available_workspace_options"][0]["worker_id"] == "wrk_existing"
            assert "pick/reuse" in payload["main_agent_next_action"]
            assert "Do not suggest switching profile or sandbox mode" in payload["main_agent_next_action"]
            assert payload["view_steer_url"] is None

    asyncio.run(scenario())
    assert api.calls == ["create_project", "find_or_resume_worker"]
    assert api.assign_run_payloads == []


def test_workspace_launch_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Check connected-account content for a public-safe marker.",
                    "success_criteria": "Report only what the available tools can prove.",
                    "context": "Use connected-account MCP/tools if the host provided them.",
                    "profile": "codex-cli",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    bootstrap_bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "did not receive a complete host-signed `glasshive-user-capabilities` broker grant/config" in assigned_instruction
    assert "Do not claim brokered MCP access" in assigned_instruction
    assert "system_instructions" in bootstrap_bundle
    assert "Do not claim brokered MCP access" in bootstrap_bundle["system_instructions"]


def test_worker_delegate_once_connected_account_intent_accepts_complete_broker_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "allowed_servers": ["google_workspace", "ms-365"],
            "scopes": {"content_read": True},
        },
        "glasshive_capability_intent": {"content_read": True},
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Brokered connected-account check",
                    "instruction": "Use the brokered connected-account tools to check the public-safe marker.",
                    "goal": "Report proven results only.",
                    "profile": "codex-cli",
                    "connected_account_content_intent": True,
                    "bootstrap_bundle_json": broker_bundle,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    bootstrap_bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" not in assigned_instruction
    assert "system_instructions" not in bootstrap_bundle
    assert bootstrap_bundle["glasshive_capability_broker"]["allowed_servers"] == ["google_workspace", "ms-365"]
    assert bootstrap_bundle["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == "public-safe-test-grant"


def test_complete_broker_bundle_requires_supported_version_and_unexpired_grant():
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "scopes": {"content_read": True},
        },
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    assert mcp_server._has_complete_capability_broker_bundle(broker_bundle) is True
    assert mcp_server._has_complete_capability_broker_bundle(
        {**broker_bundle, "glasshive_capability_broker": {**broker_bundle["glasshive_capability_broker"], "version": 2}}
    ) is False
    assert mcp_server._has_complete_capability_broker_bundle(
        {
            **broker_bundle,
            "glasshive_capability_broker": {
                **broker_bundle["glasshive_capability_broker"],
                "grant_expires_at": 1,
            },
        }
    ) is False


def test_worker_delegate_once_preserves_explicit_goal_in_run_instruction(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Exact data in out QA",
                    "instruction": "Create a file named goal_preservation_qa.txt.",
                    "goal": "The file must state whether the explicit goal was visible in the active run instruction.",
                    "profile": "codex-cli",
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    assert "User-visible success condition:" in assigned_instruction
    assert "explicit goal was visible" in assigned_instruction


def test_worker_delegate_once_connected_account_intent_warns_without_content_read_scope(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "allowed_servers": ["google_workspace"],
            "scopes": {"content_read": False},
        },
        "glasshive_capability_intent": {"content_read": False},
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Brokered connected-account check",
                    "instruction": "Use brokered connected-account tools only if content-read scope is real.",
                    "goal": "Report proven results only.",
                    "profile": "codex-cli",
                    "connected_account_content_intent": True,
                    "bootstrap_bundle_json": broker_bundle,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    bootstrap_bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" in assigned_instruction
    assert "Do not claim brokered MCP access" in bootstrap_bundle["system_instructions"]


def test_worker_run_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_run",
                {
                    "worker_id": "wrk_123",
                    "instruction": "Check connected-account content for a public-safe marker.",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["state"] == "queued"

    asyncio.run(scenario())

    assigned = api.assign_run_payloads[-1]
    assert "Do not claim brokered MCP access" in assigned["instruction"]
    assert "Do not claim brokered MCP access" in assigned["bootstrap_bundle"]["system_instructions"]


def test_workspace_schedule_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Check connected-account content later.",
                    "success_criteria": "Report proven results only.",
                    "schedule_text": "in 5 minutes",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "scheduled"

    asyncio.run(scenario())

    assert "Do not claim brokered MCP access" in api.find_or_resume_payloads[-1]["bootstrap_bundle"]["system_instructions"]
    assert "Do not claim brokered MCP access" in api.schedule_run_payloads[-1]["instruction"]


def test_worker_create_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_123",
                    "name": "Connected account worker",
                    "role": "Report proven connected-account results only.",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["worker_id"] == "wrk_new"

    asyncio.run(scenario())

    bundle = api.create_worker_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" in bundle["system_instructions"]


def test_worker_find_or_resume_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_123",
                    "name": "Connected account worker",
                    "role": "Report proven connected-account results only.",
                    "alias": "connected-account-worker",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["worker_id"] == "wrk_resumed"

    asyncio.run(scenario())

    bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" in bundle["system_instructions"]


def test_worker_schedule_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_schedule",
                {
                    "worker_id": "wrk_123",
                    "instruction": "Check connected-account content later.",
                    "schedule_text": "in 5 minutes",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["state"] == "pending"

    asyncio.run(scenario())

    scheduled = api.schedule_run_payloads[-1]
    assert "Do not claim brokered MCP access" in scheduled["instruction"]
    assert "Do not claim brokered MCP access" in scheduled["bootstrap_bundle"]["system_instructions"]


def test_audit_preview_redacts_json_quoted_tokens():
    long_base64 = "A" * 520
    preview = mcp_server._audit_preview(
        json.dumps(
            {
                "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-token-value-123456",
                "auth": "abcd",
                "credential": "creds",
                "session_token": "short",
                "signature": "very-private-signature-value",
                "blob": long_base64,
                "pair": "credentialid:abcdefghijklmnopqrstuvwxyz123456",
            }
        )
    )

    assert "public-safe-token-value" not in preview
    assert "abcd" not in preview
    assert "creds" not in preview
    assert "short" not in preview
    assert "very-private-signature-value" not in preview
    assert long_base64 not in preview
    assert "abcdefghijklmnopqrstuvwxyz123456" not in preview
    assert "[REDACTED]" in preview


def test_workspace_continue_rechecks_stale_connected_account_guard_with_fresh_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class PreviousGuardApi(TrackingApiClient):
        def get_run(self, run_id: str):
            return {
                "run_id": run_id,
                "worker_id": "wrk_123",
                "project_id": "prj_123",
                "state": "failed",
                "instruction": "Check connected-account content.\n\n" + mcp_server.CONNECTED_ACCOUNT_NO_BROKER_NOTE,
            }

    api = PreviousGuardApi()
    server = create_mcp_server(api_client=api)
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "allowed_servers": ["google_workspace"],
            "scopes": {"content_read": True},
        },
        "glasshive_capability_intent": {"content_read": True},
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_continue",
                {
                    "run_id": "run_failed",
                    "connected_account_content_intent": True,
                    "bootstrap_bundle_json": broker_bundle,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "queued"

    asyncio.run(scenario())

    assigned = api.assign_run_payloads[-1]
    assert "Do not claim brokered MCP access" not in assigned["instruction"]
    assert assigned["bootstrap_bundle"]["glasshive_capability_broker"]["scopes"]["content_read"] is True


def test_workspace_launch_reuses_existing_workspace_alias_across_projects(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class ExistingAliasApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {
                "items": [
                    {
                        "worker_id": "wrk_existing",
                        "project_id": "prj_existing",
                        "name": "Marketing sandbox",
                        "alias": "marketing-sandbox",
                        "state": "paused",
                    }
                ],
                "next_cursor": None,
            }

        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "demo-owner", "title": "Marketing sandbox"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "name": "JohnDoe",
                    "role": "Marketing",
                    "profile": "codex-cli",
                    "backend": "openclaw",
                    "execution_mode": "docker",
                    "alias": "marketing-sandbox",
                    "state": "paused",
                }
            ]

    api = ExistingAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Use my Marketing sandbox to update the campaign note.",
                    "success_criteria": "The existing named workspace is reused.",
                    "workspace_alias": "marketing-sandbox",
                    "reuse_existing_workspace": True,
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] == "marketing-sandbox"


def test_workspace_launch_inherits_saved_claude_profile_when_reuse_omits_profile(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class ExistingClaudeWorkspaceApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {
                "items": [
                    {
                        "worker_id": "wrk_claude",
                        "project_id": "prj_claude",
                        "name": "Claude work services",
                        "alias": "claude-work-services",
                        "profile": "claude-code",
                        "execution_mode": "docker",
                        "state": "ready",
                    }
                ],
                "next_cursor": None,
            }

        def list_projects(self, owner_id: str | None = None):
            return [
                {
                    "project_id": "prj_claude",
                    "owner_id": "demo-owner",
                    "title": "Claude work services",
                }
            ]

        def list_workers(self, project_id: str):
            return [
                {
                    "worker_id": "wrk_claude",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "name": "Claude work services",
                    "profile": "claude-code",
                    "execution_mode": "docker",
                    "alias": "claude-work-services",
                    "state": "ready",
                }
            ]

    api = ExistingClaudeWorkspaceApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Claude work services\nCreate a short proof file.",
                    "workspace_alias": "Claude work services",
                    "reuse_existing_workspace": True,
                },
            )
            assert _tool_json(result)["status"] == "dispatched"

    asyncio.run(scenario())

    assert api.find_or_resume_payloads[-1]["profile"] == "claude-code"
    assert api.find_or_resume_payloads[-1]["execution_mode"] == "docker"


def test_workspace_launch_cannot_replace_existing_workspace_provider_policy(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class ExistingAliasApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {
                "items": [
                    {
                        "worker_id": "wrk_existing",
                        "project_id": "prj_existing",
                        "name": "Research",
                        "alias": "research",
                        "state": "paused",
                    }
                ],
                "next_cursor": None,
            }

        def list_projects(self, owner_id: str | None = None):
            return [{"project_id": "prj_existing", "owner_id": "demo-owner", "title": "Research"}]

        def list_workers(self, project_id: str):
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "alias": "research",
                    "state": "paused",
                }
            ]

    api = ExistingAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="keep their saved provider account policy"):
                await client.call_tool(
                    "workspace_launch",
                    {
                        "description": "Resume research",
                        "workspace_alias": "research",
                        "reuse_existing_workspace": True,
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                        "provider_account_policy": "personal_required",
                        "provider_account_id": "acct_codex_ready",
                    },
                )

    asyncio.run(scenario())
    assert api.find_or_resume_payloads == []
    assert api.assign_run_payloads == []


def test_workspace_launch_does_not_reuse_alias_unless_explicit(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class ExistingAliasApi(TrackingApiClient):
        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "demo-owner", "title": "Marketing sandbox"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "name": "JohnDoe",
                    "role": "Marketing",
                    "profile": "codex-cli",
                    "backend": "openclaw",
                    "execution_mode": "docker",
                    "alias": "marketing-sandbox",
                    "state": "paused",
                }
            ]

    api = ExistingAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Prepare a fresh marketing analysis.",
                    "success_criteria": "The fresh analysis is complete.",
                    "workspace_alias": "marketing-sandbox",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" in api.calls
    assert "list_projects" not in api.calls
    assert api.create_project_payloads[-1]["title"] == "Prepare a fresh marketing analysis."
    assert api.find_or_resume_payloads[-1]["project_id"] != "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] != "marketing-sandbox"


def test_worker_delegate_once_does_not_reuse_alias_unless_explicit(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Marketing sandbox",
                    "instruction": "Prepare a fresh marketing analysis.",
                    "project_id": "prj_existing",
                    "alias": "marketing-sandbox",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    worker_payload = api.find_or_resume_payloads[-1]
    assert worker_payload["project_id"] == "prj_existing"
    assert worker_payload["alias"] != "marketing-sandbox"
    assert worker_payload["alias"].startswith("marketing-sandbox-")
    assert "list_projects" not in api.calls


def test_worker_delegate_once_reuses_alias_when_explicit(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Marketing sandbox",
                    "instruction": "Continue the existing marketing analysis.",
                    "project_id": "prj_existing",
                    "alias": "marketing-sandbox",
                    "reuse_existing_workspace": True,
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    worker_payload = api.find_or_resume_payloads[-1]
    assert worker_payload["project_id"] == "prj_existing"
    assert worker_payload["alias"] == "marketing-sandbox"


def test_workspace_launch_reuses_enterprise_catalog_alias_without_scoping_twice(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-WPR-Token": "service-token",
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    class ExistingEnterpriseAliasApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {
                "items": [
                    {
                        "worker_id": "wrk_existing",
                        "project_id": "prj_existing",
                        "name": "Marketing sandbox",
                        "alias": "tenant-alpha--user-a--marketing-sandbox",
                        "state": "paused",
                    }
                ],
                "next_cursor": None,
            }

        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "user-a", "title": "Marketing sandbox"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "user-a",
                    "name": "JohnDoe",
                    "role": "Marketing",
                    "profile": "codex-cli",
                    "backend": "openclaw",
                    "execution_mode": "docker",
                    "alias": "tenant-alpha--user-a--marketing-sandbox",
                    "state": "paused",
                }
            ]

    api = ExistingEnterpriseAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Tell JohnDoe to use my Marketing sandbox for the next task.",
                    "success_criteria": "The enterprise-scoped workspace alias is reused.",
                    "workspace_alias": "tenant-alpha--user-a--marketing-sandbox",
                    "reuse_existing_workspace": True,
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] == "marketing-sandbox"


@pytest.mark.parametrize(
    ("workspace_alias", "description", "expected_searches"),
    [
        (
            None,
            "Microsoft work hub\nRead connected accounts without changing anything.",
            ("Microsoft work hub",),
        ),
        (
            "Microsoft work hub",
            "Microsoft work hub check\nRead connected accounts without changing anything.",
            ("Microsoft work hub",),
        ),
        (
            "microsoft-work-hub",
            "Microsoft work hub\nRead connected accounts without changing anything.",
            ("microsoft-work-hub",),
        ),
    ],
)
def test_workspace_launch_resolves_exact_saved_name_for_human_reuse_inputs(
    monkeypatch,
    workspace_alias,
    description,
    expected_searches,
):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-WPR-Token": "service-token",
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    search_calls = []

    class ExactNameCatalogApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            search_calls.append(kwargs["search"])
            if kwargs["search"] not in {"Microsoft work hub", "microsoft-work-hub"}:
                return {"items": [], "next_cursor": None}
            return {
                "items": [
                    {
                        "worker_id": "wrk_existing",
                        "project_id": "prj_existing",
                        "name": "Microsoft work hub",
                        "alias": "tenant-alpha--user-a--microsoft-work-hub",
                        "state": "paused",
                    },
                    {
                        "worker_id": "wrk_old_test",
                        "project_id": "prj_old_test",
                        "name": "Microsoft work hub",
                        "alias": "tenant-alpha--user-a--old-test",
                        "state": "terminated",
                    },
                ],
                "next_cursor": None,
            }

        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "user-a", "title": "Microsoft work hub"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "user-a",
                    "name": "Microsoft work hub",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "alias": "tenant-alpha--user-a--microsoft-work-hub",
                    "state": "paused",
                }
            ]

    api = ExactNameCatalogApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": description,
                    "reuse_existing_workspace": True,
                    "profile": "codex-cli",
                    "require_callback": False,
                    **({"workspace_alias": workspace_alias} if workspace_alias else {}),
                },
            )
            assert _tool_json(result)["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] == "microsoft-work-hub"
    assert search_calls == list(expected_searches)


def test_workspace_launch_explicit_alias_wins_over_colliding_description_title(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class CollidingCatalogApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            rows = {
                "microsoft-work-hub": {
                    "worker_id": "wrk_microsoft",
                    "project_id": "prj_microsoft",
                    "name": "Microsoft work hub",
                    "alias": "microsoft-work-hub",
                    "state": "paused",
                },
                "Weekly report": {
                    "worker_id": "wrk_weekly",
                    "project_id": "prj_weekly",
                    "name": "Weekly report",
                    "alias": "weekly-report",
                    "state": "paused",
                },
            }
            item = rows.get(kwargs["search"])
            return {"items": [item] if item else [], "next_cursor": None}

        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [
                {"project_id": "prj_microsoft", "owner_id": "demo-owner", "title": "Microsoft work hub"},
                {"project_id": "prj_weekly", "owner_id": "demo-owner", "title": "Weekly report"},
            ]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            aliases = {
                "prj_microsoft": "microsoft-work-hub",
                "prj_weekly": "weekly-report",
            }
            return [
                {
                    "worker_id": f"wrk_{project_id}",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "name": "Existing workspace",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "alias": aliases[project_id],
                    "state": "paused",
                }
            ]

    api = CollidingCatalogApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Weekly report\nRead connected accounts without changing anything.",
                    "workspace_alias": "microsoft-work-hub",
                    "reuse_existing_workspace": True,
                    "profile": "codex-cli",
                },
            )
            assert _tool_json(result)["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_microsoft"
    assert api.find_or_resume_payloads[-1]["alias"] == "microsoft-work-hub"


@pytest.mark.parametrize("state", ["terminating", "termination_failed", "terminated"])
def test_workspace_launch_reuse_rejects_every_closed_workspace_state(monkeypatch, state):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class ClosedCatalogApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {
                "items": [
                    {
                        "worker_id": "wrk_closed",
                        "project_id": "prj_closed",
                        "name": "Microsoft work hub",
                        "alias": "microsoft-work-hub",
                        "state": state,
                    }
                ],
                "next_cursor": None,
            }

    api = ClosedCatalogApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Could not resolve exactly one saved workspace"):
                await client.call_tool(
                    "workspace_launch",
                    {
                        "description": "Microsoft work hub\nRead connected accounts without changing anything.",
                        "reuse_existing_workspace": True,
                        "profile": "codex-cli",
                    },
                )

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert "assign_run" not in api.calls


def test_workspace_launch_reuses_legacy_workspace_by_exact_alias(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class LegacyCatalogApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            assert kwargs["kind"] == ""
            return {
                "items": [
                    {
                        "worker_id": "wrk_legacy",
                        "project_id": "prj_legacy",
                        "name": "Legacy work hub",
                        "alias": "legacy-work-hub",
                        "workspace_kind": "legacy",
                        "execution_mode": "docker",
                        "state": "paused",
                    }
                ],
                "next_cursor": None,
            }

        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_legacy", "owner_id": "demo-owner", "title": "Legacy work hub"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_legacy",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "name": "Legacy work hub",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "alias": "legacy-work-hub",
                    "workspace_kind": "legacy",
                    "state": "paused",
                }
            ]

    api = LegacyCatalogApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Legacy work hub\nRead connected accounts without changing anything.",
                    "workspace_alias": "legacy-work-hub",
                    "reuse_existing_workspace": True,
                    "profile": "codex-cli",
                },
            )
            assert _tool_json(result)["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_legacy"
    assert api.find_or_resume_payloads[-1]["alias"] == "legacy-work-hub"


def test_workspace_launch_reuse_rejects_execution_mode_mismatch_without_creation(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class MismatchedModeCatalogApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {
                "items": [
                    {
                        "worker_id": "wrk_workstation",
                        "project_id": "prj_workstation",
                        "name": "Microsoft work hub",
                        "alias": "microsoft-work-hub",
                        "execution_mode": "workstation",
                        "state": "paused",
                    }
                ],
                "next_cursor": None,
            }

    api = MismatchedModeCatalogApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Could not resolve existing workspace alias"):
                await client.call_tool(
                    "workspace_launch",
                    {
                        "description": "Microsoft work hub\nRead connected accounts without changing anything.",
                        "workspace_alias": "microsoft-work-hub",
                        "reuse_existing_workspace": True,
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                    },
                )

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert "assign_run" not in api.calls


def test_workspace_launch_reuse_rejects_unknown_explicit_alias_without_creation(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class MissingAliasCatalogApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {"items": [], "next_cursor": None}

    api = MissingAliasCatalogApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Could not resolve exactly one saved workspace"):
                await client.call_tool(
                    "workspace_launch",
                    {
                        "description": "Unrelated title\nRead connected accounts without changing anything.",
                        "workspace_alias": "missing-workspace",
                        "reuse_existing_workspace": True,
                        "profile": "codex-cli",
                    },
                )

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert "assign_run" not in api.calls


@pytest.mark.parametrize("match_count", [0, 2])
def test_workspace_launch_without_alias_fails_closed_on_missing_or_ambiguous_name(
    monkeypatch,
    match_count,
):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class AmbiguousCatalogApi(TrackingApiClient):
        def workspace_catalog(self, **kwargs):
            return {
                "items": [
                    {
                        "worker_id": f"wrk_{index}",
                        "project_id": f"prj_{index}",
                        "name": "Microsoft work hub",
                        "alias": f"microsoft-work-hub-{index}",
                    }
                    for index in range(match_count)
                ],
                "next_cursor": None,
            }

    api = AmbiguousCatalogApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Could not resolve exactly one saved workspace"):
                await client.call_tool(
                    "workspace_launch",
                    {
                        "description": "Microsoft work hub\nRead connected accounts without changing anything.",
                        "reuse_existing_workspace": True,
                        "profile": "codex-cli",
                    },
                )

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert "assign_run" not in api.calls


def test_tool_descriptions_advertise_mcp_owned_usage_contract(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = mcp_server.glasshive_workers_server_instructions()
    assert "Use the one GlassHive tool" in instructions
    assert "Make one call when one call can complete it" in instructions
    assert "never enumerate or summarize the tool catalog" in instructions
    assert "launch it directly without listing first" in instructions
    assert "without inventing plans, success criteria, tool results, or extra workflow" in instructions
    assert "Use the exact callable tool id shown by the host" in instructions
    assert len(instructions) < 1_000

    public_action_tools = {
        "projects_list",
        "project_create",
        "project_get",
        "workspace_launch",
        "worker_delegate_once",
        "project_runs",
        "project_events",
        "workers_list",
        "worker_create",
        "worker_find_or_resume",
        "worker_get",
        "worker_live",
        "worker_run",
        "worker_message",
        "worker_pause",
        "worker_resume",
        "worker_interrupt",
        "worker_terminate",
        "worker_desktop_action",
        "worker_takeover",
        "run_get",
        "workspace_status",
        "workspace_wait",
        "workspace_continue",
        "workspace_artifacts",
        "workspace_artifact_download",
        "metrics_summary",
    }

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool.model_dump() for tool in await client.list_tools()}
            assert public_action_tools.issubset(set(tools))

            for tool_name in public_action_tools:
                description = tools[tool_name]["description"]
                normalized = description.lower()
                assert "use" in normalized, tool_name
                assert "return" in normalized, tool_name
                assert any(marker in normalized for marker in ("do not", "prefer", "only when", "instead")), tool_name
                assert len(description.split()) >= 20, tool_name

            delegate_description = tools["worker_delegate_once"]["description"]
            workspace_description = tools["workspace_launch"]["description"]
            workspace_list_description = tools["workspace_list"]["description"]
            assert "do not call this before workspace_launch" in workspace_list_description.lower()
            assert "description" in workspace_description
            assert "success_criteria" in workspace_description
            assert "optional context" in workspace_description
            assert "optional success_criteria" in workspace_description
            assert "Do not chain project_create" in workspace_description
            assert "uploaded-file requests" in workspace_description
            assert "Do not shorten" in workspace_description
            assert "full available background" in workspace_description
            assert "View / Steer link" in workspace_description
            assert "workspace-internal deliverable blockers" in workspace_description
            assert "do not turn tool choice into a success criterion" in workspace_description
            assert "leave omitted criteria empty" in workspace_description
            assert "Do not invent provider lists" in workspace_description
            assert "memory-derived priorities" in workspace_description
            assert "For vague user adjectives like urgent or important" in workspace_description
            assert "must not fabricate MCP/tool results" in workspace_description
            assert "force a downloadable artifact" in workspace_description
            assert "deep research" in workspace_description
            assert "critical analysis" in workspace_description
            assert "high/xhigh" in workspace_description
            assert "Claude max/xhigh" in workspace_description
            assert "omit effort" in workspace_description
            assert "deployment defaults own the baseline" in workspace_description
            assert "Use medium only for ordinary bounded tasks" not in workspace_description
            assert "first show the View / Steer link" in workspace_description

            assert "callbacks are optional" in delegate_description.lower()
            assert "uploaded-file tasks" in delegate_description
            assert "workspace_status" in delegate_description
            assert "workspace_wait" in delegate_description
            assert "write your own short acknowledgement" in delegate_description.lower()
            assert "sandbox" in delegate_description.lower()
            assert "blocked" in delegate_description.lower()
            assert "delegation_audit" in delegate_description
            assert "View / Steer link" in delegate_description
            assert "result_tools" in delegate_description
            assert "expose_diagnostics=true only when" in delegate_description
            assert "Do not shorten" in delegate_description
            assert "full available brief" in delegate_description
            assert "workspace-internal deliverable blockers" in delegate_description
            assert "Pass MCP/tool availability as context" in delegate_description
            assert "critical analysis" in delegate_description
            assert "high/xhigh" in delegate_description

            for tool_name in (
                "workspace_launch",
                "worker_delegate_once",
                "workspace_schedule",
                "worker_create",
                "worker_find_or_resume",
                "worker_run",
                "worker_schedule",
            ):
                schema = tools[tool_name]["inputSchema"]["properties"]
                assert "connected_account_content_intent" in schema
                intent_description = schema["connected_account_content_intent"]["description"]
                assert "connected-account content" in intent_description
                assert "host-signed broker grant" in intent_description
                assert "does not unlock reads or writes" in intent_description

            desktop_description = tools["worker_desktop_action"]["description"]
            assert "raw desktop URLs are diagnostic" in desktop_description

            wait_description = tools["workspace_wait"]["description"]
            assert "first surface the View / Steer link" in wait_description
            assert "pass its returned run_id and worker_id" in wait_description
            assert "Omit poll_interval_seconds for normal work" in wait_description
            assert "backs off toward the configured cap" in wait_description

            launch_success_description = tools["workspace_launch"]["inputSchema"]["properties"][
                "success_criteria"
            ]["description"]
            schedule_description = tools["workspace_schedule"]["description"]
            schedule_success_description = tools["workspace_schedule"]["inputSchema"]["properties"][
                "success_criteria"
            ]["description"]
            launch_context_description = tools["workspace_launch"]["inputSchema"]["properties"][
                "context"
            ]["description"]
            launch_effort_description = tools["workspace_launch"]["inputSchema"]["properties"]["effort"][
                "description"
            ]
            assert "Use explicit user requirements only" in launch_success_description
            assert "Optional workspace-internal acceptance criteria" in launch_success_description
            assert "broker/tool availability" in launch_success_description
            assert "memory-derived priorities" in launch_context_description
            assert "For vague user adjectives like urgent or important" in launch_context_description
            assert "deep research" in launch_effort_description
            assert "critical analysis" in launch_effort_description
            assert "omit effort" in launch_effort_description
            assert "deployment defaults own the baseline" in launch_effort_description
            assert "minimal is for explicitly allowlisted deployments only" in launch_effort_description
            assert "Use medium only for ordinary bounded tasks" not in launch_effort_description
            assert "Do not invent schedule success criteria" in schedule_description
            assert "Trust the scheduled GlassHive worker" in schedule_description
            assert "For vague user adjectives like urgent or important" in schedule_description
            assert "Use explicit user requirements only" in schedule_success_description
            assert "Optional acceptance criteria" in schedule_success_description
            assert "omit this field" in (
                launch_success_description + schedule_success_description
            )
            assert "success_criteria" not in tools["workspace_launch"]["inputSchema"].get("required", [])
            assert "success_criteria" not in tools["workspace_schedule"]["inputSchema"].get("required", [])

    asyncio.run(scenario())


def test_host_worker_disabled_forces_docker_default_and_rejects_host(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Docker Worker",
                    "role": "coding",
                    "alias": "docker-main",
                    "profile": "codex-cli",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["execution_mode"] == "docker"

            with pytest.raises(ToolError, match="host-native GlassHive workers are disabled"):
                await client.call_tool(
                    "worker_find_or_resume",
                    {
                        "project_id": "prj_new",
                        "owner_id": "demo-owner",
                        "name": "Codex Host",
                        "role": "coding",
                        "alias": "codex-main",
                        "profile": "codex-cli",
                        "execution_mode": "host",
                    },
                )

    asyncio.run(scenario())


def test_worker_tools_default_owner_from_request_headers(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-from-request",
        },
    )
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            project = await client.call_tool(
                "project_create",
                {
                    "title": "Host Browser QA",
                    "goal": "Open a host browser.",
                    "default_worker_profile": "codex-cli",
                },
            )
            project_payload = _tool_json(project)
            assert project_payload["owner_id"] == "user-from-request"

            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "name": "Codex Host",
                    "role": "host browser control",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["owner_id"] == "user-from-request"
            assert worker_payload["execution_mode"] == "host"

    asyncio.run(scenario())


def test_worker_delegate_once_local_request_identity_overrides_forged_owner_and_project(
    monkeypatch,
):
    class OwnerCheckingClient(FakeApiClient):
        def __init__(self):
            self.project_lookups: list[str] = []
            self.created_owners: list[str | None] = []

        def get_project(self, project_id: str):
            self.project_lookups.append(project_id)
            return {
                "project_id": project_id,
                "owner_id": "owner-b",
                "title": "Sibling project",
                "goal": "Must remain isolated",
            }

        def create_project(self, **kwargs):
            self.created_owners.append(kwargs.get("owner_id"))
            return super().create_project(**kwargs)

    api = OwnerCheckingClient()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-local",
            "X-Viventium-User-Id": "owner-a",
        },
    )
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="project is not available"):
                await client.call_tool(
                    "worker_delegate_once",
                    {
                        "title": "Scoped mission",
                        "instruction": "Perform only the authenticated owner's work.",
                        "owner_id": "owner-b",
                        "project_id": "prj_owner_b",
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                    },
                )

    asyncio.run(scenario())
    assert api.created_owners == []
    assert api.project_lookups == ["prj_owner_b"]


def test_local_mcp_legacy_tools_forward_trusted_owner_scope_to_the_api(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_MODE", raising=False)
    api_app = create_app(
        db_path=str(tmp_path / "local-mcp-owner-scope.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    api_http = TestClient(api_app)
    owner_a_headers = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-local",
        "X-Viventium-User-Id": "owner-a",
    }
    owner_b_headers = {
        **owner_a_headers,
        "X-Viventium-User-Id": "owner-b",
    }
    project_a = api_http.post(
        "/v1/projects",
        headers=owner_a_headers,
        json={"owner_id": "forged", "title": "Owner A", "goal": "A only"},
    ).json()
    project_b = api_http.post(
        "/v1/projects",
        headers=owner_b_headers,
        json={"owner_id": "forged", "title": "Owner B", "goal": "B only"},
    ).json()

    class InProcessHttpClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, json=None, headers=None):
            parsed = urlsplit(url)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            return api_http.request(method, path, json=json, headers=headers)

    monkeypatch.setattr(mcp_server.httpx, "Client", InProcessHttpClient)
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-local",
            "X-Viventium-User-Id": "owner-a",
        },
    )
    api = mcp_server.WorkersProjectsApiClient(
        base_url="http://glasshive.in-process",
        api_token="service-token",
    )
    server = create_mcp_server(api_client=api)
    created_projects: list[dict] = []

    async def scenario():
        async with Client(server) as mcp_client:
            listed = _tool_json(
                await mcp_client.call_tool("projects_list", {"owner_id": "owner-b"})
            )
            assert listed in ([], {"result": []})
            with pytest.raises(ToolError):
                await mcp_client.call_tool(
                    "project_get", {"project_id": project_b["project_id"]}
                )
            created = _tool_json(
                await mcp_client.call_tool(
                    "project_create",
                    {
                        "owner_id": "owner-b",
                        "title": "Forged owner project",
                        "goal": "Must remain with the trusted owner",
                    },
                )
            )
            assert created["owner_id"] == "owner-a"
            created_projects.append(created)

    asyncio.run(scenario())
    assert {item["project_id"] for item in api.list_projects()} == {
        project_a["project_id"],
        created_projects[0]["project_id"],
    }
    with pytest.raises(mcp_server.GlassHiveApiError):
        api.get_project(project_b["project_id"])


def test_merge_request_context_adds_callback_metadata(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://localhost:3080/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Agent-Id": "agent-main",
            "X-Viventium-Conversation-Id": "conv-123",
            "X-Viventium-Parent-Message-Id": "msg-parent",
            "X-Viventium-Message-Id": "msg-current",
            "X-Viventium-Surface": "telegram",
            "X-Viventium-Input-Mode": "voice_note",
            "X-Viventium-Stream-Id": "stream-123",
            "X-Viventium-Voice-Call-Session-Id": "call-123",
            "X-Viventium-Voice-Request-Id": "voice-req-123",
            "X-Viventium-Telegram-Chat-Id": "chat-123",
            "X-Viventium-Telegram-User-Id": "tg-user-123",
            "X-Viventium-Telegram-Message-Id": "tg-msg-123",
            "X-Viventium-Logical-Turn-Id": "turn-123",
            "X-Viventium-Logical-Turn-Revision": "2",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    callbacks = bundle["callbacks"]
    assert callbacks["events_webhook_url"].endswith("/api/viventium/glasshive/callback")
    assert callbacks["hmac_secret"] == "callback-secret"
    assert callbacks["conversation_id"] == "conv-123"
    assert callbacks["parent_message_id"] == "msg-parent"
    assert callbacks["surface"] == "telegram"
    assert callbacks["stream_id"] == "stream-123"
    assert callbacks["voice_call_session_id"] == "call-123"
    assert callbacks["telegram_chat_id"] == "chat-123"
    assert callbacks["logical_turn_id"] == "turn-123"
    assert callbacks["logical_turn_revision"] == "2"
    assert bundle["viventium_context"]["user_id"] == "user-123"


def test_merge_request_context_loads_callback_metadata_from_runtime_env(tmp_path, monkeypatch):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "\n".join(
            [
                "VIVENTIUM_GLASSHIVE_CALLBACK_URL=http://localhost:3080/api/viventium/glasshive/callback",
                "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET=runtime-secret",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_env))
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Conversation-Id": "conv-123",
            "X-Viventium-Parent-Message-Id": "msg-parent",
            "X-Viventium-Message-Id": "msg-current",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    callbacks = bundle["callbacks"]
    assert callbacks["events_webhook_url"].endswith("/api/viventium/glasshive/callback")
    assert callbacks["hmac_secret"] == "runtime-secret"


def test_merge_request_context_does_not_auto_attach_incomplete_parent_callback(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://localhost:3080/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Conversation-Id": "conv-123",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    assert "callbacks" not in bundle
    assert bundle["viventium_context"]["user_id"] == "user-123"
    assert bundle["viventium_context"]["conversation_id"] == "conv-123"


def test_merge_request_context_projects_uploaded_file_headers(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_viventium_runtime_env", lambda: {})
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["viventium_upload_context"]["request_files"][0]["file_id"] == "file-123"
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"
    assert "source_path" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert manifest["file_id"] == "file-123"
    assert "source_ref" not in manifest


def test_merge_request_context_accepts_generic_glasshive_headers(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_viventium_runtime_env", lambda: {})
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-GlassHive-Conversation-Id": "conv-123",
            "X-GlassHive-Request-Files": encoded_files,
            "X-LibreChat-Tool-Resources": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["glasshive_context"]["tenant_id"] == "tenant-alpha"
    assert bundle["glasshive_context"]["user_id"] == "user-123"
    assert bundle["viventium_context"]["conversation_id"] == "conv-123"
    assert bundle["glasshive_upload_context"]["request_files"][0]["file_id"] == "file-123"
    assert bundle["viventium_upload_context"]["tool_resources"][0]["file_id"] == "file-123"
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"


def test_merge_request_context_maps_virtual_uploads_to_trusted_local_source(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = uploads_root / "user-123" / "brief with spaces.txt"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text("Use this brief.")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief with spaces.txt",
            "filepath": "/uploads/user-123/brief with spaces.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief-with-spaces.txt"
    assert bundle["files"][0]["source_path"] == str(upload_path)
    assert "## Attached workspace files" in bundle["project_definition"]
    assert "`uploads/brief-with-spaces.txt`" in bundle["project_definition"]
    assert "Do not ask the user to re-attach" in bundle["project_definition"]
    assert bundle["files"][0]["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_merge_request_context_preserves_ordered_media_group_metadata(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    owner_root = uploads_root / "user-123"
    owner_root.mkdir(parents=True)
    first_path = owner_root / "first-image.jpg"
    second_path = owner_root / "second-image.jpg"
    first_path.write_bytes(b"synthetic-first-image")
    second_path.write_bytes(b"synthetic-second-image")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-first",
            "filename": "first-image.jpg",
            "filepath": "/uploads/user-123/first-image.jpg",
            "source": "local",
            "context": "message_attachment",
            "media_group_index": 0,
        },
        {
            "file_id": "file-second",
            "filename": "second-image.jpg",
            "filepath": "/uploads/user-123/second-image.jpg",
            "source": "local",
            "context": "message_attachment",
            "media_group_index": 1,
        },
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Inspect both images."})

    assert [entry["file_id"] for entry in bundle["files"]] == [
        "file-first",
        "file-second",
    ]
    assert [entry["media_group_index"] for entry in bundle["files"]] == [0, 1]
    assert [os.path.basename(entry["source_path"]) for entry in bundle["files"]] == [
        "first-image.jpg",
        "second-image.jpg",
    ]


def test_merge_request_context_uses_storage_user_id_for_upload_source(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = uploads_root / "storage-user-123" / "uuid__brief with spaces.pdf"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"%PDF-1.7\nsynthetic pdf bytes\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief with spaces.pdf",
            "filepath": "/uploads/storage-user-123/uuid__brief with spaces.pdf",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "person@example.com",
            "X-Viventium-Storage-User-Id": "storage-user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["glasshive_context"]["user_id"] == "person@example.com"
    assert "storage_user_id" not in bundle["glasshive_context"]
    assert "storage_user_id" not in bundle["viventium_context"]
    assert '"storage_user_id"' not in json.dumps(bundle)
    assert bundle["files"][0]["path"] == "uploads/brief-with-spaces.pdf"
    assert bundle["files"][0]["source_path"] == str(upload_path)
    assert bundle["files"][0]["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="person@example.com",
    )


def test_explicit_uploaded_file_never_resolves_owner_storage_by_filename_only(
    monkeypatch, tmp_path
):
    uploads_root = tmp_path / "uploads"
    owner_root = uploads_root / "storage-user-123"
    owner_root.mkdir(parents=True)
    older = owner_root / "older-token__same display name.pdf"
    newer = owner_root / "private-token__same display name.pdf"
    older.write_bytes(b"%PDF-1.7\nolder bytes\n")
    newer.write_bytes(b"%PDF-1.7\nprivate newer bytes\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "person@example.com",
            "X-Viventium-Storage-User-Id": "storage-user-123",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})
    request_context = bundle.get("glasshive_context") if isinstance(bundle, dict) else None
    bundle = mcp_server._merge_explicit_uploaded_files(
        bundle,
        [{"filename": "same display name.pdf", "text": "visible model text is not enough"}],
        tenant_id=request_context.get("tenant_id"),
        owner_id=request_context.get("user_id"),
        storage_owner_id=request_context.get("storage_user_id"),
    )

    projected = bundle["files"][0]
    assert projected["path"] == "uploads/same-display-name.pdf.metadata.json"
    assert "source_path" not in projected
    assert str(older) not in json.dumps(bundle)
    assert str(newer) not in json.dumps(bundle)


def test_local_explicit_upload_filename_uses_header_owner_not_model_bundle_scope(
    monkeypatch,
    tmp_path,
):
    uploads_root = tmp_path / "uploads"
    own_upload = uploads_root / "owner-a" / "uuid-a__same display name.pdf"
    other_upload = uploads_root / "owner-b" / "uuid-b__same display name.pdf"
    own_upload.parent.mkdir(parents=True)
    other_upload.parent.mkdir(parents=True)
    own_upload.write_bytes(b"%PDF-1.7\nowner a\n")
    other_upload.write_bytes(b"%PDF-1.7\nowner b\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "local",
            "X-Viventium-User-Id": "owner-a",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Owner-scoped local upload",
                    "instruction": "Use the attached PDF.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "bootstrap_bundle_json": {
                        "glasshive_context": {
                            "tenant_id": "forged-tenant",
                            "user_id": "owner-b",
                            "storage_user_id": "owner-b",
                        }
                    },
                    "uploaded_files": [
                        {
                            "file_id": "uuid-a",
                            "filename": "same display name.pdf",
                            "text": "model-visible text is not file authorization",
                        }
                    ],
                },
            )
            assert _tool_json(delegated)["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    projected = bundle["files"][0]
    assert projected["path"] == "uploads/same-display-name.pdf"
    assert projected["source_path"] == str(own_upload)
    assert projected["source_path"] != str(other_upload)


def test_storage_user_id_does_not_allow_other_storage_owner_path(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    other_upload = uploads_root / "other-storage-user" / "uuid__brief.pdf"
    other_upload.parent.mkdir(parents=True)
    other_upload.write_bytes(b"%PDF-1.7\nother user's file\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-other",
            "filename": "brief.pdf",
            "filepath": "/uploads/other-storage-user/uuid__brief.pdf",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "person@example.com",
            "X-Viventium-Storage-User-Id": "storage-user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.pdf.metadata.json"
    assert "source_path" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert "source_ref" not in manifest


def test_legacy_upload_fallback_never_materializes_an_unselected_recent_owner_file(
    monkeypatch, tmp_path, caplog
):
    uploads_root = tmp_path / "uploads"
    upload_path = uploads_root / "storage-user-123" / "cb606104-3792-48ca-a767-76d67d939ba4__source report.pdf"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"%PDF-1.7\nsynthetic pdf bytes\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _configure_enterprise_mcp_oauth(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    monkeypatch.setenv("GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_FALLBACK", "true")
    monkeypatch.setenv("GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_RECENT_SECONDS", "900")
    monkeypatch.setattr(mcp_server, "DEFAULT_MCP_API_TOKEN", "service-secret")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-WPR-Token": "service-secret",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user@example.test",
            "X-GlassHive-Storage-User-Id": "storage-user-123",
            "X-GlassHive-Conversation-Id": "conv-public-safe",
            "X-GlassHive-Message-Id": "msg-public-safe",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            await client.call_tool(
                "workspace_launch",
                {
                    "description": "Modify the attached PDF.",
                    "success_criteria": "Return a revised PDF.",
                    "expose_diagnostics": True,
                },
            )

    with caplog.at_level("INFO", logger="workers_projects_runtime.mcp_server"):
        asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "files" not in bundle
    assert "legacy_owner_recent_uploads" not in json.dumps(bundle)
    assert "storage_user_id" not in json.dumps(bundle)
    assert "legacy LibreChat upload compatibility fallback materialized" not in caplog.text


def test_legacy_upload_fallback_is_disabled_by_default(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = uploads_root / "storage-user-123" / "uuid__source.pdf"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"%PDF-1.7\nsynthetic pdf bytes\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user@example.test",
            "X-GlassHive-Storage-User-Id": "storage-user-123",
            "X-GlassHive-Conversation-Id": "conv-public-safe",
            "X-GlassHive-Message-Id": "msg-public-safe",
        },
    )

    bundle = mcp_server._merge_request_context({})
    assert "files" not in bundle
    assert "glasshive_upload_context" not in bundle


def test_legacy_upload_fallback_ignores_other_owner_and_old_files(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    old_owner_upload = uploads_root / "storage-user-123" / "uuid__old.pdf"
    other_owner_upload = uploads_root / "other-storage-user" / "uuid__recent.pdf"
    old_owner_upload.parent.mkdir(parents=True)
    other_owner_upload.parent.mkdir(parents=True)
    old_owner_upload.write_bytes(b"%PDF-1.7\nold owner file\n")
    other_owner_upload.write_bytes(b"%PDF-1.7\nother owner file\n")
    old_mtime = time.time() - 3600
    os.utime(old_owner_upload, (old_mtime, old_mtime))
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_FALLBACK", "true")
    monkeypatch.setenv("GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_RECENT_SECONDS", "60")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user@example.test",
            "X-GlassHive-Storage-User-Id": "storage-user-123",
            "X-GlassHive-Conversation-Id": "conv-public-safe",
            "X-GlassHive-Message-Id": "msg-public-safe",
        },
    )

    bundle = mcp_server._merge_request_context({})
    assert "files" not in bundle
    assert "glasshive_upload_context" not in bundle


def test_merge_request_context_uses_existing_source_root_when_upload_root_is_stale(monkeypatch, tmp_path):
    stale_root = tmp_path / "stale-uploads"
    fallback_root = tmp_path / "repo-uploads"
    upload_path = fallback_root / "user-123" / "brief.txt"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text("Use this brief.")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(stale_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", os.pathsep.join([str(stale_root), str(fallback_root)]))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user-123/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["source_path"] == str(upload_path)
    assert bundle["files"][0]["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_merge_request_context_uses_metadata_manifest_when_upload_file_is_missing(monkeypatch, tmp_path):
    missing_root = tmp_path / "missing-uploads"
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(missing_root))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user-123/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"
    assert "source_path" not in bundle["files"][0]
    assert "source_path_token" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert "source_ref" not in manifest


def test_enterprise_request_context_does_not_copy_cross_user_virtual_upload(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    other_user_file = uploads_root / "other-user" / "brief.txt"
    other_user_file.parent.mkdir(parents=True)
    other_user_file.write_text("other user's data")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-other",
            "filename": "brief.txt",
            "filepath": "/uploads/other-user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"
    assert "source_path" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert "source_ref" not in manifest
    assert "## Attached workspace files" in bundle["project_definition"]
    assert "`uploads/brief.txt.metadata.json`" in bundle["project_definition"]


def test_multi_user_security_mode_rejects_cross_user_virtual_upload_without_legacy_flag(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    own_file = uploads_root / "user-a" / "own.txt"
    other_file = uploads_root / "user-b" / "secret.txt"
    own_file.parent.mkdir(parents=True)
    other_file.parent.mkdir(parents=True)
    own_file.write_text("synthetic owner data")
    other_file.write_text("synthetic cross-user data")
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_MODE", raising=False)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))

    assert mcp_server._trusted_virtual_upload_source(
        "/uploads/user-a/own.txt",
        owner_id="user-a",
        storage_owner_id="user-a",
    ) == str(own_file)
    assert mcp_server._trusted_virtual_upload_source(
        "/uploads/user-b/secret.txt",
        owner_id="user-a",
        storage_owner_id="user-a",
    ) == ""
    assert mcp_server._diagnostic_payloads_enabled() is False


@pytest.mark.parametrize("explicit_exists", [True, False])
def test_explicit_runtime_env_never_imports_another_installation(monkeypatch, tmp_path, explicit_exists):
    from workers_projects_runtime import runtime_env

    default_dir = tmp_path / "Library/Application Support/Viventium/runtime"
    default_dir.mkdir(parents=True)
    (default_dir / "runtime.env").write_text("GLASSHIVE_SIGNED_LINK_SECRET=other-installation\n")
    explicit = tmp_path / "selected/runtime.env"
    if explicit_exists:
        explicit.parent.mkdir()
        explicit.write_text("GLASSHIVE_RUNTIME_BASE_URL=http://selected.invalid\n")
    monkeypatch.setattr(runtime_env.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(explicit))
    monkeypatch.delenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)
    assert runtime_env._candidate_env_files() == [explicit]
    runtime_env.load_viventium_runtime_env({"GLASSHIVE_SIGNED_LINK_SECRET"})
    assert "GLASSHIVE_SIGNED_LINK_SECRET" not in os.environ


def test_runtime_env_repairs_missing_upload_root_to_local_checkout(monkeypatch, tmp_path):
    fallback_root = tmp_path / "repo-uploads"
    fallback_root.mkdir()
    runtime_file = tmp_path / "runtime.env"
    runtime_file.write_text(
        "\n".join(
            [
                f"WPR_LIBRECHAT_UPLOADS_ROOT={tmp_path / 'missing-uploads'}",
                f"WPR_BOOTSTRAP_SOURCE_ROOTS={tmp_path / 'missing-uploads'}",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_ROOTS", raising=False)
    monkeypatch.setattr(runtime_env, "_local_checkout_librechat_uploads_root", lambda: fallback_root)

    loaded = runtime_env.load_viventium_runtime_env({"WPR_LIBRECHAT_UPLOADS_ROOT", "WPR_BOOTSTRAP_SOURCE_ROOTS"})

    assert loaded["WPR_LIBRECHAT_UPLOADS_ROOT"] == str(fallback_root)
    assert os.environ["WPR_LIBRECHAT_UPLOADS_ROOT"] == str(fallback_root)
    assert str(fallback_root) in os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"].split(os.pathsep)


def test_runtime_env_loads_host_cli_binary_paths(monkeypatch, tmp_path):
    runtime_file = tmp_path / "runtime.env"
    runtime_file.write_text(
        "\n".join(
            [
                f"WPR_CODEX_BIN={tmp_path / 'codex'}",
                f"WPR_CLAUDE_CODE_BIN={tmp_path / 'claude'}",
                f"WPR_OPENCLAW_BIN={tmp_path / 'openclaw'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    monkeypatch.delenv("WPR_CODEX_BIN", raising=False)
    monkeypatch.delenv("WPR_CLAUDE_CODE_BIN", raising=False)
    monkeypatch.delenv("WPR_OPENCLAW_BIN", raising=False)

    loaded = runtime_env.load_viventium_runtime_env(
        {"WPR_CODEX_BIN", "WPR_CLAUDE_CODE_BIN", "WPR_OPENCLAW_BIN"}
    )

    assert loaded["WPR_CODEX_BIN"] == str(tmp_path / "codex")
    assert os.environ["WPR_CLAUDE_CODE_BIN"] == str(tmp_path / "claude")
    assert os.environ["WPR_OPENCLAW_BIN"] == str(tmp_path / "openclaw")


def test_runtime_env_loads_provider_account_home_and_isolation(monkeypatch, tmp_path):
    runtime_file = tmp_path / "runtime.env"
    account_root = tmp_path / "provider-accounts"
    runtime_file.write_text(
        f"GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT={account_root}\n"
        "GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION=per_worker_container\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    monkeypatch.delenv("GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT", raising=False)
    monkeypatch.delenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", raising=False)

    loaded = runtime_env.load_viventium_runtime_env()

    assert loaded["GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT"] == str(account_root)
    assert loaded["GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION"] == "per_worker_container"


def test_runtime_env_loads_host_worker_native_capability_knobs(monkeypatch, tmp_path):
    runtime_file = tmp_path / "runtime.env"
    runtime_file.write_text(
        "\n".join(
            [
                'GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON={"claude-code":{"required_help_flags":["--chrome"]}}',
                "WPR_HOST_RUNTIME_REQUIREMENTS_FILE=glasshive-requirements.json",
                "GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST=computer-use,node_repl",
                "WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST=computer-use,node_repl",
                "GLASSHIVE_HOST_CODEX_PLUGIN_CACHE=codex-plugin-cache",
                "WPR_HOST_CODEX_PLUGIN_CACHE=codex-plugin-cache",
                "GLASSHIVE_HOST_PLUGIN_DENYLIST=synthetic-plugin@synthetic-marketplace",
                "WPR_HOST_PLUGIN_DENYLIST=synthetic-plugin@synthetic-marketplace",
                "WPR_CODEX_CLI_PERSONALITY=none",
                "WPR_CODEX_CLI_CONVERSATION_PROJECT_INSTRUCTIONS=exclude",
                "WPR_CODEX_CLI_IGNORE_USER_CONFIG=false",
                "WPR_CODEX_CLI_DISABLE_FEATURES=image_generation",
                "WPR_CODEX_CLI_PROVIDER_NAME=GlassHive Test Provider",
                "WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER=false",
                "WPR_CLAUDE_CODE_ENABLE_CHROME=true",
                "WPR_CLAUDE_CODE_EFFORT=max",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    keys = {
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        "WPR_HOST_RUNTIME_REQUIREMENTS_FILE",
        "GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
        "WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
        "GLASSHIVE_HOST_CODEX_PLUGIN_CACHE",
        "WPR_HOST_CODEX_PLUGIN_CACHE",
        "GLASSHIVE_HOST_PLUGIN_DENYLIST",
        "WPR_HOST_PLUGIN_DENYLIST",
        "WPR_CODEX_CLI_PERSONALITY",
        "WPR_CODEX_CLI_CONVERSATION_PROJECT_INSTRUCTIONS",
        "WPR_CODEX_CLI_IGNORE_USER_CONFIG",
        "WPR_CODEX_CLI_DISABLE_FEATURES",
        "WPR_CODEX_CLI_PROVIDER_NAME",
        "WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER",
        "WPR_CLAUDE_CODE_ENABLE_CHROME",
        "WPR_CLAUDE_CODE_EFFORT",
    }
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    loaded = runtime_env.load_viventium_runtime_env()

    for key in keys:
        assert key in loaded
        assert os.environ[key] == loaded[key]
    assert os.environ["WPR_CLAUDE_CODE_EFFORT"] == "max"
    assert "computer-use" in os.environ["GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST"]
    assert os.environ["GLASSHIVE_HOST_PLUGIN_DENYLIST"] == (
        "synthetic-plugin@synthetic-marketplace"
    )


def test_merge_request_context_projects_extracted_upload_text(monkeypatch):
    files = [
        {
            "file_id": "file-456",
            "filename": "brief.txt",
            "text": "Use this brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["content"] == "Use this brief."


def test_deferred_callback_tool_registry_names_only_registered_dispatch_tools():
    import asyncio

    from workers_projects_runtime.mcp_tool_registry import DEFERRED_CALLBACK_TOOLS

    server = create_mcp_server(api_client=FakeApiClient())
    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    missing = sorted(DEFERRED_CALLBACK_TOOLS - registered)
    assert missing == [], f"registry names unregistered tools: {missing}"
    # Request/response tools stay outside the registry so they never arm long host polling.
    for name in ("workspace_status", "workspace_wait", "run_get", "project_get", "workers_list"):
        assert name in registered and name not in DEFERRED_CALLBACK_TOOLS
