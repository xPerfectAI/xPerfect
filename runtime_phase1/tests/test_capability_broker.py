from __future__ import annotations

import base64
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.capability_broker import (
    CapabilityBrokerConfig,
    CapabilityBrokerError,
    CapabilityOwnerBinding,
    GlassHiveCapabilityBroker,
    capability_broker_config_from_environment,
    worker_with_ephemeral_capability_bundle,
)
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, StubRuntime
from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime


SECRET = "synthetic-capability-secret-with-at-least-32-characters"
NOW = 2_000_000_000


def _decode_assertion(token: str) -> dict[str, object]:
    padding = "=" * ((4 - len(token) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))


def config() -> CapabilityBrokerConfig:
    return CapabilityBrokerConfig(
        issuer_url="https://librechat.example.test/api/viventium/glasshive/capabilities/direct",
        secret=SECRET,
        broker_tenant_id="broker-tenant",
        owner_bindings=(
            CapabilityOwnerBinding(
                glasshive_tenant_id="glass-tenant",
                glasshive_owner_id="owner-a",
                librechat_user_id="user-a",
                proof="operator_verified",
            ),
            CapabilityOwnerBinding(
                glasshive_tenant_id="glass-tenant",
                glasshive_owner_id="owner-b",
                librechat_user_id="user-b",
                proof="operator_verified",
            ),
        ),
    )


class FakeCapabilityIssuer:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.grant_counter = 0

    def __call__(self, method, url, headers, body, timeout):
        assertion = _decode_assertion(headers["Authorization"].removeprefix("Bearer "))
        self.requests.append(
            {
                "method": method,
                "url": url,
                "assertion": assertion,
                "body": json.loads(body),
                "timeout": timeout,
            }
        )
        if url.endswith("/status"):
            return 200, {}, {
                "status": "degraded",
                "reason": "",
                "connections": [
                    {
                        "connection_id": "librechat:documents",
                        "label": "Documents",
                        "kind": "documents",
                        "adapter": "librechat_capability_broker",
                        "status": "ready",
                    },
                    {
                        "connection_id": "librechat:calendar",
                        "label": "Calendar",
                        "kind": "calendar",
                        "adapter": "librechat_capability_broker",
                        "status": "action_required",
                    },
                ],
            }
        if url.endswith("/revoke"):
            return 200, {}, {
                "revoked": True,
                "grant_id": json.loads(body)["grant_id"],
            }
        self.grant_counter += 1
        run_id = str(assertion["run_id"])
        worker_id = str(assertion["worker_id"])
        user_id = str(assertion["user_id"])
        mode = str(assertion["execution_mode"])
        grant_id = f"ghcb_direct_{self.grant_counter:064x}"
        return 200, {"cache-control": "no-store"}, {
            "bootstrapBundle": {
                "glasshive_capability_broker": {
                    "grant_id": grant_id,
                    "allowed_servers": ["documents"],
                },
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": f"synthetic-run-token-{self.grant_counter}"
                },
                "codex_config_append": "[mcp_servers.glasshive-user-capabilities]",
            },
            "grantRef": {
                "grant_id": grant_id,
                "tenant_id": "broker-tenant",
                "user_id": user_id,
                "worker_id": worker_id,
                "run_id": run_id,
                "execution_mode": mode,
                "exp": NOW + 600,
                "renewable_until": NOW + 3600,
            },
            "capabilityStatus": {
                "status": "ready",
                "connections": [
                    {
                        "connection_id": "librechat:documents",
                        "label": "Documents",
                        "kind": "documents",
                        "adapter": "librechat_capability_broker",
                        "status": "ready",
                    }
                ],
            },
        }


def test_owner_binding_is_explicit_and_two_users_are_isolated(monkeypatch):
    monkeypatch.setenv(
        "GLASSHIVE_CAPABILITY_BROKER_ISSUER_URL",
        "https://librechat.example.test/api/viventium/glasshive/capabilities/direct",
    )
    monkeypatch.setenv("GLASSHIVE_CAPABILITY_BROKER_ISSUER_SECRET", SECRET)
    monkeypatch.setenv("GLASSHIVE_CAPABILITY_BROKER_TENANT_ID", "broker-tenant")
    monkeypatch.setenv(
        "GLASSHIVE_CAPABILITY_BROKER_OWNER_BINDINGS_JSON",
        json.dumps(
            [
                {
                    "glasshive_tenant_id": "glass-tenant",
                    "glasshive_owner_id": "owner-a",
                    "librechat_user_id": "user-a",
                    "proof": "operator_verified",
                },
                {
                    "glasshive_tenant_id": "glass-tenant",
                    "glasshive_owner_id": "owner-b",
                    "librechat_user_id": "user-b",
                    "proof": "operator_verified",
                },
            ]
        ),
    )
    parsed = capability_broker_config_from_environment()
    broker = GlassHiveCapabilityBroker(parsed)

    assert broker.principal_for_owner(tenant_id="glass-tenant", owner_id="owner-a") == "user-a"
    assert broker.principal_for_owner(tenant_id="glass-tenant", owner_id="owner-b") == "user-b"
    with pytest.raises(CapabilityBrokerError) as exc_info:
        broker.principal_for_owner(tenant_id="glass-tenant", owner_id="owner-c")
    assert exc_info.value.code == "owner_binding_required"


def test_shared_oidc_binding_scales_without_email_or_per_user_static_mapping(monkeypatch):
    monkeypatch.setenv(
        "GLASSHIVE_CAPABILITY_BROKER_ISSUER_URL",
        "https://librechat.example.test/api/viventium/glasshive/capabilities/direct",
    )
    monkeypatch.setenv("GLASSHIVE_CAPABILITY_BROKER_ISSUER_SECRET", SECRET)
    monkeypatch.setenv("GLASSHIVE_CAPABILITY_BROKER_TENANT_ID", "glass-tenant")
    monkeypatch.setenv(
        "GLASSHIVE_CAPABILITY_BROKER_IDENTITY_BINDING",
        "shared_oidc_subject",
    )
    parsed = capability_broker_config_from_environment()
    broker = GlassHiveCapabilityBroker(parsed, request=FakeCapabilityIssuer(), now=lambda: NOW)
    owner_a = "usr_0123456789abcdef0123456789abcdef"
    owner_b = "usr_fedcba9876543210fedcba9876543210"

    assert broker.principal_for_owner(tenant_id="glass-tenant", owner_id=owner_a) == owner_a
    assert broker.principal_for_owner(tenant_id="glass-tenant", owner_id=owner_b) == owner_b
    token = broker._assertion(  # noqa: SLF001 - exact signed boundary regression
        action="status",
        tenant_id="glass-tenant",
        owner_id=owner_a,
        execution_mode="docker",
    )
    claims = _decode_assertion(token)
    assert claims["binding_proof"] == "shared_oidc_subject"
    assert "email" not in claims
    with pytest.raises(CapabilityBrokerError) as exc_info:
        broker.principal_for_owner(tenant_id="other-tenant", owner_id=owner_a)
    assert exc_info.value.code == "owner_binding_required"
    with pytest.raises(CapabilityBrokerError):
        broker.principal_for_owner(tenant_id="glass-tenant", owner_id="email@example.test")


def test_shared_oidc_mode_prefers_exact_operator_binding_for_legacy_owner():
    canonical = "usr_0123456789abcdef0123456789abcdef"
    legacy = "mongo-legacy-user-id"
    mixed_config = CapabilityBrokerConfig(
        issuer_url="https://librechat.example.test/api/viventium/glasshive/capabilities/direct",
        secret=SECRET,
        broker_tenant_id="glass-tenant",
        owner_bindings=(
            CapabilityOwnerBinding(
                glasshive_tenant_id="glass-tenant",
                glasshive_owner_id=legacy,
                librechat_user_id="librechat-mongo-user-id",
                proof="operator_verified",
            ),
        ),
        identity_binding="shared_oidc_subject",
    )
    broker = GlassHiveCapabilityBroker(mixed_config, now=lambda: NOW)

    assert broker.binding_for_owner(tenant_id="glass-tenant", owner_id=legacy) == (
        "librechat-mongo-user-id",
        "operator_verified",
    )
    assert broker.binding_for_owner(tenant_id="glass-tenant", owner_id=canonical) == (
        canonical,
        "shared_oidc_subject",
    )
    legacy_claims = _decode_assertion(
        broker._assertion(  # noqa: SLF001 - exact signed boundary regression
            action="status",
            tenant_id="glass-tenant",
            owner_id=legacy,
            execution_mode="docker",
        )
    )
    assert legacy_claims["user_id"] == "librechat-mongo-user-id"
    assert legacy_claims["binding_proof"] == "operator_verified"

    with pytest.raises(CapabilityBrokerError) as exc_info:
        broker.principal_for_owner(tenant_id="glass-tenant", owner_id="unmapped-legacy-id")
    assert exc_info.value.code == "owner_binding_required"


def test_each_run_gets_a_fresh_bound_grant_and_revokes_without_mutating_workspace():
    issuer = FakeCapabilityIssuer()
    broker = GlassHiveCapabilityBroker(config(), request=issuer, now=lambda: NOW)
    original = {
        "worker_id": "worker-a",
        "bootstrap_bundle_json": json.dumps({"agents_md": "Persistent instructions"}),
    }
    seen_tokens: list[str] = []

    for run_id in ("run-ui", "run-mcp"):
        with broker.bind_run(
            tenant_id="glass-tenant",
            owner_id="owner-a",
            worker_id="worker-a",
            run_id=run_id,
            execution_mode="docker",
        ) as (bundle, readiness):
            projected = worker_with_ephemeral_capability_bundle(original, bundle)
            parsed = json.loads(projected["bootstrap_bundle_json"])
            seen_tokens.append(parsed["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"])
            assert parsed["agents_md"] == "Persistent instructions"
            assert readiness["status"] == "ready"

    assert seen_tokens == ["synthetic-run-token-1", "synthetic-run-token-2"]
    assert "GLASSHIVE_CAPABILITY_BROKER_TOKEN" not in original["bootstrap_bundle_json"]
    actions = [str(item["assertion"]["action"]) for item in issuer.requests]
    assert actions == ["grant", "revoke", "grant", "revoke"]
    assert [
        str(item["assertion"].get("run_id") or "")
        for item in issuer.requests
    ] == ["run-ui", "run-ui", "run-mcp", "run-mcp"]


class RecordingMissionRuntime:
    runtime_name = "codex-cli"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        self.calls.append(
            {
                "worker": dict(worker),
                "instruction": instruction,
                "run_id": run_id,
            }
        )
        return "ok"

    def resolve_model(self, _profile):
        return "synthetic-model"

    def terminate_worker(self, _worker):
        return RuntimeInfo("codex-cli", "", "", None, None, None, "", "", None)


class RecordingRunBinder:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    @contextmanager
    def bind_run(self, **kwargs):
        self.calls.append(dict(kwargs))
        yield {
            "env": {
                "GLASSHIVE_CAPABILITY_BROKER_TOKEN": f"token-{kwargs['run_id']}"
            }
        }, {"status": "ready", "connections": []}

    def revoke_active(self, **_kwargs):
        return None


def test_central_runtime_wrapper_covers_ui_and_direct_mcp_assign_without_persisting_token(tmp_path):
    profiled = ProfiledWorkerRuntime(base_dir=str(tmp_path))
    mission = RecordingMissionRuntime()
    binder = RecordingRunBinder()
    profiled._runtime_for_worker = lambda _worker: mission  # type: ignore[method-assign]
    profiled.capability_broker = binder  # type: ignore[assignment]
    worker = {
        "worker_id": "worker-a",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": "{}",
    }

    assert profiled.run_task(worker, "UI launch", run_id="run-ui") == "ok"
    assert profiled.run_task(worker, "MCP assign", run_id="run-mcp") == "ok"

    assert [call["run_id"] for call in binder.calls] == ["run-ui", "run-mcp"]
    assert [
        json.loads(str(call["worker"]["bootstrap_bundle_json"]))["env"][
            "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
        ]
        for call in mission.calls
    ] == ["token-run-ui", "token-run-mcp"]
    assert worker["bootstrap_bundle_json"] == "{}"


class RejectingMissionCapabilityBinder:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    @contextmanager
    def bind_run(self, **kwargs):
        self.calls.append(dict(kwargs))
        raise RuntimeError("mission capability issuer must not wrap direct conversation")
        yield  # pragma: no cover

    def revoke_active(self, **_kwargs):
        return None


def test_direct_conversation_reuses_existing_bundle_without_mission_capability_wrapper(tmp_path):
    profiled = ProfiledWorkerRuntime(base_dir=str(tmp_path))
    conversation = RecordingMissionRuntime()
    rejecting_binder = RejectingMissionCapabilityBinder()
    profiled._runtime_for_worker = lambda _worker: conversation  # type: ignore[method-assign]
    profiled.capability_broker = rejecting_binder  # type: ignore[assignment]
    existing_bundle = {
        "run_mode": "conversation",
        "env": {
            "VIVENTIUM_GLASSHIVE_CAPABILITY_GRANT": "existing-signed-conversation-grant"
        },
    }
    worker = {
        "worker_id": "worker-conversation",
        "owner_id": "librechat-mongo-user-id",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps(existing_bundle),
    }

    assert profiled.run_task(worker, "Direct conversation", run_id="run-conversation") == "ok"

    assert rejecting_binder.calls == []
    assert conversation.calls[0]["worker"]["bootstrap_bundle_json"] == json.dumps(existing_bundle)
    assert conversation.calls[0]["run_id"] == "run-conversation"


class RedactedStatusBroker:
    def status(self, **kwargs):
        assert kwargs["tenant_id"] == "local"
        assert kwargs["owner_id"] == "demo-owner"
        return {
            "status": "degraded",
            "reason": "",
            "connections": [
                {
                    "connection_id": "librechat:documents",
                    "label": "Documents",
                    "kind": "documents",
                    "adapter": "librechat_capability_broker",
                    "status": "action_required",
                }
            ],
        }


def test_connections_api_exposes_only_redacted_user_readiness(tmp_path):
    runtime = StubRuntime()
    runtime.capability_broker = RedactedStatusBroker()  # type: ignore[attr-defined]
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime=runtime))

    response = client.get("/v1/connections")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "connection_id": "librechat:documents",
                "label": "Documents",
                "kind": "documents",
                "adapter": "librechat_capability_broker",
                "status": "action_required",
            }
        ]
    }
    assert "token" not in response.text.lower()
