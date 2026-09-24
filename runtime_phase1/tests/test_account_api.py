from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import uuid

import pytest
from fastapi.testclient import TestClient

import workers_projects_runtime.api as api_module
import workers_projects_runtime.service as service_module
from workers_projects_runtime.api import create_app
from workers_projects_runtime.service import (
    ParallelExecutionIsolationError,
    WorkersProjectsService,
)
from workers_projects_runtime.service_assertions import verify_service_assertion
from workers_projects_runtime.signed_links import sign_link_params
from workers_projects_runtime.openclaw_runtime import HostCapacityError, StubRuntime
from workers_projects_runtime.run_evidence import build_constraint_ledger
from workers_projects_runtime.store import Store


API_TOKEN = "test-glasshive-service-token"
ASSERTION_SECRET = "test-service-assertion-secret"
ASSERTION_AUDIENCE = "glasshive-account-api"


def _healthy_capability_producers() -> dict[str, object]:
    return {
        "readinessScope": {
            "contractVersion": 1,
            "scope": "deployment",
            "ownerCredentialRole": "transport_auth",
        },
        "storagePressure": {
            "version": 1,
            "state": "healthy",
            "healthy": True,
            "usedPercent": 50.0,
            "availableBytes": 50,
            "thresholdPercent": 90.0,
        },
        "promptLayers": {
            "contractVersion": 1,
            "producerScope": "glasshive.worker_prompt_registry",
            "unknownLayerNames": [],
        },
        "workTraceContract": WorkersProjectsService.work_trace_contract_capability(),
    }


def _expected_worker_prompt_trace() -> dict[str, object]:
    return {
        "contractVersion": 1,
        "producerScope": "glasshive.worker_prompt_registry",
        "layerNames": sorted(service_module.WORKER_PROMPT_LAYER_PRODUCERS),
        "unknownLayerNames": [],
    }


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def service_assertion(
    *,
    tenant_id: str = "tenant-a",
    owner_id: str = "owner-a",
    nonce: str | None = None,
    now: int | None = None,
    ttl: int = 60,
    aud: str = ASSERTION_AUDIENCE,
    canonical: bool = True,
    secret: str = ASSERTION_SECRET,
    omit: str | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    claims: dict[str, object] = {
        "v": 1,
        "aud": aud,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "iat": issued_at,
        "exp": issued_at + ttl,
        "nonce": nonce or f"nonce_{uuid.uuid4().hex}",
    }
    if omit:
        claims.pop(omit)
    if canonical:
        encoded_json = json.dumps(
            claims,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    else:
        encoded_json = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    payload = _b64url(encoded_json)
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64url(signature)}"


def account_headers(
    *,
    tenant_id: str = "tenant-a",
    owner_id: str = "owner-a",
    nonce: str | None = None,
    assertion: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "X-Viventium-Service-Assertion": assertion
        or service_assertion(tenant_id=tenant_id, owner_id=owner_id, nonce=nonce),
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _truthfully_invoke_run(store: Store, run_id: str, *, suffix: str) -> dict:
    run = store.get_run(run_id)
    assert run is not None and run["state"] == "queued"
    worker = store.get_worker(run["worker_id"])
    assert worker is not None
    existing_lease = store.get_active_host_run_lease_for_run(run_id)
    executor_id = str(
        (existing_lease or {}).get("executor_id") or f"account-test-{suffix}"
    )
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=executor_id
    )
    assert claimed is not None
    lease = store.get_active_host_run_lease_for_run(run_id)
    if lease is None:
        lease = store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker["owner_id"]),
            worker_id=str(worker["worker_id"]),
            run_id=run_id,
            executor_id=executor_id,
            conversation_limit=2,
            mission_limit=64,
            account_mission_limit=64,
            tenant_mission_limit=64,
            lease_ttl_s=300,
        )
    admitted = store.admit_claimed_run(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=executor_id,
    )
    assert admitted is not None
    invoked = store.mark_run_runtime_invoked(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=executor_id,
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=run_id,
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=executor_id,
        identity_kind="in_process",
        pid=None,
        process_group=None,
        process_start_identity="",
        container_id="",
        session_id="in-process",
    )
    assert confirmed is not None
    store.update_worker_state(worker["worker_id"], "running", last_error="")
    return confirmed["run"]


def _exact_terminal_generation(store: Store, run_id: str) -> dict[str, str]:
    run = store.get_run(run_id)
    assert run is not None
    return {
        **(store.get_run_retry_generation(run_id) or {}),
        "expected_runtime_invoked_at": str(run.get("runtime_invoked_at") or ""),
    }


def _truthfully_pause_run(store: Store, run_id: str, *, suffix: str) -> dict:
    invoked = _truthfully_invoke_run(store, run_id, suffix=suffix)
    paused = store.transition_run_if_state(
        run_id,
        "running",
        "paused",
        ended_at=None,
        error_text="Paused",
    )
    assert paused is not None
    store.update_worker_state(invoked["worker_id"], "paused", last_error="")
    return paused


def delegation_payload(*, title: str = "Research alpha", instruction: str = "Research alpha deeply") -> dict:
    return {
        "title": title,
        "goal": f"Complete {title}",
        "instruction": instruction,
        "profile": "codex-cli",
        "executionMode": "docker",
        "workerName": f"{title} worker",
        "workerRole": "General intelligent worker",
        "originSurface": "telegram",
        "bootstrapBundle": {
            "context": {"private_path": "/private/example", "secret": "must-not-leak"}
        },
    }


def delegation_payload_with_origin(
    *,
    origin_ref: str = "ghi_synthetic_origin_0001",
    title: str = "Research alpha",
) -> dict:
    payload = delegation_payload(title=title)
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "callbacks": {
            "origin_ref": origin_ref,
            "events_webhook_url": "https://callback.example.invalid/events",
        },
    }
    return payload


def conversation_orchestrator_payload(*, title: str = "Isolated parallel mission") -> dict:
    payload = delegation_payload(title=title)
    broker_url = "http://host.docker.internal:3080/api/viventium/glasshive/capabilities/mcp"
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
            "fallback_worker_profile": "claude-code",
        },
        "glasshive_capability_broker": {
            "version": 1,
            "status": "pending_admission",
            "name": "glasshive-user-capabilities",
            "url": broker_url,
            "allowed_servers": [],
            "allowed_host_tools": [],
            "scopes": {"content_read": False},
            "projection": "all_user_enabled_policy_gated",
        },
        "claude_project_mcp": {
            "glasshive-user-capabilities": {
                "type": "http",
                "transport": "http",
                "url": broker_url,
                "headers": {
                    "Authorization": "Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}"
                },
            }
        },
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            f'url = "{broker_url}"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {},
    }
    return payload


@pytest.fixture
def account_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "8")
    monkeypatch.setenv("WPR_HOST_ACCOUNT_ACTIVE_LIMIT", "8")
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: service_module.shutil._ntuple_diskusage(100, 50, 50),
    )
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: service_module.HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )
    app = create_app(
        db_path=str(tmp_path / "account-api.sqlite3"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    app.state.service.runtime.isolated_parallel_readiness = lambda: {
        "ready": True,
        "reason": "",
    }
    # Mission acceptance must not wait for provider startup. Keep rows queued so
    # each API assertion can inspect the durable reservation deterministically.
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {API_TOKEN}"},
        {
            "Authorization": "Bearer wrong-token",
            "X-Viventium-Service-Assertion": service_assertion(),
        },
    ],
)
def test_account_api_requires_bearer_and_service_assertion(account_client, headers):
    response = account_client.get("/v1/active-work", headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "assertion",
    [
        service_assertion(secret="wrong-secret"),
        service_assertion(aud="wrong-audience"),
        service_assertion(ttl=61),
        service_assertion(now=int(time.time()) - 61, ttl=60),
        service_assertion(omit="owner_id"),
        service_assertion(canonical=False),
    ],
)
def test_account_api_rejects_invalid_expired_or_noncanonical_assertions(account_client, assertion):
    response = account_client.get(
        "/v1/active-work",
        headers=account_headers(assertion=assertion),
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"].startswith("service_assertion_")


def test_mutating_service_assertion_nonce_is_durable_and_one_use(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    db_path = str(tmp_path / "durable-replay.sqlite3")
    fixed = service_assertion(nonce="nonce_durable_replay")
    headers = account_headers(assertion=fixed, idempotency_key="delegation-durable-replay")

    first_app = create_app(db_path=db_path, runtime_backend="stub", runtime=StubRuntime())
    first_app.state.service.start_assigned_run = lambda _worker_id: None
    first_app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(first_app) as first_client:
        first = first_client.post("/v1/delegations", headers=headers, json=delegation_payload())
        assert first.status_code == 202

    second_app = create_app(db_path=db_path, runtime_backend="stub", runtime=StubRuntime())
    second_app.state.service.start_assigned_run = lambda _worker_id: None
    second_app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(second_app) as second_client:
        replay = second_client.post("/v1/delegations", headers=headers, json=delegation_payload())

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "service_assertion_replayed"


def test_delegation_atomically_reserves_one_project_worker_and_run(account_client):
    headers = account_headers(idempotency_key="delegation-alpha")

    response = account_client.post("/v1/delegations", headers=headers, json=delegation_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["workRef"].startswith("work_")
    assert body["state"] == "accepted"
    assert body["actions"] == ["queue", "message", "steer", "pause", "stop"]
    assert body["idempotentReplay"] is False
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        run = conn.execute(
            """
            SELECT state, claimed_at, admitted_at, runtime_invoked_at
            FROM runs
            """
        ).fetchone()
        assert run == ("queued", None, None, None)
        assert conn.execute(
            "SELECT COUNT(*) FROM host_run_leases WHERE status = 'active'"
        ).fetchone()[0] == 1


def test_dead_lease_reconciliation_keeps_unproved_generation_fenced_before_api_read(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="dead-lease-running-api"),
        json=delegation_payload(title="Dead lease running API"),
    )
    assert accepted.status_code == 202, accepted.text
    store = account_client.app.state.store
    service = account_client.app.state.service
    work_ref = accepted.json()["workRef"]
    record = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    running = _truthfully_invoke_run(
        store, str(record["current_run_id"]), suffix="dead-lease-api"
    )
    attempt_id = str(running["active_attempt_id"])
    lease = store.get_active_host_run_lease_for_run(str(running["run_id"]))
    assert lease is not None
    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
            (expired_at, expired_at, lease["lease_id"]),
        )

    reconciled = service.reconcile_host_run_leases(stale_after_s=0)
    detail = account_client.get(
        f"/v1/work/{work_ref}", headers=account_headers()
    )

    assert reconciled == {"renewed": 0, "released": 0, "unchanged": 1}
    assert detail.status_code == 200, detail.text
    assert detail.json()["state"] == "running"
    durable = store.get_run(str(running["run_id"]))
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["runtime_invoked_at"] == running["runtime_invoked_at"]
    assert durable["active_attempt_id"] == attempt_id
    attempts = store.list_run_attempts(str(running["run_id"]))
    fenced_attempt = next(item for item in attempts if item["attempt_id"] == attempt_id)
    assert fenced_attempt["state"] == "running"
    assert fenced_attempt["ended_at"] is None
    assert fenced_attempt["runtime_invoked_at"] == running["runtime_invoked_at"]
    assert (store.get_host_run_lease(str(lease["lease_id"])) or {})["status"] == "active"


def test_conversation_orchestrator_delegation_rejects_host_execution(account_client):
    payload = delegation_payload(title="Isolated parallel mission")
    payload["executionMode"] = "host"
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        },
    }

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-host-must-be-isolated"),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 0



def native_orchestrator_payload(title):
    payload = delegation_payload(title=title)
    payload["executionMode"] = "host"
    payload["bootstrapBundle"] = {
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "host",
            "fallback_worker_profile": "claude-code",
        },
    }
    return payload


def enable_native_orchestration(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_PARALLEL_WORK_EXECUTION_MODE", "host")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS", "true")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", raising=False)


def test_native_parallel_roots_preserve_siblings_and_exact_owner_controls(account_client, monkeypatch):
    enable_native_orchestration(monkeypatch)
    store = account_client.app.state.store
    work = []
    for title in ("Registration preparation", "Research summary"):
        response = account_client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=title.replace(" ", "-")),
            json=native_orchestrator_payload(title),
        )
        assert response.status_code == 202, response.text
        work.append(response.json()["workRef"])
    first, sibling = [store.get_delegation(ref, tenant_id="tenant-a", owner_id="owner-a") for ref in work]
    assert first["worker_id"] != sibling["worker_id"]
    assert first["project_id"] != sibling["project_id"]
    assert store.get_worker(first["worker_id"])["execution_mode"] == "host"
    sibling_run = store.get_run(sibling["current_run_id"])
    wrong_owner = account_client.post(
        f"/v1/work/{work[0]}/actions",
        headers=account_headers(owner_id="owner-b"),
        json={"action": "steer", "instruction": "Replace the goal", "idempotencyKey": "wrong-owner"},
    )
    assert wrong_owner.status_code == 404
    steer = account_client.post(
        f"/v1/work/{work[0]}/actions",
        headers=account_headers(),
        json={"action": "steer", "instruction": "Use the public admission category and stop before payment", "idempotencyKey": "steer-first"},
    )
    assert steer.status_code == 202, steer.text
    current = store.get_delegation(work[0], tenant_id="tenant-a", owner_id="owner-a")
    assert current["worker_id"] == first["worker_id"]
    assert current["current_run_id"] != first["current_run_id"]
    assert store.get_delegation(work[1], tenant_id="tenant-a", owner_id="owner-a") == sibling
    assert store.get_run(sibling["current_run_id"]) == sibling_run
    assert store.get_worker(first["worker_id"])["trusted_run_lane"] == "mission"
    service = account_client.app.state.service
    first_worker = store.get_worker(first["worker_id"])
    assert service._host_mutation_scope(first_worker) == ""
    assert service._host_mutation_scope({**first_worker, "owner_id": "owner-b"})
    assert service._host_mutation_scope({**first_worker, "worker_id": "manual-spoofed-worker"})
    assert service._trusted_parallel_fallback_profile(first_worker, preflight=False) == "claude-code"
    assert service._trusted_parallel_fallback_profile({**first_worker, "worker_id": "manual-spoofed-worker"}, preflight=False) == ""



@pytest.mark.parametrize("disabled_setting", ["GLASSHIVE_HOST_WORKERS_ENABLED", "GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS"])
def test_native_parallel_requires_existing_host_authority(account_client, monkeypatch, disabled_setting):
    enable_native_orchestration(monkeypatch)
    monkeypatch.setenv(disabled_setting, "false")
    response = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="unauthorized-native"),
        json=native_orchestrator_payload("Use the local desktop"),
    )
    assert response.status_code == 409, response.text
    assert account_client.app.state.store.list_all_workers() == []



def test_native_parallel_revoked_full_access_blocks_resume_but_focused_does_not(account_client, monkeypatch):
    enable_native_orchestration(monkeypatch)
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="revocation-lifecycle"),
        json=native_orchestrator_payload("Prepare a local document"),
    )
    assert accepted.status_code == 202, accepted.text
    work_ref = accepted.json()["workRef"]
    pause = account_client.post(
        f"/v1/work/{work_ref}/actions", headers=account_headers(),
        json={"action": "pause", "idempotencyKey": "pause-before-revocation"},
    )
    assert pause.status_code == 202, pause.text
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS", "false")
    rejected = account_client.post(
        f"/v1/work/{work_ref}/actions", headers=account_headers(),
        json={"action": "resume", "idempotencyKey": "revoked-resume"},
    )
    assert rejected.status_code == 409, rejected.text
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS", "true")
    monkeypatch.setenv("VIVENTIUM_PARALLEL_WORK_DEFAULT_MODE", "focused")
    resumed = account_client.post(
        f"/v1/work/{work_ref}/actions", headers=account_headers(),
        json={"action": "resume", "idempotencyKey": "focused-resume"},
    )
    assert resumed.status_code == 202, resumed.text


def test_native_parallel_rejects_unsigned_launch_before_rows(account_client, monkeypatch):
    enable_native_orchestration(monkeypatch)
    response = account_client.post(
        "/v1/delegations",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        json=native_orchestrator_payload("Use the local desktop"),
    )
    assert response.status_code == 401
    assert account_client.app.state.store.list_all_workers() == []


def test_conversation_orchestrator_derives_server_owned_clean_room_policy(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-clean-room-derived"),
        json=conversation_orchestrator_payload(),
    )

    assert accepted.status_code == 202
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        worker = conn.execute(
            "SELECT bootstrap_profile, bootstrap_bundle_json, execution_mode FROM workers"
        ).fetchone()
    assert worker is not None
    assert worker["execution_mode"] == "docker"
    assert worker["bootstrap_profile"] == "clean-room"
    persisted_bundle = json.loads(worker["bootstrap_bundle_json"])
    assert persisted_bundle["execution_policy"] == "parallel-clean-room-v1"
    assert persisted_bundle["viventium_launch_authority"]["fallback_worker_profile"] == "claude-code"
    assert persisted_bundle["env"] == {}
    assert set(persisted_bundle["claude_project_mcp"]) == {
        "glasshive-user-capabilities"
    }


def test_conversation_orchestrator_persists_only_allowlisted_clean_room_context(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    payload = conversation_orchestrator_payload(title="Strict clean-room context")
    payload["bootstrapBundle"].update(
        {
            "raw_chat": "private chat must not persist",
            "hostname": "private-host.invalid",
            "run_id": "run_private_host_identifier",
            "context": {
                "raw_chat": "private nested chat",
                "hostname": "private-host.invalid",
                "run_id": "run_private_host_identifier",
            },
        }
    )
    payload["bootstrapBundle"]["glasshive_capability_broker"]["run_id"] = (
        "run_private_host_identifier"
    )
    payload["bootstrapBundle"]["glasshive_capability_broker"][
        "allowed_servers"
    ] = [
        "safe-server",
        {
            "name": "nested-server",
            "raw_chat": "private nested broker chat",
            "hostname": "private-host.invalid",
            "run_id": "run_private_host_identifier",
        },
    ]

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="strict-clean-room-context"),
        json=payload,
    )

    assert accepted.status_code == 202, accepted.text
    store = account_client.app.state.store
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        worker = conn.execute("SELECT * FROM workers").fetchone()
        persisted_raw = conn.execute(
            "SELECT bootstrap_bundle_json FROM workers"
        ).fetchone()[0]
    persisted = json.loads(persisted_raw)
    assert "context" not in persisted
    assert "raw_chat" not in persisted
    assert "hostname" not in persisted
    assert "run_id" not in persisted
    assert "run_id" not in persisted["glasshive_capability_broker"]
    assert "private chat" not in persisted_raw
    assert "private-host" not in persisted_raw
    assert "run_private_host_identifier" not in persisted_raw
    assert persisted["glasshive_capability_broker"]["allowed_servers"] == [
        "safe-server"
    ]

    account_client.app.state.service.assign_run(
        str(worker["worker_id"]),
        "Follow-up clean-room assignment",
        runtime_bundle={
            "glasshive_capability_broker": {
                "allowed_servers": [
                    "safe-server",
                    {
                        "name": "later-nested-server",
                        "raw_chat": "later private broker chat",
                        "hostname": "later-private-host.invalid",
                        "run_id": "run_later_private_identifier",
                    },
                ]
            }
        },
        start_processor=False,
    )
    assigned_worker = store.get_worker(str(worker["worker_id"]))
    assigned_raw = str(assigned_worker["bootstrap_bundle_json"])
    assigned = json.loads(assigned_raw)
    assert assigned["glasshive_capability_broker"]["allowed_servers"] == [
        "safe-server"
    ]
    assert "later private" not in assigned_raw
    assert "run_later_private_identifier" not in assigned_raw


def test_work_get_does_not_repair_mismatched_lease_and_explicit_reconciler_does(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="attempt-lease-mismatch"),
        json=delegation_payload(title="Attempt lease mismatch"),
    )
    assert accepted.status_code == 202, accepted.text
    store = account_client.app.state.store
    work_ref = accepted.json()["workRef"]
    record = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    running = _truthfully_invoke_run(
        store, str(record["current_run_id"]), suffix="attempt-lease-mismatch"
    )
    attempt_id = str(running["active_attempt_id"])
    lease = store.get_active_host_run_lease_for_run(str(running["run_id"]))
    assert lease is not None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE run_attempts SET lease_id = ? WHERE attempt_id = ?",
            ("hrl_mismatched_generation", attempt_id),
        )

    detail = account_client.get(
        f"/v1/work/{work_ref}", headers=account_headers()
    )

    assert detail.status_code == 200, detail.text
    assert detail.json()["state"] == "running"
    assert store.get_run(str(running["run_id"]))["state"] == "running"

    assert store.reconcile_invalid_running_runs() == 1
    durable = store.get_run(str(running["run_id"]))
    assert durable is not None
    assert durable["state"] == "queued"
    assert durable["runtime_invoked_at"] is None
    repaired_attempt = store.get_run_attempt(attempt_id)
    assert repaired_attempt is not None
    assert repaired_attempt["state"] == "retry_queued"
    assert repaired_attempt["runtime_invoked_at"] == running["runtime_invoked_at"]
    assert (store.get_host_run_lease(str(lease["lease_id"])) or {})["status"] == "released"


@pytest.mark.parametrize(
    ("env_key", "env_value"),
    [
        ("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh"),
        ("WPR_CLAUDE_CODE_EFFORT", "max"),
    ],
)
def test_conversation_orchestrator_accepts_only_bounded_nonsecret_effort_preferences(
    account_client, monkeypatch, env_key, env_value
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    payload = conversation_orchestrator_payload(title="Bounded effort preference")
    payload["bootstrapBundle"]["env"] = {env_key: env_value}

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(
            idempotency_key=f"parallel-effort-{env_key.lower()}"
        ),
        json=payload,
    )

    assert accepted.status_code == 202
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        (persisted_bundle_json,) = conn.execute(
            "SELECT bootstrap_bundle_json FROM workers"
        ).fetchone()
    assert json.loads(persisted_bundle_json)["env"] == {env_key: env_value}


def test_parallel_clean_room_policy_cannot_be_replaced_by_a_later_runtime_bundle(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-clean-room-immutable"),
        json=conversation_orchestrator_payload(title="Immutable clean room"),
    )
    assert accepted.status_code == 202
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )

    with pytest.raises(ParallelExecutionIsolationError, match="immutable"):
        account_client.app.state.service.assign_run(
            delegation["worker_id"],
            "Synthetic follow-up",
            runtime_bundle={"execution_policy": "host-login-v1"},
            start_processor=False,
        )

    worker = store.get_worker(delegation["worker_id"])
    assert json.loads(worker["bootstrap_bundle_json"])["execution_policy"] == (
        "parallel-clean-room-v1"
    )
    assert len(store.list_runs_for_worker(delegation["worker_id"])) == 1


@pytest.mark.parametrize(
    ("bootstrap_profile", "bundle_update"),
    [
        ("codex-host", {}),
        ("claude-host", {}),
        ("host-login", {}),
        ("full-local", {}),
        (
            None,
            {
                "files": [
                    {
                        "scope": "home",
                        "path": ".codex/auth.json",
                        "content": "synthetic-host-auth",
                    }
                ]
            },
        ),
        (
            None,
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": ".mcp.json",
                        "content": '{"mcpServers":{"caller":{"command":"unsafe"}}}',
                    }
                ]
            },
        ),
        (
            None,
            {"metadata": {"provider": {"api_key": "synthetic-provider-key"}}},
        ),
        (None, {"env": {"OPENAI_API_KEY": "synthetic-caller-provider-key"}}),
        (None, {"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "impossible"}}),
        (None, {"env": {"WPR_CLAUDE_CODE_EFFORT": "max "}}),
        (None, {"provider_credentials": {"api_key": "synthetic-provider-key"}}),
        (
            None,
            {
                "glasshive_capability_broker": {
                    "version": 1,
                    "status": "pending_admission",
                    "name": "glasshive-user-capabilities",
                    "url": "http://host.docker.internal:3080/api/viventium/glasshive/capabilities/mcp",
                    "grant_token": "synthetic-caller-broker-grant",
                },
                "claude_project_mcp": None,
                "codex_config_append": None,
            },
        ),
        (None, {"claude_settings_local": {"permissions": {"allow": ["*"]}}}),
        (
            None,
            {
                "claude_project_mcp": {
                    "caller-mcp": {"command": "synthetic-untrusted-command"}
                }
            },
        ),
        (
            None,
            {
                "codex_config_append": (
                    "[mcp_servers.caller-mcp]\n"
                    'command = "synthetic-untrusted-command"'
                )
            },
        ),
        (None, {"execution_policy": "parallel-clean-room-v1"}),
    ],
    ids=[
        "codex-host-profile",
        "claude-host-profile",
        "host-login-profile",
        "full-local-profile",
        "home-scoped-file",
        "workspace-authority-file",
        "nested-provider-credentials",
        "caller-env",
        "unsupported-codex-effort",
        "noncanonical-claude-effort",
        "provider-credentials",
        "caller-broker-grant",
        "claude-settings",
        "caller-claude-mcp",
        "caller-codex-mcp",
        "caller-execution-policy",
    ],
)
def test_conversation_orchestrator_rejects_non_clean_room_authority_before_rows(
    account_client,
    monkeypatch,
    bootstrap_profile,
    bundle_update,
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    payload = conversation_orchestrator_payload(title="Reject unsafe bootstrap")
    payload["bootstrapBundle"].update(bundle_update)
    if bootstrap_profile is not None:
        payload["bootstrapProfile"] = bootstrap_profile

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(
            idempotency_key=f"parallel-reject-{uuid.uuid4().hex}"
        ),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    if bundle_update == {"env": {"OPENAI_API_KEY": "synthetic-caller-provider-key"}}:
        assert rejected.json()["detail"]["reason"] == "caller_provider_credentials"
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        for table in (
            "delegations",
            "projects",
            "workers",
            "runs",
            "events",
            "callback_outbox",
            "host_run_leases",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_conversation_orchestrator_launch_fails_closed_when_isolation_policy_is_off(
    account_client, monkeypatch
):
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", raising=False)
    payload = delegation_payload(title="Policy must be live")
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        },
    }

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-policy-must-be-live"),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    assert account_client.app.state.store.list_all_workers() == []


def test_conversation_orchestrator_launch_fails_closed_while_a_host_mission_exists(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = account_client.app.state.store
    project = store.create_project(
        "owner-legacy", "Existing host mission", "Finish first", "codex-cli"
    )
    host_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-legacy",
        name="Existing host mission",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    store.create_run(host_worker["worker_id"], project["project_id"], "Still active")
    payload = delegation_payload(title="Blocked until host exits")
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        },
    }

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-existing-host-blocks"),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    assert len(store.list_all_workers()) == 1


def test_orchestration_capabilities_are_service_asserted_and_report_global_host_gate(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")

    unauthenticated = account_client.get("/v1/orchestration-capabilities")
    ready = account_client.get(
        "/v1/orchestration-capabilities", headers=account_headers()
    )

    assert unauthenticated.status_code == 401
    assert ready.status_code == 200
    assert ready.json() == {
        "policyVersion": 1,
        "isolatedParallelReady": True,
        "isolatedParallelReason": "",
        "hostMissionsAllowed": False,
        "hostMissionsActive": 0,
        "nativeParallelReady": False,
        "nativeParallelReason": "native_parallel_not_authorized",
        "sharedHostDesktop": False,
        **_healthy_capability_producers(),
    }


def test_orchestration_capabilities_preserve_structured_isolation_failure_reason(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    account_client.app.state.service.runtime.isolated_parallel_readiness = lambda **_kwargs: {
        "ready": False,
        "reason": "parallel_clean_room_network_unconfigured",
    }

    response = account_client.get(
        "/v1/orchestration-capabilities", headers=account_headers()
    )

    assert response.status_code == 200
    assert response.json() == {
        "policyVersion": 1,
        "isolatedParallelReady": False,
        "isolatedParallelReason": "parallel_clean_room_network_unconfigured",
        "hostMissionsAllowed": False,
        "hostMissionsActive": 0,
        "nativeParallelReady": False,
        "nativeParallelReason": "native_parallel_not_authorized",
        "sharedHostDesktop": False,
        **_healthy_capability_producers(),
    }


def test_orchestration_capability_scope_is_owner_invariant_and_not_caller_overridable(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")

    owner_a = account_client.get(
        "/v1/orchestration-capabilities?scope=owner&ownerCredentialRole=authority",
        headers=account_headers(owner_id="owner-a"),
    )
    owner_b = account_client.get(
        "/v1/orchestration-capabilities?scope=request&ownerCredentialRole=policy",
        headers=account_headers(owner_id="owner-b"),
    )

    assert owner_a.status_code == 200
    assert owner_b.status_code == 200
    assert owner_a.json() == owner_b.json()
    assert owner_a.json()["readinessScope"] == {
        "contractVersion": 1,
        "scope": "deployment",
        "ownerCredentialRole": "transport_auth",
    }


def test_delegation_capacity_shortage_accepts_durable_queued_work(
    account_client,
):
    available_memory = int(4.3 * 1024**3)
    runtime = account_client.app.state.service.runtime
    runtime.isolated_resource_usage = lambda **_kwargs: {
        "child_processes": 0,
        "threads": 0,
        "available_memory_bytes": available_memory,
        "available_disk_bytes": 64 * 1024**3,
        "running_worker_containers": 0,
        "running_worker_ids": [],
        "worker_process_counts": {},
        "process_probe_ok": True,
        "memory_probe_ok": True,
        "disk_probe_ok": True,
    }

    response = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="capacity-api-blocked"),
        json=delegation_payload(title="Capacity API blocked"),
    )

    assert response.status_code == 202, response.text
    accepted = response.json()
    store = account_client.app.state.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    assert store.get_run(record["current_run_id"])["state"] == "queued"
    with store._connect() as conn:
        assert {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("projects", "workers", "runs", "delegations", "host_run_leases")} == {
            "projects": 1, "workers": 1, "runs": 1, "delegations": 1, "host_run_leases": 0,
        }


def test_delegation_returns_202_only_after_capacity_reservation_and_replays_stably(
    account_client,
):
    runtime = account_client.app.state.service.runtime
    available_memory = 16 * 1024**3

    def usage(**_kwargs):
        return {
            "child_processes": 0,
            "threads": 0,
            "available_memory_bytes": available_memory,
            "available_disk_bytes": 64 * 1024**3,
            "running_worker_containers": 0,
            "running_worker_ids": [],
            "worker_process_counts": {},
            "process_probe_ok": True,
            "memory_probe_ok": True,
            "disk_probe_ok": True,
        }

    runtime.isolated_resource_usage = usage
    payload = delegation_payload(title="Capacity API accepted")
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="capacity-api-accepted"),
        json=payload,
    )
    assert accepted.status_code == 202
    work_ref = accepted.json()["workRef"]
    store = account_client.app.state.store
    record = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    assert record is not None
    lease = store.get_active_host_run_lease_for_run(record["initial_run_id"])
    assert lease is not None

    available_memory = 1024**3
    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="capacity-api-accepted"),
        json=payload,
    )
    assert replay.status_code == 202
    assert replay.json()["idempotentReplay"] is True
    assert replay.json()["workRef"] == work_ref


def test_work_detail_exposes_bounded_redacted_immutable_trace_and_artifact_refs(
    account_client, tmp_path
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="work-detail-trace"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_work_detail_trace_0001",
            title="Work detail trace",
        ),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = record["current_run_id"]
    worker_id = record["worker_id"]
    now = datetime.now(timezone.utc).isoformat()
    for _index in range(15):
        claimed = store.claim_next_queued_run(worker_id)
        assert claimed is not None
        lease = store.get_active_host_run_lease_for_run(run_id)
        if lease is not None:
            store.release_host_run_lease(
                lease["lease_id"], executor_id=None, reason="synthetic_capacity_wait"
            )
        store.requeue_run_for_retry(
            run_id,
            retry_after=now,
            **(store.get_run_retry_generation(run_id) or {}),
            error_text="secret /private/path token=abc",
            last_retry_class="host_capacity",
            consume_retry_budget=False,
            capacity_class="resource_pressure",
            capacity_available={"memoryBytes": 4 * 1024**3},
            capacity_required={"memoryBytes": 5 * 1024**3},
            capacity_shortage={"memoryBytes": 1024**3},
            capacity_next_retry_at=now,
            failure_class="host_capacity",
            failure_retryable=1,
            failure_structured=1,
        )
    store.insert_callback_outbox_once(
        callback_id="cb_work_detail_trace",
        project_id=record["project_id"],
        worker_id=worker_id,
        run_id=run_id,
        attempt_number=15,
        event_type="run.queue_status",
        url="https://callback.example.invalid/events",
        payload_json=json.dumps(
            {
                "callback_ts": datetime.now(timezone.utc).timestamp(),
                "attempt_number": 15,
                "secret": "must-not-project",
            }
        ),
    )
    workspace = tmp_path / "artifact-workspace"
    artifact = workspace / "artifacts" / "final.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body>Synthetic artifact</body></html>", encoding="utf-8")
    store.update_worker(worker_id, workspace_dir=str(workspace))
    store.record_artifact_trace(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
        artifact_refs=service_module.work_artifact_observation(
            store.get_worker(worker_id),
            store.get_run(run_id),
            output_text="FINAL REPORT:\nartifacts/final.html",
            error_text="",
        ),
    )

    visible = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    hidden = account_client.get(
        f"/v1/work/{accepted['workRef']}",
        headers=account_headers(owner_id="owner-b"),
    )

    assert visible.status_code == 200
    body = visible.json()
    assert len(body["attemptHistory"]) == 15
    assert body["attemptHistoryOverflowCount"] == 0
    assert len(body["capacityAttempts"]) == 1
    assert body["capacityAttemptOverflowCount"] == 0
    assert body["callbackDeliveries"][0]["status"] in {
        "pending",
        "delivering",
        "dead_lettered",
    }
    assert "promptLayers" not in body
    assert body["traceability"]["origin"] == {
        "originRef": "origin_sha256:"
        + hashlib.sha256(b"ghi_work_detail_trace_0001").hexdigest(),
        "sourceEventRef": None,
        "sourceRevision": 1,
        "surface": "telegram",
    }
    assert body["traceability"]["promptLayers"] == _expected_worker_prompt_trace()
    assert body["traceability"]["contractVersion"] == 2
    assert body["traceability"]["runtimeInvocations"] == []
    assert body["traceability"]["providerAuthorizationPreflights"] == []
    assert body["traceability"]["integrity"]["algorithm"] == "sha256-chain-v1"
    assert body["traceability"]["integrity"]["eventCount"] >= 36
    assert body["traceability"]["integrity"]["headSha256"].startswith("sha256:")
    assert body["traceability"]["integrity"]["headSha256"].startswith("sha256:")
    assert body["runRef"] == "run_sha256:" + hashlib.sha256(
        run_id.encode("utf-8")
    ).hexdigest()
    assert body["artifactRefs"]["available"] is True
    assert body["artifactRefs"]["refs"][0]["artifactRef"].startswith(
        "artifact_sha256:"
    )
    assert hidden.status_code == 404
    encoded = json.dumps(body)
    assert "/private" not in encoded
    assert str(tmp_path) not in encoded
    assert "must-not-project" not in encoded
    assert "lease_id" not in encoded


def test_work_detail_freezes_queue_age_after_invocation_terminal_and_reload(
    account_client, monkeypatch
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="work-detail-frozen-queue"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_work_detail_frozen_queue_0001",
            title="Frozen queue history",
        ),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = str(record["current_run_id"])
    claimed = store.claim_next_queued_run(record["worker_id"])
    lease = store.get_active_host_run_lease_for_run(run_id)
    assert claimed is not None and lease is not None
    admitted = store.admit_claimed_run(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=str(lease["executor_id"]),
    )
    preflight = store.record_provider_authorization_preflight(
        run_id,
        provider="openai",
        status="authorized",
        failure_class="",
    )
    invoked = store.mark_run_runtime_invoked(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=str(lease["executor_id"]),
    )
    assert admitted is not None and preflight is not None and invoked is not None

    class FirstRead(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    class LaterRead(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(api_module, "datetime", FirstRead)
    first = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    ).json()["queue"]
    monkeypatch.setattr(api_module, "datetime", LaterRead)
    later = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    ).json()["queue"]

    assert first == later
    assert first["waitOpen"] is False
    assert isinstance(first["durationSeconds"], int)
    assert first["lastBlocker"] == {"class": "admission_pending"}
    assert "ageSeconds" not in first
    assert "nextRetryAt" not in first
    assert "timeoutAt" not in first

    terminal = store.finalize_run_if_state(
        run_id,
        "running",
        "completed",
        output_text="Synthetic completion",
        **_exact_terminal_generation(store, run_id),
    )
    assert terminal is not None
    db_path = str(store.db_path)
    reloaded_app = create_app(
        db_path=db_path, runtime_backend="stub", runtime=StubRuntime()
    )
    reloaded_app.state.service.start_assigned_run = lambda _worker_id: None
    reloaded_app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(reloaded_app) as reloaded_client:
        reloaded = reloaded_client.get(
            f"/v1/work/{accepted['workRef']}", headers=account_headers()
        ).json()["queue"]

    assert reloaded == first


def test_viventium_callback_persists_exact_attempt_identity_and_stable_replay_timestamp(
    account_client, monkeypatch
):
    service = account_client.app.state.service
    service.executor.submit = lambda *_args, **_kwargs: None
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="callback-attempt-identity"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_callback_attempt_identity_0001",
            title="Callback attempt identity",
        ),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    worker = store.get_worker(delegation["worker_id"])
    run = store.get_run(delegation["current_run_id"])
    callback_id = "cb_callback_attempt_identity"

    monkeypatch.setattr(service_module.time, "time", lambda: 1_700_000_001)
    first = service._emit_callback(
        worker,
        "run.queued",
        run=run,
        message="Queued",
        callback_id=callback_id,
        insert_once=True,
        submit_delivery=False,
    )
    monkeypatch.setattr(service_module.time, "time", lambda: 1_700_000_999)
    replay = service._emit_callback(
        worker,
        "run.queued",
        run=run,
        message="Queued",
        callback_id=callback_id,
        insert_once=True,
        submit_delivery=False,
    )

    assert first is not None and replay is not None
    first_payload = json.loads(first["payload_json"])
    replay_payload = json.loads(replay["payload_json"])
    assert first_payload == replay_payload
    assert first_payload["callback_id"] == callback_id
    assert first_payload["attempt_number"] is None
    assert first_payload["callback_ts"] == 1_700_000_001
    assert first_payload["origin_ref"] == "ghi_callback_attempt_identity_0001"
    assert first_payload["work_ref"] == accepted["workRef"]
    assert first_payload["worker_id"] == delegation["worker_id"]
    assert first_payload["run_id"] == delegation["current_run_id"]
    assert first["attempt_number"] == 0


def test_completed_work_detail_matches_strict_core_producer_contract(
    account_client, tmp_path, monkeypatch
):
    service = account_client.app.state.service
    service.executor.submit = lambda *_args, **_kwargs: None
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="strict-producer-detail"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_strict_producer_detail_0001",
            title="Strict producer detail",
        ),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = str(delegation["current_run_id"])
    worker = store.get_worker(delegation["worker_id"])
    claimed = store.claim_next_queued_run(delegation["worker_id"])
    lease = store.get_active_host_run_lease_for_run(run_id)
    assert claimed is not None and lease is not None
    admitted = store.admit_claimed_run(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=str(lease["executor_id"]),
    )
    preflight = store.record_provider_authorization_preflight(
        run_id,
        provider="openai",
        status="authorized",
        failure_class="",
    )
    invoked = store.mark_run_runtime_invoked(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=str(lease["executor_id"]),
    )
    assert admitted is not None and preflight is not None and invoked is not None

    workspace = tmp_path / "strict-producer-workspace"
    artifact = workspace / "artifacts" / "result.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body>Safe result</body></html>", encoding="utf-8")
    store.update_worker(worker["worker_id"], workspace_dir=str(workspace))
    terminal_artifact_refs = service_module.work_artifact_observation(
        store.get_worker(worker["worker_id"]),
        store.get_run(run_id),
        output_text="FINAL REPORT:\nSafe result",
        error_text="",
    )
    store.record_artifact_trace(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
        artifact_refs=terminal_artifact_refs,
    )
    terminal = store.finalize_run_if_state(
        run_id,
        "running",
        "completed",
        output_text="FINAL REPORT:\nSafe result",
        artifact_refs=terminal_artifact_refs,
        **_exact_terminal_generation(store, run_id),
    )
    assert terminal is not None
    callback = service._emit_callback(
        store.get_worker(worker["worker_id"]),
        "run.completed",
        run=terminal,
        message="Safe result",
        callback_id="cb_strict_producer_completed",
        insert_once=True,
        submit_delivery=False,
    )
    assert callback is not None
    for retry_number in range(8):
        retry_claim = store.claim_pending_callback(callback["callback_id"])
        assert retry_claim is not None
        pending_callback = store.mark_callback_pending(
            callback["callback_id"],
            lease_token=retry_claim["delivery_lease_token"],
            delivery_generation=retry_claim["delivery_generation"],
            attempts=1,
            payload_json=retry_claim["payload_json"],
            last_error=f"synthetic transport retry {retry_number + 1}",
        )
        assert pending_callback is not None
    claimed_callback = store.claim_pending_callback(callback["callback_id"])
    assert claimed_callback is not None
    accepted_callback = store.mark_callback_http_accepted(
        callback["callback_id"],
        lease_token=claimed_callback["delivery_lease_token"],
        delivery_generation=claimed_callback["delivery_generation"],
        attempts=1,
        payload_json=claimed_callback["payload_json"],
    )
    assert accepted_callback is not None

    def reject_get_write(*_args, **_kwargs):
        raise AssertionError("GET attempted a durable reconciliation or artifact write")

    monkeypatch.setattr(store, "record_artifact_trace", reject_get_write)
    monkeypatch.setattr(store, "reconcile_invalid_running_runs", reject_get_write)
    with sqlite3.connect(store.db_path) as observer:
        before_data_version = observer.execute("PRAGMA data_version").fetchone()[0]
        before_trace = observer.execute(
            "SELECT COUNT(*), MAX(sequence), MAX(event_sha256) "
            "FROM work_trace_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        response = account_client.get(
            f"/v1/work/{accepted['workRef']}", headers=account_headers()
        )
        after_data_version = observer.execute("PRAGMA data_version").fetchone()[0]
        after_trace = observer.execute(
            "SELECT COUNT(*), MAX(sequence), MAX(event_sha256) "
            "FROM work_trace_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert after_data_version == before_data_version
    assert after_trace == before_trace
    assert response.status_code == 200
    detail = response.json()
    assert detail["runRef"] == "run_sha256:" + hashlib.sha256(
        run_id.encode("utf-8")
    ).hexdigest()
    assert set(detail["lifecycle"]) == {
        "attemptNumber",
        "queuedAt",
        "claimedAt",
        "admittedAt",
        "runtimeInvokedAt",
        "startedAt",
        "endedAt",
    }
    assert detail["lifecycle"]["attemptNumber"] == 1
    assert detail["lifecycle"]["startedAt"] == detail["lifecycle"]["runtimeInvokedAt"]
    assert detail["attemptHistoryOverflowCount"] == 0
    assert detail["attemptHistory"][-1]["attemptNumber"] == 1
    assert detail["attemptHistory"][-1]["state"] == "completed"
    assert set(detail["attemptHistory"][-1]) == {
        "attemptNumber",
        "state",
        "claimedAt",
        "admittedAt",
        "runtimeInvokedAt",
        "endedAt",
        "terminalReason",
        "providerHealthObservedLastFailedAt",
        "providerHealthObservedGeneration",
    }
    assert detail["capacityAttemptOverflowCount"] == 0
    assert len(detail["capacityAttempts"]) == 1
    assert detail["callbackDeliveryOverflowCount"] == 4
    assert len(detail["callbackDeliveries"]) == 16
    assert detail["callbackDeliveries"][0]["ledgerSequence"] == 5
    assert detail["callbackDeliveries"][0]["previousEventSha256"].startswith("sha256:")
    for callback_delivery in detail["callbackDeliveries"]:
        assert set(callback_delivery) == {
            "acceptedAt",
            "attemptNumber",
            "attempts",
            "authoritySha256",
            "callbackRef",
            "callbackRevision",
            "createdAt",
            "deliveryGeneration",
            "event",
            "eventSha256",
            "ledgerSequence",
            "payloadSha256",
            "previousEventSha256",
            "resultDigest",
            "resultRevision",
            "status",
            "updatedAt",
        }
        assert callback_delivery["callbackRef"].startswith("callback_sha256:")
        expected_attempt = 1 if callback_delivery["event"] == "run.completed" else None
        assert callback_delivery["attemptNumber"] == expected_attempt
    final_callback = detail["callbackDeliveries"][-1]
    assert {
        key: final_callback[key]
        for key in (
            "callbackRef",
            "attemptNumber",
            "event",
            "status",
            "attempts",
            "createdAt",
            "updatedAt",
            "acceptedAt",
            "callbackRevision",
            "deliveryGeneration",
            "resultRevision",
            "resultDigest",
        )
    } == {
        "callbackRef": "callback_sha256:"
        + hashlib.sha256(b"cb_strict_producer_completed").hexdigest(),
        "attemptNumber": 1,
        "event": "run.completed",
        "status": "http_accepted",
        "attempts": 9,
        "createdAt": callback["created_at"],
        "updatedAt": accepted_callback["updated_at"],
        "acceptedAt": accepted_callback["http_accepted_at"],
        "callbackRevision": 19,
        "deliveryGeneration": 9,
        "resultRevision": accepted_callback["result_revision"],
        "resultDigest": accepted_callback["result_digest"] or None,
    }
    assert final_callback["ledgerSequence"] == (
        detail["callbackDeliveryOverflowCount"] + len(detail["callbackDeliveries"])
    )
    assert final_callback["previousEventSha256"] == detail["callbackDeliveries"][-2][
        "eventSha256"
    ]
    for key in ("eventSha256", "payloadSha256", "authoritySha256"):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", final_callback[key])
    assert set(detail["traceability"]) == {
        "contractVersion",
        "origin",
        "promptLayers",
        "runtimeInvocations",
        "providerAuthorizationPreflights",
        "integrity",
    }
    assert detail["traceability"]["origin"] == {
        "originRef": "origin_sha256:"
        + hashlib.sha256(b"ghi_strict_producer_detail_0001").hexdigest(),
        "sourceEventRef": None,
        "sourceRevision": 1,
        "surface": "telegram",
    }
    assert detail["traceability"]["promptLayers"] == _expected_worker_prompt_trace()
    assert detail["traceability"]["contractVersion"] == 2
    assert detail["traceability"]["runtimeInvocations"] == [
        {
            "attemptNumber": 1,
            "model": worker["model"],
            "profile": worker["profile"],
            "runtimeInvocationRef": detail["traceability"]["runtimeInvocations"][0][
                "runtimeInvocationRef"
            ],
            "runtime": worker["runtime"],
            "runtimeInvokedAt": invoked["runtime_invoked_at"],
        }
    ]
    assert detail["traceability"]["runtimeInvocations"][0][
        "runtimeInvocationRef"
    ].startswith("runtime_invocation_sha256:")
    assert detail["traceability"]["providerAuthorizationPreflights"] == [
        {
            "attemptNumber": 1,
            "failureClass": None,
            "observedAt": detail["traceability"]["providerAuthorizationPreflights"][0][
                "observedAt"
            ],
            "provider": "openai",
            "providerAuthorizationPreflightRef": detail["traceability"][
                "providerAuthorizationPreflights"
            ][0]["providerAuthorizationPreflightRef"],
            "status": "authorized",
        }
    ]
    assert detail["traceability"]["integrity"]["algorithm"] == "sha256-chain-v1"
    assert detail["traceability"]["integrity"]["eventCount"] >= 11
    assert detail["traceability"]["integrity"]["headSha256"].startswith("sha256:")
    assert detail["delivery"]["state"] == "pending"
    assert set(detail["artifactRefs"]) == {"available", "refs", "overflowCount"}
    assert len(detail["artifactHistory"]) == 2
    assert detail["artifactHistory"][-1]["observedAt"] >= detail["lifecycle"][
        "endedAt"
    ]
    assert detail["artifactRefs"]["overflowCount"] == 0
    for artifact_ref in detail["artifactRefs"]["refs"]:
        assert set(artifact_ref) == {
            "artifactRef",
            "fingerprint",
            "kind",
            "state",
            "sizeBytes",
        }
        assert artifact_ref["artifactRef"].removeprefix("artifact_sha256:") == (
            artifact_ref["fingerprint"].removeprefix("sha256:")
        )
    contract_output = os.environ.get("GLASSHIVE_CORE_TRACE_CONTRACT_OUTPUT", "").strip()
    if contract_output:
        Path(contract_output).write_text(
            json.dumps(
                {
                    "workRef": accepted["workRef"],
                    "runRef": run_id,
                    "detail": detail,
                    "contract": {
                        **service.work_trace_contract_capability(),
                        "emittedKeySetDigest": (
                            service_module.work_trace_emitted_key_set_digest(detail)
                        ),
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    first_artifact_refs = detail["artifactRefs"]
    first_head = detail["traceability"]["integrity"]["headSha256"]
    artifact.write_text(
        "<html><body>Altered after completion</body></html>", encoding="utf-8"
    )
    altered_response = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert altered_response.status_code == 200
    altered = altered_response.json()
    assert altered["artifactRefs"] == first_artifact_refs
    assert altered["artifactHistory"] == detail["artifactHistory"]
    assert altered["traceability"]["integrity"]["headSha256"] == first_head
    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE work_trace_events SET payload_json = '{}' "
                "WHERE run_id = ? AND event_type = 'artifact.observed'",
                (run_id,),
            )


def test_terminal_artifact_reconciler_repairs_legacy_missing_observation_once(
    account_client, tmp_path
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="legacy-artifact-observation"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_legacy_artifact_observation_0001",
            title="Legacy artifact observation",
        ),
    ).json()
    store = account_client.app.state.store
    service = account_client.app.state.service
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = str(delegation["current_run_id"])
    workspace = tmp_path / "legacy-artifact-workspace"
    artifact = workspace / "artifacts" / "result.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body>Legacy result</body></html>", encoding="utf-8")
    store.update_worker(delegation["worker_id"], workspace_dir=str(workspace))
    artifact_refs = service_module.work_artifact_observation(
        store.get_worker(delegation["worker_id"]),
        store.get_run(run_id),
        output_text="FINAL REPORT:\nartifacts/result.html",
        error_text="",
    )
    store.record_artifact_trace(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
        artifact_refs=artifact_refs,
    )
    ended_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE runs SET state = 'completed', ended_at = ?, "
            "output_text = ? WHERE run_id = ?",
            (ended_at, "FINAL REPORT:\nartifacts/result.html", run_id),
        )

    before = store.work_trace_detail(
        run_id=run_id, tenant_id="tenant-a", owner_id="owner-a"
    )
    assert before is not None and len(before["artifactHistory"]) == 1
    assert service.reconcile_terminal_artifact_observations() == 1
    assert service.reconcile_terminal_artifact_observations() == 0
    after = store.work_trace_detail(
        run_id=run_id, tenant_id="tenant-a", owner_id="owner-a"
    )
    assert after is not None
    assert len(after["artifactHistory"]) == 2
    assert after["artifactRefs"]["available"] is True
    assert after["artifactHistory"][-1]["observedAt"] >= ended_at


def test_work_trace_detail_normalizes_interrupted_attempt_to_public_cancelled_state(
    account_client,
):
    service = account_client.app.state.service
    service.executor.submit = lambda *_args, **_kwargs: None
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="interrupted-trace-normalization"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_interrupted_trace_normalization_0001",
            title="Interrupted trace normalization",
        ),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = str(delegation["current_run_id"])
    _truthfully_invoke_run(store, run_id, suffix="interrupted-trace-normalization")
    interrupted = store.transition_run_if_state(run_id, "running", "interrupted")
    assert interrupted is not None

    detail = store.work_trace_detail(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
    )

    assert detail is not None
    assert detail["attemptHistory"][-1]["state"] == "cancelled"
    assert detail["attemptHistory"][-1]["terminalReason"] == "interrupted"


def test_work_detail_returns_truthful_bounded_pages_when_trace_history_overflows(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="producer-detail-overflow"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_producer_detail_overflow_0001",
            title="Producer detail overflow",
        ),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = str(delegation["current_run_id"])
    for _index in range(17):
        claimed = store.claim_next_queued_run(delegation["worker_id"])
        assert claimed is not None
        store.requeue_run_for_retry(
            run_id,
            retry_after=datetime.now(timezone.utc).isoformat(),
            **(store.get_run_retry_generation(run_id) or {}),
            last_retry_class="host_capacity",
            consume_retry_budget=False,
            capacity_class="resource_pressure",
            failure_class="host_capacity",
            failure_retryable=1,
            failure_structured=1,
        )

    response = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert response.status_code == 200
    first = response.json()
    assert len(first["attemptHistory"]) == 16
    assert first["attemptHistoryOverflowCount"] == 1
    assert first["historyPage"]["cursor"] is None
    assert first["historyPage"]["nextCursor"]
    assert first["historyPage"]["limit"] == 16
    assert first["historyPage"]["total"] >= 17
    assert first["historyPage"]["showing"] == sum(
        len(first[key])
        for key in (
            "attemptHistory",
            "capacityAttempts",
            "callbackDeliveries",
            "artifactHistory",
        )
    )
    assert first["historyPage"]["overflowCount"] == (
        first["historyPage"]["total"] - first["historyPage"]["showing"]
    )
    cursor = str(first["historyPage"]["nextCursor"])
    head_index = len("history_")
    tampered_cursor = (
        cursor[:head_index]
        + ("0" if cursor[head_index] != "0" else "1")
        + cursor[head_index + 1 :]
    )
    invalid_response = account_client.get(
        f"/v1/work/{accepted['workRef']}",
        headers=account_headers(),
        params={"historyCursor": tampered_cursor},
    )
    assert invalid_response.status_code == 400
    assert invalid_response.json()["detail"]["code"] == "work_history_cursor_invalid"

    appended = store.claim_next_queued_run(delegation["worker_id"])
    assert appended is not None
    store.requeue_run_for_retry(
        run_id,
        retry_after=datetime.now(timezone.utc).isoformat(),
        **(store.get_run_retry_generation(run_id) or {}),
        last_retry_class="host_capacity",
        consume_retry_budget=False,
        capacity_class="resource_pressure",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )

    older_response = account_client.get(
        f"/v1/work/{accepted['workRef']}",
        headers=account_headers(),
        params={"historyCursor": first["historyPage"]["nextCursor"]},
    )
    assert older_response.status_code == 200
    older = older_response.json()
    assert len(older["attemptHistory"]) == 1
    assert older["attemptHistory"][0]["attemptNumber"] == 1
    assert not {
        item["attemptNumber"] for item in first["attemptHistory"]
    }.intersection(item["attemptNumber"] for item in older["attemptHistory"])
    assert older["historyPage"]["cursor"] == first["historyPage"]["nextCursor"]
    assert older["historyPage"]["nextCursor"] is None
    assert older["historyPage"]["total"] == first["historyPage"]["total"]
    assert older["traceability"]["integrity"] == first["traceability"]["integrity"]


def test_delegation_idempotency_returns_same_work_and_rejects_changed_request(account_client):
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-same"),
        json=delegation_payload(),
    )
    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-same"),
        json=delegation_payload(),
    )
    changed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-same"),
        json=delegation_payload(instruction="A materially different instruction"),
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "delegation_idempotency_conflict"
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_delegation_replay_returns_committed_receipt_before_mutable_admission_checks(
    account_client, monkeypatch
):
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-replay-before-admission"),
        json=delegation_payload(),
    )
    assert first.status_code == 202

    service = account_client.app.state.service
    mutable_checks: list[str] = []

    def reject(check: str):
        def _raise(*_args, **_kwargs):
            mutable_checks.append(check)
            raise RuntimeError(f"mutable {check} changed after commit")

        return _raise

    monkeypatch.setattr(service, "_ensure_profile_allowed", reject("profile"))
    monkeypatch.setattr(service, "_ensure_runtime_available", reject("runtime"))
    monkeypatch.setattr(service, "_enforce_worker_limits", reject("capacity"))

    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-replay-before-admission"),
        json=delegation_payload(),
    )
    changed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-replay-before-admission"),
        json=delegation_payload(instruction="Changed after the committed request"),
    )

    assert replay.status_code == 202
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "delegation_idempotency_conflict"
    assert mutable_checks == []


def test_delegation_identity_separates_intentional_identical_objectives(account_client):
    goal_digest = hashlib.sha256(b"same objective").hexdigest()

    def identified_payload(*, ordinal: int, key: str) -> dict:
        payload = delegation_payload(title="Same objective")
        payload["bootstrapBundle"]["viventium_delegation_identity"] = {
            "version": 1,
            "idempotency_key": key,
            "goal_digest": goal_digest,
            "call_identity_digest": hashlib.sha256(
                f"provider-call-{ordinal}".encode()
            ).hexdigest(),
            "source_event_id": "telegram-update-synthetic-1",
            "objective_ordinal": ordinal,
        }
        return payload

    first_key = hashlib.sha256(b"objective ordinal zero").hexdigest()
    second_key = hashlib.sha256(b"objective ordinal one").hexdigest()
    first_payload = identified_payload(ordinal=0, key=first_key)
    second_payload = identified_payload(ordinal=1, key=second_key)
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=first_key),
        json=first_payload,
    )
    second = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=second_key),
        json=second_payload,
    )
    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=first_key),
        json=first_payload,
    )

    assert first.status_code == second.status_code == replay.status_code == 202
    assert first.json()["workRef"] != second.json()["workRef"]
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True


def test_delegation_identity_header_binding_and_digest_fail_closed(account_client):
    key = hashlib.sha256(b"trusted identity key").hexdigest()
    other_key = hashlib.sha256(b"other identity key").hexdigest()
    payload = delegation_payload(title="Bound identity")
    payload["bootstrapBundle"]["viventium_delegation_identity"] = {
        "version": 1,
        "idempotency_key": key,
        "goal_digest": hashlib.sha256(b"goal A").hexdigest(),
        "call_identity_digest": hashlib.sha256(b"provider-call-bound").hexdigest(),
        "source_event_id": "telegram-update-synthetic-2",
        "objective_ordinal": 0,
    }
    mismatch = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=other_key),
        json=payload,
    )
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=payload,
    )
    changed = json.loads(json.dumps(payload))
    changed["bootstrapBundle"]["viventium_delegation_identity"]["goal_digest"] = (
        hashlib.sha256(b"goal B").hexdigest()
    )
    conflict = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=changed,
    )

    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "delegation_identity_invalid"
    assert accepted.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "delegation_idempotency_conflict"


def test_account_delegation_accepts_the_core_verified_v2_launch_identity(account_client):
    key = hashlib.sha256(b"core verified v2 identity").hexdigest()
    payload = delegation_payload(title="Verified v2 launch")
    payload["bootstrapBundle"]["viventium_delegation_identity"] = {
        "version": 2,
        "idempotency_key": key,
        "goal_digest": hashlib.sha256(b"goal v2").hexdigest(),
        "launch_payload_digest": hashlib.sha256(b"final enriched launch").hexdigest(),
        "call_identity_digest": hashlib.sha256(b"provider-call-v2").hexdigest(),
        "source_event_id": "web-source-synthetic-v2",
        "objective_ordinal": 0,
    }
    payload["bootstrapBundle"]["viventium_delegation_assertion"] = "a" * 64

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=payload,
    )

    assert accepted.status_code == 202
    assert accepted.json()["workRef"].startswith("work_")


def test_delegation_identity_ordinal_is_not_part_of_atomic_conflict_digest(account_client):
    key = hashlib.sha256(b"stable provider call identity key").hexdigest()
    identity = {
        "version": 1,
        "idempotency_key": key,
        "goal_digest": hashlib.sha256(b"stable goal").hexdigest(),
        "call_identity_digest": hashlib.sha256(b"stable provider tool call").hexdigest(),
        "source_event_id": "telegram-update-synthetic-stable-call",
        "objective_ordinal": 0,
    }
    payload = delegation_payload(title="Stable reconstructed call")
    payload["bootstrapBundle"]["viventium_delegation_identity"] = identity
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=payload,
    )
    reordered = json.loads(json.dumps(payload))
    reordered["bootstrapBundle"]["viventium_delegation_identity"][
        "objective_ordinal"
    ] = 7
    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=reordered,
    )

    assert first.status_code == replay.status_code == 202
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True


def test_active_work_and_detail_are_assertion_scoped_and_do_not_leak_internal_ids(account_client):
    owner_a = account_client.post(
        "/v1/delegations",
        headers=account_headers(owner_id="owner-a", idempotency_key="delegation-owner-a"),
        json=delegation_payload(title="Owner A mission"),
    )
    owner_b = account_client.post(
        "/v1/delegations",
        headers=account_headers(owner_id="owner-b", idempotency_key="delegation-owner-b"),
        json=delegation_payload(title="Owner B mission"),
    )
    assert owner_a.status_code == owner_b.status_code == 202

    roster_headers = account_headers(owner_id="owner-a")
    roster_headers["X-Viventium-Owner-Id"] = "owner-b"
    roster_headers["X-GlassHive-User-Id"] = "owner-b"
    roster = account_client.get("/v1/active-work", headers=roster_headers)
    own_detail = account_client.get(
        f"/v1/work/{owner_a.json()['workRef']}",
        headers=account_headers(owner_id="owner-a"),
    )
    foreign_detail = account_client.get(
        f"/v1/work/{owner_b.json()['workRef']}",
        headers=account_headers(owner_id="owner-a"),
    )

    assert roster.status_code == 200
    assert roster.json()["snapshot"] == "fresh"
    assert roster.json()["overflowCount"] == 0
    assert [item["title"] for item in roster.json()["work"]] == ["Owner A mission"]
    assert own_detail.status_code == 200
    assert foreign_detail.status_code == 404
    assert own_detail.json()["originSurface"] == "telegram"
    assert own_detail.json()["provider"] == "codex"
    assert own_detail.json()["nativeTeam"] is None
    assert own_detail.json()["delivery"] == {
        "state": "pending",
        "unreadTerminal": False,
    }
    serialized = json.dumps({"roster": roster.json(), "detail": own_detail.json()})
    assert "prj_" not in serialized
    assert "wrk_" not in serialized
    own_record = account_client.app.state.store.get_delegation(
        owner_a.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert own_record is not None
    assert str(own_record["current_run_id"]) not in serialized
    assert '"runRef": "run_sha256:' in serialized
    assert "Research alpha deeply" not in serialized
    assert "/private/example" not in serialized
    assert "must-not-leak" not in serialized


def test_active_work_native_team_is_null_until_child_projection_is_observed(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-native-team"),
        json=delegation_payload(title="Native team mission"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = record["current_run_id"]
    summary = {
        "observable": True,
        "provider": "codex",
        "sessionId": "thread_synthetic",
        "activeCount": 1,
        "children": [
            {
                "childRef": "child_synthetic",
                "role": "researcher",
                "state": "running",
                "updatedAt": "2099-01-01T00:00:00+00:00",
            },
            {
                "childRef": "child_private_path",
                "role": "/Users/example/private/reviewer@example.com",
                "state": "failed",
                "updatedAt": "2099-01-01T00:00:00+00:00",
            },
        ],
    }
    store.update_run(
        run_id,
        native_capabilities_json=json.dumps(
            {"providerStream": True, "childProjection": False}
        ),
        native_child_summary_json=json.dumps(summary),
    )

    session_only = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert session_only.status_code == 200
    assert session_only.json()["nativeTeam"] is None

    store.update_run(
        run_id,
        native_capabilities_json=json.dumps(
            {"providerStream": True, "childProjection": True}
        ),
    )
    child_observed = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert child_observed.status_code == 200
    assert child_observed.json()["nativeTeam"] == {
        "active": 1,
        "total": 2,
        "needsAttention": 1,
        "degraded": False,
        "topology": [
            {"role": "worker", "state": "failed", "count": 1},
            {"role": "researcher", "state": "running", "count": 1},
        ],
        "overflowCount": 0,
    }
    roster = account_client.get("/v1/active-work", headers=account_headers())
    roster_item = next(
        item for item in roster.json()["work"] if item["workRef"] == accepted["workRef"]
    )
    assert roster_item["nativeTeam"] == {
        "active": 1,
        "total": 2,
        "needsAttention": 1,
        "degraded": False,
    }
    serialized = json.dumps({"detail": child_observed.json(), "list": roster_item})
    assert "thread_synthetic" not in serialized
    assert "child_synthetic" not in serialized
    assert "/Users/" not in serialized
    assert "reviewer@example.com" not in serialized


def test_recent_terminal_work_is_pinned_then_ages_into_history(account_client, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ACTIVE_WORK_TERMINAL_RECENCY_S", "3600")
    completed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-completed"),
        json=delegation_payload(title="Completed mission"),
    ).json()
    failed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-failed"),
        json=delegation_payload(title="Failed mission"),
    ).json()
    store = account_client.app.state.store
    completed_row = store.get_delegation(completed["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    failed_row = store.get_delegation(failed["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    store.finalize_run(completed_row["current_run_id"], "completed", output_text="All done")
    store.finalize_run(
        failed_row["current_run_id"],
        "failed",
        error_text="Provider unavailable",
        failure_class="provider_temporarily_unavailable",
        failure_retryable=1,
        failure_structured=1,
        failure_user_message="The provider is temporarily unavailable.",
    )

    roster = account_client.get("/v1/active-work", headers=account_headers())

    assert roster.status_code == 200
    assert [item["title"] for item in roster.json()["work"]] == [
        "Failed mission",
        "Completed mission",
    ]
    assert roster.json()["work"][0]["state"] == "failed"
    assert roster.json()["work"][0]["actions"] == ["retry", "queue", "message", "dismiss"]
    assert roster.json()["work"][1]["delivery"]["unreadTerminal"] is True

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE runs SET ended_at = ? WHERE run_id IN (?, ?)",
            (old, completed_row["current_run_id"], failed_row["current_run_id"]),
        )
        conn.execute(
            "UPDATE delegations SET updated_at = ? WHERE work_ref IN (?, ?)",
            (old, completed["workRef"], failed["workRef"]),
        )

    aged_roster = account_client.get("/v1/active-work", headers=account_headers()).json()
    history = account_client.get("/v1/active-work/history", headers=account_headers()).json()

    assert aged_roster["work"] == []
    assert {item["workRef"] for item in history["work"]} == {
        completed["workRef"],
        failed["workRef"],
    }
    assert all(item["actions"] == [] for item in history["work"])


def test_active_work_keeps_only_a_bounded_recent_set_per_terminal_state(
    account_client, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_ACTIVE_WORK_TERMINAL_RECENCY_S", "86400")
    monkeypatch.setenv("GLASSHIVE_ACTIVE_WORK_TERMINAL_RECENT_PER_STATE", "2")
    created: list[str] = []
    store = account_client.app.state.store

    for index in range(4):
        accepted = account_client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-recent-terminal-{index}"),
            json=delegation_payload(title=f"Completed {index}"),
        ).json()
        created.append(accepted["workRef"])
        row = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        store.finalize_run(row["current_run_id"], "completed", output_text="Done")

    active = account_client.get("/v1/active-work", headers=account_headers()).json()
    history = account_client.get("/v1/active-work/history", headers=account_headers()).json()

    assert [item["workRef"] for item in active["work"]] == list(reversed(created[-2:]))
    assert {item["workRef"] for item in history["work"]} == set(created[:2])
    assert active["overflowCount"] == 0


def test_active_work_discloses_structured_provider_route_switch(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-route-switch"),
        json=delegation_payload(title="Route switch mission"),
    ).json()
    store = account_client.app.state.store
    association = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    retry_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    store.update_run(
        association["current_run_id"],
        provider_route_profile="claude-code",
        provider_route_runtime="claude-code",
        provider_route_model="opus",
        provider_route_decision="fallback_selected",
        provider_route_from_profile="codex-cli",
        provider_route_from_runtime="codex-cli",
        provider_route_from_model="gpt-5.6-sol",
        provider_route_failure_class="provider_quota_exhausted",
        provider_route_cooldown_until=retry_at,
    )

    roster = account_client.get("/v1/active-work", headers=account_headers())

    item = next(
        row for row in roster.json()["work"] if row["workRef"] == accepted["workRef"]
    )
    assert item["route"] == {
        "decision": "fallback_selected",
        "profile": "claude-code",
        "runtime": "claude-code",
        "model": "opus",
        "from": {
            "profile": "codex-cli",
            "runtime": "codex-cli",
            "model": "gpt-5.6-sol",
        },
        "failureClass": "provider_quota_exhausted",
        "cooldownUntil": retry_at,
    }


def test_active_work_stop_is_exact_idempotent_and_owner_scoped(account_client):
    account_client.app.state.service.executor.submit = lambda *_args, **_kwargs: None
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-cancel"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_pre_runtime_stop_0001",
            title="Cancelable mission",
        ),
    ).json()
    work_ref = accepted["workRef"]

    stopped = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-once"},
    )
    replay = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-once"},
    )
    foreign = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(owner_id="owner-b"),
        json={"action": "stop", "idempotencyKey": "action-foreign"},
    )

    assert stopped.status_code == replay.status_code == 202
    assert stopped.json()["action"] == "stop"
    assert stopped.json()["state"] == "cancelled"
    assert replay.json()["idempotentReplay"] is True
    assert foreign.status_code == 404
    row = account_client.app.state.store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    store = account_client.app.state.store
    run_id = str(row["current_run_id"])
    assert store.get_run(run_id)["state"] == "cancelled"
    terminal_callback = next(
        callback
        for callback in store.list_callback_outbox_for_run(
            run_id,
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        if callback["event_type"] in {"run.cancelled", "run.interrupted"}
    )
    if terminal_callback["status"] == "pending":
        assert store.claim_pending_callback(terminal_callback["callback_id"]) is not None
    else:
        assert terminal_callback["status"] == "delivering"
    detail_response = account_client.get(f"/v1/work/{work_ref}", headers=account_headers())
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["lifecycle"] == {
        "attemptNumber": None,
        "queuedAt": detail["lifecycle"]["queuedAt"],
        "claimedAt": None,
        "admittedAt": None,
        "runtimeInvokedAt": None,
        "startedAt": None,
        "endedAt": detail["lifecycle"]["endedAt"],
    }
    assert detail["lifecycle"]["queuedAt"]
    assert detail["lifecycle"]["endedAt"]
    assert detail["attemptHistory"] == []
    assert detail["traceability"]["contractVersion"] == 2
    assert detail["traceability"]["runtimeInvocations"] == []
    assert detail["traceability"]["providerAuthorizationPreflights"] == []
    assert detail["callbackDeliveries"][-1]["attemptNumber"] is None
    output_path = os.environ.get("GLASSHIVE_CORE_TRACE_PRE_RUNTIME_OUTPUT", "").strip()
    if output_path:
        Path(output_path).write_text(
            json.dumps({"workRef": work_ref, "runRef": run_id, "detail": detail}),
            encoding="utf-8",
        )


def test_active_work_stop_after_retry_queue_finishes_terminal_trace(
    account_client, monkeypatch
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-after-retry-queue"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_retry_queue_stop_0001",
            title="Retry queued stop",
        ),
    ).json()
    store = account_client.app.state.store
    work_ref = accepted["workRef"]
    record = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = str(record["current_run_id"])
    running = _truthfully_invoke_run(store, run_id, suffix="retry-queue-stop")
    lease = store.get_active_host_run_lease_for_run(run_id)
    assert lease is not None
    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
            (expired_at, expired_at, lease["lease_id"]),
        )
    assert store.reconcile_invalid_running_runs() == 1
    retry_queued = store.get_run(run_id)
    assert retry_queued is not None and retry_queued["state"] == "queued"
    attempts_before = store.list_run_attempts(run_id)
    assert attempts_before[-1]["state"] == "retry_queued"

    stopped = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-after-retry-queue"},
    )

    assert stopped.status_code == 202, stopped.text
    assert stopped.json()["state"] == "cancelled"
    terminal_callback = next(
        callback
        for callback in store.list_callback_outbox_for_run(
            run_id,
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        if callback["event_type"] == "run.cancelled"
    )
    if terminal_callback["status"] == "pending":
        assert store.claim_pending_callback(terminal_callback["callback_id"]) is not None
    else:
        assert terminal_callback["status"] == "delivering"
    before_get = store.work_trace_detail(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    assert before_get is not None
    assert before_get["artifactHistory"]
    assert before_get["artifactHistory"][-1]["artifactRefs"] == before_get["artifactRefs"]
    assert before_get["artifactHistory"][-1]["observedAt"] >= str(
        store.get_run(run_id)["ended_at"]
    )

    def reject_get_write(*_args, **_kwargs):
        raise AssertionError("GET attempted a durable reconciliation or artifact write")

    monkeypatch.setattr(store, "record_artifact_trace", reject_get_write)
    monkeypatch.setattr(store, "reconcile_invalid_running_runs", reject_get_write)
    with sqlite3.connect(store.db_path) as observer:
        before_data_version = observer.execute("PRAGMA data_version").fetchone()[0]
        before_rows = observer.execute(
            "SELECT COUNT(*), MAX(sequence), MAX(event_sha256) "
            "FROM work_trace_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        detail_response = account_client.get(
            f"/v1/work/{work_ref}", headers=account_headers()
        )
        after_data_version = observer.execute("PRAGMA data_version").fetchone()[0]
        after_rows = observer.execute(
            "SELECT COUNT(*), MAX(sequence), MAX(event_sha256) "
            "FROM work_trace_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert after_data_version == before_data_version
    assert after_rows == before_rows
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()

    assert detail["lifecycle"]["attemptNumber"] == 1
    assert detail["attemptHistory"][-1] == {
        **detail["attemptHistory"][-1],
        "attemptNumber": 1,
        "state": "cancelled",
        "endedAt": detail["lifecycle"]["endedAt"],
        "terminalReason": "work_stopped",
    }
    assert detail["callbackDeliveries"][-1]["event"] == "run.cancelled"
    assert detail["callbackDeliveries"][-1]["status"] == "delivering"
    assert detail["callbackDeliveries"][-1]["attemptNumber"] == 1
    assert detail["artifactHistory"][-1]["artifactRefs"] == detail["artifactRefs"]
    assert detail["artifactHistory"][-1]["observedAt"] >= detail["lifecycle"]["endedAt"]

    replay = account_client.get(f"/v1/work/{work_ref}", headers=account_headers())
    assert replay.status_code == 200, replay.text
    assert replay.json()["artifactHistory"] == detail["artifactHistory"]
    output_path = os.environ.get("GLASSHIVE_CORE_TRACE_RETRY_STOP_OUTPUT", "").strip()
    if output_path:
        Path(output_path).write_text(
            json.dumps({"workRef": work_ref, "runRef": run_id, "detail": detail}),
            encoding="utf-8",
        )


def test_active_work_pause_and_resume_preserve_the_exact_queued_run(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-pause-queued"),
        json=delegation_payload(title="Pause queued mission"),
    ).json()
    work_ref = accepted["workRef"]
    store = account_client.app.state.store
    before = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = before["current_run_id"]

    paused = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "pause", "idempotencyKey": "action-pause-queued"},
    )
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"
    assert store.get_run(run_id)["state"] == "paused"

    resumed = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "resume", "idempotencyKey": "action-resume-queued"},
    )
    assert resumed.status_code == 202
    assert resumed.json()["state"] == "queued"
    assert store.get_run(run_id)["state"] == "queued"
    assert [run["run_id"] for run in store.list_runs_for_worker(before["worker_id"])] == [
        run_id
    ]


def test_active_work_stop_accepts_the_exact_paused_run(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-paused"),
        json=delegation_payload(title="Stop paused mission"),
    ).json()
    work_ref = accepted["workRef"]
    store = account_client.app.state.store
    before = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = before["current_run_id"]

    assert account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "pause", "idempotencyKey": "action-pause-before-stop"},
    ).status_code == 202
    stopped = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-after-pause"},
    )

    assert stopped.status_code == 202
    assert stopped.json()["state"] == "cancelled"
    assert store.get_run(run_id)["state"] == "cancelled"


def test_auth_attention_resume_accepts_bounded_core_reauthorization_and_reuses_run(
    account_client,
):
    payload = delegation_payload_with_origin(
        origin_ref="ghi_synthetic_reauthorization_origin",
        title="Reauthorize mission",
    )
    old_max = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    payload["bootstrapBundle"]["glasshive_capability_authorization"] = {
        "version": 1,
        "status": "pending_admission",
        "authorization_ref": "gha_synthetic_reauthorization_ref",
        "origin_ref": "ghi_synthetic_reauthorization_origin",
        "scope_fingerprint": "scope_synthetic_reauthorization",
        "max_expires_at": old_max,
    }
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-reauthorize"),
        json=payload,
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = record["current_run_id"]
    assert store.transition_run_if_state(
        run_id,
        "queued",
        "needs_input",
        error_text="Explicit authorization is required",
        failure_class="capability_authorization_horizon_expired",
        failure_user_message="Explicit authorization is required",
    )
    store.update_worker_state(
        record["worker_id"], "needs_input", last_error="Explicit authorization is required"
    )

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["attention"]["kind"] == "auth"
    assert (
        detail.json()["attention"]["code"]
        == "capability_authorization_horizon_expired"
    )

    new_max = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    invalid = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "resume",
            "idempotencyKey": "action-reauthorize-wrong-scope",
            "capabilityReauthorization": {
                "version": 1,
                "authorizationRef": "gha_synthetic_reauthorization_ref",
                "maxExpiresAt": new_max,
                "scopeFingerprint": "scope_changed_not_allowed",
            },
        },
    )
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "capability_reauthorization_invalid"
    assert store.get_run(run_id)["state"] == "needs_input"

    resumed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "resume",
            "idempotencyKey": "action-reauthorize-valid",
            "capabilityReauthorization": {
                "version": 1,
                "authorizationRef": "gha_synthetic_reauthorization_ref",
                "maxExpiresAt": new_max,
                "scopeFingerprint": "scope_synthetic_reauthorization",
            },
        },
    )

    assert resumed.status_code == 202
    assert resumed.json()["state"] == "queued"
    assert resumed.json()["resumeMode"] == "authorization_re_admission"
    assert store.get_run(run_id)["state"] == "queued"
    assert [item["run_id"] for item in store.list_runs_for_worker(record["worker_id"])] == [
        run_id
    ]
    worker = store.get_worker(record["worker_id"])
    persisted = json.loads(worker["bootstrap_bundle_json"])
    assert persisted["glasshive_capability_authorization"]["max_expires_at"] == new_max
    serialized_response = json.dumps(resumed.json())
    assert "gha_synthetic_reauthorization_ref" not in serialized_response
    assert "scope_synthetic_reauthorization" not in serialized_response


@pytest.mark.parametrize(
    "failure_code",
    [
        "capability_policy_denied",
        "capability_account_unavailable",
        "capability_registry_unavailable",
    ],
)
def test_non_horizon_needs_input_is_not_misreported_as_reauthorization(
    account_client,
    failure_code,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-attention-{failure_code}"),
        json=delegation_payload(title=f"Attention {failure_code}"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert store.transition_run_if_state(
        record["current_run_id"],
        "queued",
        "needs_input",
        error_text="Input is required",
        failure_class=failure_code,
        failure_user_message="Input is required",
    )
    store.update_worker_state(record["worker_id"], "needs_input", last_error="")

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )

    assert detail.status_code == 200
    assert detail.json()["attention"]["kind"] == "input"
    assert detail.json()["attention"]["code"] == failure_code


@pytest.mark.parametrize("failure_class,retryable,invoked,allowed", [
    ("provider_temporarily_unavailable", 1, False, True),
    ("service_processor_unexpected", 0, False, True),
    ("service_processor_unexpected", 0, True, False),
    ("capability_authorization_revoked", 0, False, False),
    ("provider_request_rejected", 0, False, False),
    ("approval_required", 0, False, False),
    ("unknown", 0, False, False),
])
def test_active_work_retry_requires_retryable_failure_and_updates_current_run(
    account_client, failure_class, retryable, invoked, allowed
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-retry"),
        json=delegation_payload(title="Retry mission"),
    ).json()
    store = account_client.app.state.store
    before = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    if invoked:
        _truthfully_invoke_run(store, before["current_run_id"], suffix="prior-provider-start")
    store.finalize_run(
        before["current_run_id"],
        "failed",
        error_text="Temporary outage",
        failure_class=failure_class,
        failure_retryable=retryable,
        failure_structured=1,
        failure_user_message="Temporary outage",
    )

    roster = account_client.get("/v1/active-work", headers=account_headers()).json()
    assert ("retry" in roster["work"][0]["actions"]) is allowed
    foreign = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(owner_id="owner-b"),
        json={"action": "retry", "idempotencyKey": "foreign-retry"},
    )
    assert foreign.status_code == 404
    retried = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "retry", "idempotencyKey": "action-retry-once"},
    )

    if not allowed:
        assert retried.status_code == 409
        after = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
        assert after["current_run_id"] == before["current_run_id"]
        return
    assert retried.status_code == 202
    assert retried.json()["state"] == "queued"
    after = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    assert after["current_run_id"] != before["current_run_id"]
    assert store.get_run(after["current_run_id"])["state"] == "queued"


def test_active_work_retry_preserves_new_user_guidance_in_continuation(account_client):
    original_instruction = "Prepare the requested reply using the connected account."
    retry_guidance = "Keep the result as an unsent draft for review. Do not send it."
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-retry-guidance"),
        json=delegation_payload(
            title="Retry with updated guidance",
            instruction=original_instruction,
        ),
    ).json()
    store = account_client.app.state.store
    before = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    store.finalize_run(
        before["current_run_id"],
        "failed",
        error_text="Synthetic provider capacity failure",
        failure_class="provider_context_limit_exceeded",
        failure_retryable=0,
        failure_structured=1,
        failure_user_message="The model request exceeded its available capacity.",
    )

    retried = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "retry",
            "instruction": retry_guidance,
            "idempotencyKey": "action-retry-with-guidance",
        },
    )

    assert retried.status_code == 202
    after = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    retry_run = store.get_run(after["current_run_id"])
    instruction = str(retry_run["instruction"])
    assert f"Prior run task context:\n{original_instruction}" in instruction
    assert f"Continuation context:\n{retry_guidance}" in instruction
    assert json.loads(retry_run["continuation_context_json"]) == {
        "version": 1,
        "base_instruction": original_instruction,
        "guidance": [retry_guidance],
    }


def test_active_work_message_preserves_original_mission_contract_after_retry(account_client):
    original_instruction = (
        "Create a user-facing PDF release checklist in reports/final-checklist.pdf and "
        "keep the final answer concise."
    )
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-message-preserves-contract"),
        json=delegation_payload(
            title="Message preserves mission contract",
            instruction=original_instruction,
        ),
    ).json()
    store = account_client.app.state.store
    initial = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    store.finalize_run(
        initial["current_run_id"],
        "failed",
        error_text="Synthetic retryable provider outage",
        failure_class="provider_temporarily_unavailable",
        failure_retryable=1,
        failure_structured=1,
        failure_user_message="Try again.",
    )

    retried = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "retry", "idempotencyKey": "retry-before-message"},
    )
    assert retried.status_code == 202
    after_retry = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    retry_run_id = after_retry["current_run_id"]

    guidance = "Add an owner sign-off line to the same deliverable."
    messaged = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "message",
            "instruction": guidance,
            "idempotencyKey": "message-preserve-original-contract",
        },
    )

    assert messaged.status_code == 202
    after_message = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    current = store.get_run(after_message["current_run_id"])
    assert current is not None
    assert f"Prior run task context:\n{original_instruction}" in current["instruction"]
    assert f"Continuation context:\n{guidance}" in current["instruction"]
    assert current["instruction"].count(original_instruction) == 1
    assert json.loads(current["continuation_context_json"]) == {
        "version": 1,
        "base_instruction": original_instruction,
        "guidance": [guidance],
    }
    assert current["run_id"] != retry_run_id
    assert store.get_run(retry_run_id)["state"] == "cancelled"
    nonterminal = [
        run
        for run in store.list_runs_for_worker(after_message["worker_id"])
        if run["state"] not in {"completed", "failed", "cancelled", "interrupted"}
    ]
    assert [run["run_id"] for run in nonterminal] == [current["run_id"]]


def test_repeated_queued_messages_preserve_every_unstarted_user_guidance(account_client):
    original_instruction = "Create one benchmark report with a latency table."
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-message-chain"),
        json=delegation_payload(
            title="Queued message chain",
            instruction=original_instruction,
        ),
    ).json()
    store = account_client.app.state.store

    first_guidance = "Add an owner sign-off line to the same report."
    first = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "message",
            "instruction": first_guidance,
            "idempotencyKey": "message-chain-first",
        },
    )
    assert first.status_code == 202
    after_first = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    first_run_id = after_first["current_run_id"]

    # A database claim is not provider execution. Reproduce the dominant
    # installed-capacity path: lease/capacity admission rejects the claimed
    # row and queues it again before the worker ever runs.
    claimed = store.claim_next_queued_run(after_first["worker_id"])
    assert claimed and claimed["run_id"] == first_run_id
    lease = store.get_active_host_run_lease_for_run(first_run_id)
    if lease is not None:
        store.release_host_run_lease(
            lease["lease_id"], executor_id=None, reason="synthetic_capacity_wait"
        )
    capacity_error = HostCapacityError(
        "Host mission lane is still full after lease admission.",
        capacity_class="resource_pressure",
    )
    account_client.app.state.service._requeue_retryable_run(
        store.get_worker(after_first["worker_id"]),
        claimed,
        capacity_error,
    )
    bounced = store.get_run(first_run_id)
    assert bounced and bounced["state"] == "queued"
    assert bounced["started_at"] is None
    assert not bounced["runtime_invoked_at"]
    attempts = store.list_run_attempts(first_run_id)
    assert len(attempts) == 1
    assert attempts[0]["state"] == "retry_queued"

    second_guidance = "Also sort every table by latency."
    second = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "message",
            "instruction": second_guidance,
            "idempotencyKey": "message-chain-second",
        },
    )

    assert second.status_code == 202
    after_second = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    current = store.get_run(after_second["current_run_id"])
    assert current is not None
    assert current["state"] == "queued"
    assert current["instruction"].count(original_instruction) == 1
    assert current["instruction"].count(first_guidance) == 1
    assert current["instruction"].count(second_guidance) == 1
    assert store.get_run(first_run_id)["state"] == "cancelled"
    nonterminal = [
        run
        for run in store.list_runs_for_worker(after_second["worker_id"])
        if run["state"] not in {"completed", "failed", "cancelled", "interrupted"}
    ]
    assert [run["run_id"] for run in nonterminal] == [current["run_id"]]


def test_active_work_exposes_quantitative_capacity_wait_without_false_running(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-capacity-quantitative"),
        json=delegation_payload(title="Quantitative capacity wait"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    claimed = store.claim_next_queued_run(record["worker_id"])
    pressure = HostCapacityError(
        "Host memory cannot cover this worker reservation.",
        capacity_class="resource_pressure",
    )
    pressure.available = {
        "childProcesses": 44,
        "threads": 1536,
        "memoryBytes": 3 * 1024**3,
        "diskBytes": 8 * 1024**3,
    }
    pressure.required = {
        "childProcesses": 21,
        "threads": 513,
        "memoryBytes": 5 * 1024**3,
        "diskBytes": 8 * 1024**3,
    }
    pressure.shortage = {
        "childProcesses": 0,
        "threads": 0,
        "memoryBytes": 2 * 1024**3,
        "diskBytes": 0,
    }
    pressure.reservation = {
        "childProcesses": 20,
        "threads": 512,
        "memoryBytes": 3 * 1024**3,
        "diskBytes": 4 * 1024**3,
    }
    account_client.app.state.service._requeue_retryable_run(
        store.get_worker(record["worker_id"]), claimed, pressure
    )

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )

    assert detail.status_code == 200
    body = detail.json()
    assert body["state"] == "queued"
    assert body["statusSummary"] == "Queued — waiting for host capacity"
    assert body["capacity"] == {
        "class": "resource_pressure",
        "available": pressure.available,
        "required": pressure.required,
        "shortage": pressure.shortage,
        "reservation": pressure.reservation,
        "nextRetryAt": body["capacity"]["nextRetryAt"],
    }
    assert body["capacity"]["nextRetryAt"]
    assert body["lifecycle"]["claimedAt"]
    assert body["lifecycle"]["admittedAt"] is None
    assert body["lifecycle"]["runtimeInvokedAt"] is None
    assert body["lifecycle"]["startedAt"] is None


def test_fifty_queued_messages_survive_replay_capacity_bounces_and_store_reopen(account_client):
    original_instruction = "Produce one synthetic ordered-ledger validation report."
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-message-chain-fifty"),
        json=delegation_payload(
            title="Fifty-message queued continuation",
            instruction=original_instruction,
        ),
    ).json()
    store = account_client.app.state.store
    guidances = [
        f"Guidance {index:02d}: preserve synthetic ledger row {index:02d} in exact order."
        for index in range(1, 51)
    ]

    for index, guidance in enumerate(guidances, start=1):
        idempotency_key = f"message-chain-fifty-{index:02d}"
        first = account_client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json={
                "action": "message",
                "instruction": guidance,
                "idempotencyKey": idempotency_key,
            },
        )
        replay = account_client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json={
                "action": "message",
                "instruction": guidance,
                "idempotencyKey": idempotency_key,
            },
        )
        assert first.status_code == replay.status_code == 202
        assert first.json()["workRef"] == replay.json()["workRef"] == accepted["workRef"]
        assert replay.json()["idempotentReplay"] is True

        current_delegation = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        if index % 10 == 0:
            claimed = store.claim_next_queued_run(current_delegation["worker_id"])
            assert claimed and claimed["run_id"] == current_delegation["current_run_id"]
            lease = store.get_active_host_run_lease_for_run(claimed["run_id"])
            if lease is not None:
                store.release_host_run_lease(
                    lease["lease_id"],
                    executor_id=None,
                    reason="synthetic_capacity_wait",
                )
            account_client.app.state.service._requeue_retryable_run(
                store.get_worker(current_delegation["worker_id"]),
                claimed,
                HostCapacityError(
                    "Synthetic capacity bounce before provider execution.",
                    capacity_class="resource_pressure",
                ),
            )
        if index == 25:
            reopened = Store(str(store.db_path))
            reopened_delegation = reopened.get_delegation(
                accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
            )
            reopened_run = reopened.get_run(reopened_delegation["current_run_id"])
            assert reopened_run and reopened_run["state"] == "queued"
            for persisted in guidances[:index]:
                assert reopened_run["instruction"].count(persisted) == 1

    final_delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    current = store.get_run(final_delegation["current_run_id"])
    assert current and current["state"] == "queued"
    assert current["instruction"].count(original_instruction) == 1
    assert current["instruction"].count("Continue this GlassHive workspace") == 1
    for guidance in guidances:
        assert current["instruction"].count(guidance) == 1
    assert len(current["instruction"]) < sum(map(len, guidances)) + 5_000
    assert json.loads(current["continuation_context_json"]) == {
        "version": 1,
        "base_instruction": original_instruction,
        "guidance": guidances,
    }

    nonterminal = [
        run
        for run in store.list_runs_for_worker(final_delegation["worker_id"])
        if run["state"] not in {"completed", "failed", "cancelled", "interrupted"}
    ]
    assert [run["run_id"] for run in nonterminal] == [current["run_id"]]
    with store._connect() as conn:
        action_count = conn.execute(
            "SELECT COUNT(*) FROM active_work_action_uses WHERE work_ref = ?",
            (accepted["workRef"],),
        ).fetchone()[0]
    assert action_count == len(guidances)


@pytest.mark.parametrize(
    "failure_class",
    [
        "provider_auth_missing",
        "provider_connected_account_reconnect_required",
        "provider_unauthorized",
    ],
)
def test_active_work_provider_auth_failure_is_user_resumable_after_reconnect(
    account_client,
    failure_class,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-auth-resume-{failure_class}"),
        json=delegation_payload(title="Reconnect and resume mission"),
    ).json()
    store = account_client.app.state.store
    before = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    store.finalize_run(
        before["current_run_id"],
        "failed",
        error_text="Synthetic provider authentication rejection",
        failure_class=failure_class,
        failure_retryable=0,
        failure_structured=1,
        failure_user_message="Reconnect the provider account, then resume this work.",
    )

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["actions"] == ["retry", "queue", "message", "dismiss"]

    retried = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "retry",
            "idempotencyKey": f"action-auth-resume-{failure_class}",
        },
    )

    assert retried.status_code == 202
    assert retried.json()["state"] == "queued"
    after = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert after["worker_id"] == before["worker_id"]
    assert after["current_run_id"] != before["current_run_id"]
    assert store.get_run(after["current_run_id"])["state"] == "queued"


@pytest.mark.parametrize("action", ["message", "steer"])
def test_instruction_actions_require_a_nonempty_instruction(account_client, action):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-{action}"),
        json=delegation_payload(title=f"{action.title()} mission"),
    ).json()

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": action, "idempotencyKey": f"action-{action}-once"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "active_work_instruction_required"


def test_active_work_steer_preserves_original_task_for_run_evidence(account_client):
    original_instruction = "Create one responsive HTML status page and save the HTML artifact."
    steer_instruction = "Add a Coverage Gaps section with exactly three bullets."
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-steer-evidence-source"),
        json=delegation_payload(
            title="Steer evidence source",
            instruction=original_instruction,
        ),
    ).json()

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "steer",
            "instruction": steer_instruction,
            "idempotencyKey": "action-steer-evidence-source",
        },
    )

    assert response.status_code == 202, response.text
    service = account_client.app.state.service
    store = account_client.app.state.store
    current = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    replacement = store.get_run(current["current_run_id"])
    assert json.loads(replacement["continuation_context_json"]) == {
        "version": 1,
        "base_instruction": original_instruction,
        "guidance": [steer_instruction],
    }

    runtime_worker = service._run_local_worker(
        store.get_worker(current["worker_id"]), replacement
    )
    runtime_bundle = json.loads(runtime_worker["bootstrap_bundle_json"])
    assert runtime_bundle["viventium_constraint_source"] == {
        "version": 1,
        "instruction": original_instruction,
    }
    ledger = build_constraint_ledger(
        instruction=replacement["instruction"],
        worker=runtime_worker,
        run_id=replacement["run_id"],
    )
    assert ledger["original_output_source"] == original_instruction
    assert ledger["outputs"]["format_expectations"] == []


def test_active_work_queue_persists_a_followup_without_interrupting_current_run(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-queue"),
        json=delegation_payload(title="Queue mission"),
    ).json()
    store = account_client.app.state.store
    before = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )

    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Then compile the findings.",
            "idempotencyKey": "action-queue-once",
        },
    )

    assert queued.status_code == 202
    assert queued.json()["state"] == "queued"
    assert queued.json()["deliveryMode"] == "queued"
    after = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert after["current_run_id"] != before["current_run_id"]
    assert store.get_run(before["current_run_id"])["state"] == "queued"
    assert store.get_run(after["current_run_id"])["state"] == "queued"


def test_active_work_queue_persists_event_time_origin_and_output_authority(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-event-time-follow-up"),
        json=delegation_payload(title="Event-time follow-up"),
    ).json()
    store = account_client.app.state.store
    follow_up_origin = "origin_event_time_follow_up_002"
    follow_up_source_event = "source-event-time-follow-up-002"
    output_contract = {
        "mode": "replace",
        "required": ["Create the final XLSX workbook."],
        "forbidden": [],
        "formats": ["xlsx"],
        "forbiddenFormats": [],
    }

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Continue with the next event-time action.",
            "idempotencyKey": "action-event-time-follow-up",
            "sourceContext": {
                "version": 1,
                "originRef": follow_up_origin,
                "sourceEventId": follow_up_source_event,
                "sourceRevision": 22,
                "surface": "chat",
                "outputContract": output_contract,
            },
        },
    )

    assert response.status_code == 202, response.text
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    follow_up = store.get_run(record["current_run_id"])
    detail = store.work_trace_detail(
        run_id=follow_up["run_id"],
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    assert detail is not None
    assert detail["traceability"]["origin"] == {
        "originRef": "origin_sha256:"
        + hashlib.sha256(follow_up_origin.encode()).hexdigest(),
        "sourceEventRef": "source_sha256:"
        + hashlib.sha256(follow_up_source_event.encode()).hexdigest(),
        "sourceRevision": 22,
        "surface": "chat",
    }
    persisted_contract = json.loads(follow_up["continuation_contract_json"])
    assert persisted_contract == {
        "version": 1,
        "run_id": follow_up["run_id"],
        "source": {
            "source_event_id": follow_up_source_event,
            "source_revision": 22,
            "surface": "chat",
        },
        "output": {
            "mode": "replace",
            "required": ["Create the final XLSX workbook."],
            "forbidden": [],
            "formats": ["xlsx"],
            "forbidden_formats": [],
        },
    }


@pytest.mark.parametrize("action", ["queue", "message"])
@pytest.mark.parametrize("terminal_state", ["completed", "failed", "cancelled", "interrupted"])
def test_active_work_reuses_terminal_mission_for_follow_up(
    account_client,
    action,
    terminal_state,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-terminal-{action}"),
        json=delegation_payload(title=f"Terminal {action}"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    store.finalize_run(
        source_run_id, terminal_state, output_text="Earlier partial work",
        failure_class="unknown" if terminal_state == "failed" else "",
        failure_retryable=0,
    )
    prior_worker = store.get_worker(record["worker_id"])
    store.update_worker_state(record["worker_id"], "ready")

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["actions"] == ["queue", "message", "dismiss"]

    foreign = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(owner_id="owner-b"),
        json={"action": action, "instruction": "Foreign continuation", "idempotencyKey": "foreign-follow-up"},
    )
    assert foreign.status_code == 404
    missing = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": action, "idempotencyKey": "empty-follow-up"},
    )
    assert missing.status_code == 400
    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": action,
            "instruction": "Continue in this exact workspace.",
            "idempotencyKey": f"terminal-{action}-follow-up",
        },
    )

    assert response.status_code == 202, response.text
    runs = store.list_runs_for_worker(record["worker_id"])
    assert len(runs) == 2
    assert store.get_run(source_run_id)["state"] == terminal_state
    assert store.get_run(source_run_id)["output_text"] == "Earlier partial work"
    current = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    follow_up = store.get_run(current["current_run_id"])
    assert follow_up["run_id"] != source_run_id
    assert follow_up["state"] == "queued"
    assert "Continue in this exact workspace." in follow_up["instruction"]

    after_worker = store.get_worker(record["worker_id"])
    for key in ("worker_id", "workspace_dir", "state_dir", "profile", "model", "bootstrap_bundle_json"):
        assert after_worker[key] == prior_worker[key]
    replay = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": action, "instruction": "Continue in this exact workspace.",
              "idempotencyKey": f"terminal-{action}-follow-up"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json() == {**response.json(), "idempotentReplay": True}
    assert len(store.list_runs_for_worker(record["worker_id"])) == 2
    if action == "message":
        assert json.loads(follow_up["continuation_context_json"])["base_instruction"] == "Research alpha deeply"


@pytest.mark.parametrize('action', ['queue', 'message'])
@pytest.mark.parametrize('operator_paused', [False, True])
def test_completed_mission_followup_wakes_idle_compute_but_preserves_operator_pause(
    account_client, monkeypatch, action, operator_paused,
):
    accepted = account_client.post(
        '/v1/delegations', headers=account_headers(idempotency_key='idle-followup'),
        json=delegation_payload(title='Continue a completed review'),
    ).json()
    service = account_client.app.state.service
    store = service.store
    record = store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    source_run_id = record['current_run_id']
    store.finalize_run(source_run_id, 'completed', output_text='The first review is complete.')
    store.update_worker_state(record['worker_id'], 'ready')
    monkeypatch.setattr(service, '_idle_terminate_after_s', lambda: 1)
    monkeypatch.setattr(service, '_worker_idle_seconds', lambda _worker: 2)
    assert service.reap_idle_workers_once()
    worker = store.get_worker(record['worker_id'])
    assert worker['state'] == 'paused' and worker['compute_released_at']
    if operator_paused:
        service.pause_worker(record['worker_id'])
        assert store.has_active_operator_pause(record['worker_id'])
    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions", headers=account_headers(),
        json={'action': action, 'instruction': 'Review the revised source.',
              'idempotencyKey': 'continue-idle-review'},
    )
    assert response.status_code == 202, response.text
    assert store.get_worker(record['worker_id'])['state'] == ('paused' if operator_paused else 'starting')
    assert store.get_run(source_run_id)['state'] == 'completed'
    assert len(store.list_runs_for_worker(record['worker_id'])) == 2


def test_startup_recovers_already_queued_idle_followup_without_replacing_it(account_client):
    accepted = account_client.post(
        '/v1/delegations', headers=account_headers(idempotency_key='idle-recovery'),
        json=delegation_payload(title='Recover pending review'),
    ).json()
    service = account_client.app.state.service
    store = service.store
    record = store.get_delegation(accepted['workRef'], tenant_id='tenant-a', owner_id='owner-a')
    store.update_worker(record['worker_id'], state='paused', compute_released_at=datetime.now(timezone.utc).isoformat())
    store.add_event(record['project_id'], record['worker_id'], None, 'worker.idle_terminated', 'Idle compute released')
    started = []
    service._ensure_worker_processor = started.append
    service._reconcile_worker_row(store.get_worker(record['worker_id']))
    assert started == [record['worker_id']]
    assert store.get_worker(record['worker_id'])['state'] == 'starting'
    assert store.get_run(record['current_run_id'])['state'] == 'queued'
    assert len(store.list_runs_for_worker(record['worker_id'])) == 1


def test_active_work_rejects_steer_after_mission_completed(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-terminal-steer"),
        json=delegation_payload(title="Terminal steer"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    store.finalize_run(record["current_run_id"], "completed", output_text="Done")
    store.update_worker_state(record["worker_id"], "ready")

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "steer",
            "instruction": "This must not create a hidden continuation.",
            "idempotencyKey": "terminal-steer-forbidden",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_work_action_not_available"
    assert len(store.list_runs_for_worker(record["worker_id"])) == 1


def test_active_work_stop_cancels_running_mission_and_queued_followup_without_false_success(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-with-followup"),
        json=delegation_payload(title="Stop mission with followup"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    _truthfully_invoke_run(store, source_run_id, suffix="stop-with-followup")
    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Follow up after the current run.",
            "idempotencyKey": "queue-before-mission-stop",
        },
    )
    assert queued.status_code == 202

    stopped = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "stop-whole-mission"},
    )

    assert stopped.status_code == 202, stopped.text
    assert stopped.json()["state"] == "cancelled"
    runs = store.list_runs_for_worker(record["worker_id"])
    assert {run["state"] for run in runs} == {"cancelled"}


def test_active_work_pause_targets_running_run_ahead_of_queued_followup(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-pause-running-followup"),
        json=delegation_payload(title="Pause running mission with followup"),
    ).json()
    service = account_client.app.state.service
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    _truthfully_invoke_run(store, source_run_id, suffix="pause-with-followup")
    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Run only after the active work resumes and completes.",
            "idempotencyKey": "queue-before-exact-pause",
        },
    )
    assert queued.status_code == 202, queued.text
    queued_run_id = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["current_run_id"]
    pause_targets: list[str] = []
    original_pause = service.runtime.pause_worker

    def capture_pause(worker):
        pause_targets.append(str(worker.get("_active_run_id") or ""))
        return original_pause(worker)

    service.runtime.pause_worker = capture_pause
    paused = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "pause", "idempotencyKey": "pause-exact-running"},
    )

    assert paused.status_code == 202, paused.text
    assert pause_targets == [source_run_id]
    assert store.get_run(source_run_id)["state"] == "paused"
    assert store.get_run(queued_run_id)["state"] == "queued"


def test_active_work_queue_does_not_resume_paused_run_and_resume_targets_it_exactly(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-resume-paused-followup"),
        json=delegation_payload(title="Resume paused mission with followup"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    _truthfully_pause_run(store, source_run_id, suffix="resume-with-followup")

    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Remain queued behind the paused exact run.",
            "idempotencyKey": "queue-behind-paused",
        },
    )
    assert queued.status_code == 202, queued.text
    queued_run_id = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["current_run_id"]
    assert store.get_worker(record["worker_id"])["state"] == "paused"
    assert store.get_run(source_run_id)["state"] == "paused"
    assert store.get_run(queued_run_id)["state"] == "queued"

    resumed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "resume", "idempotencyKey": "resume-exact-paused"},
    )

    assert resumed.status_code == 202, resumed.text
    assert store.get_run(source_run_id)["state"] == "running"
    assert store.get_run(queued_run_id)["state"] == "queued"


def test_unproven_paused_generation_cannot_resume_to_running(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-unproven-resume"),
        json=delegation_payload(title="Reject unproven resume"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    lease = store.get_active_host_run_lease_for_run(record["run_id"])
    assert lease is not None
    store.release_host_run_lease(
        lease["lease_id"], executor_id=None, reason="synthetic_legacy_pause"
    )
    first_started_at = datetime.now(timezone.utc).isoformat()
    assert store.transition_run_if_state(
        record["run_id"],
        "queued",
        "paused",
        started_at=first_started_at,
        error_text="Legacy paused row without invocation",
    )
    store.update_worker_state(record["worker_id"], "paused", last_error="")

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "resume", "idempotencyKey": "reject-unproven-resume"},
    )

    durable = store.get_run(record["run_id"])
    assert response.status_code == 409
    assert durable["state"] == "paused"
    assert durable["runtime_invoked_at"] is None
    assert durable["started_at"] == first_started_at


def test_active_work_stop_closes_needs_input_run_and_queued_followup(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-needs-input-followup"),
        json=delegation_payload(title="Stop needs-input mission with followup"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert store.transition_run_if_state(
        record["current_run_id"],
        "queued",
        "needs_input",
        error_text="Authorization required",
        failure_class="capability_authorization_horizon_expired",
    )
    store.update_worker_state(record["worker_id"], "needs_input", last_error="Authorization required")
    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Queued after authorization.",
            "idempotencyKey": "queue-after-needs-input",
        },
    )
    assert queued.status_code == 202, queued.text

    stopped = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "stop-needs-input-work"},
    )

    assert stopped.status_code == 202, stopped.text
    assert {run["state"] for run in store.list_runs_for_worker(record["worker_id"])} == {
        "cancelled"
    }


def test_queued_followup_does_not_hide_or_overtake_needs_input_source(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-needs-input-followup-order"),
        json=delegation_payload(title="Needs-input source before follow-up"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    assert store.transition_run_if_state(
        source_run_id,
        "queued",
        "needs_input",
        error_text="Provider authorization projection is unavailable",
        failure_class="provider_auth_projection_unavailable",
        failure_user_message="Reconnect the provider account and resume.",
    )
    store.update_worker_state(
        record["worker_id"],
        "needs_input",
        last_error="Provider authorization projection is unavailable",
    )

    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Run only after the source objective can resume.",
            "idempotencyKey": "queue-behind-needs-input",
        },
    )
    assert queued.status_code == 202, queued.text
    followup_run_id = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["current_run_id"]
    assert followup_run_id != source_run_id

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["state"] == "needs_input"
    assert "resume" in detail.json()["actions"]
    assert "steer" not in detail.json()["actions"]

    resumed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "resume",
            "idempotencyKey": "resume-needs-input-before-followup",
        },
    )
    assert resumed.status_code == 202, resumed.text
    with sqlite3.connect(store.db_path) as conn:
        action_use_id = conn.execute(
            "SELECT action_use_id FROM active_work_action_uses WHERE idempotency_key = ?",
            ("resume-needs-input-before-followup",),
        ).fetchone()[0]
    action_row = store.get_active_work_action(action_use_id)
    assert action_row["lifecycle_target_run_id"] == source_run_id
    assert store.get_run(source_run_id)["state"] == "queued"
    assert store.get_run(followup_run_id)["state"] == "queued"


def test_run_terminal_callback_marks_work_nonterminal_while_followup_is_queued(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-callback-followup"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_synthetic_callback_followup",
            title="Callback work truth",
        ),
    ).json()
    service = account_client.app.state.service
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source = store.get_run(record["current_run_id"])
    followup = service.assign_run(
        record["worker_id"],
        "Queued sibling",
        start_processor=False,
        idempotency_key="callback-followup-sibling",
    )
    completed = store.finalize_run(source["run_id"], "completed", output_text="First done")
    worker = store.get_worker(record["worker_id"])
    service._emit_callback(worker, "run.completed", run=completed, message="First done")
    with sqlite3.connect(store.db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM callback_outbox WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (source["run_id"],),
            ).fetchone()[0]
        )
    assert payload["work_ref"] == accepted["workRef"]
    assert payload["work_state"] == "queued"
    assert payload["work_terminal"] is False
    assert payload["callback_id"].startswith("cb_run_terminal_")
    assert "result_state" not in payload
    assert "result_revision" not in payload
    assert "result_digest" not in payload
    assert "result_ended_at" not in payload
    assert source["run_id"] not in {
        row["run_id"] for row in store.list_terminal_runs_missing_callback_intent()
    }

    terminal = store.finalize_run(followup["run_id"], "completed", output_text="All done")
    service._emit_callback(worker, "run.completed", run=terminal, message="All done")
    with sqlite3.connect(store.db_path) as conn:
        final_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM callback_outbox WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (followup["run_id"],),
            ).fetchone()[0]
        )
    assert final_payload["work_state"] == "completed"
    assert final_payload["work_terminal"] is True
    assert final_payload["callback_id"].startswith("cb_terminal_")
    assert final_payload["result_state"] == "completed"
    assert final_payload["result_revision"] >= 1
    assert final_payload["result_digest"].startswith("sha256:")


def test_active_work_dismiss_hides_terminal_card_without_deleting_history(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-dismiss"),
        json=delegation_payload(title="Dismiss mission"),
    ).json()
    store = account_client.app.state.store
    row = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    store.finalize_run(row["current_run_id"], "completed", output_text="Done")

    dismissed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "dismiss", "idempotencyKey": "action-dismiss-once"},
    )
    roster = account_client.get("/v1/active-work", headers=account_headers())

    assert dismissed.status_code == 202
    assert dismissed.json()["state"] == "completed"
    assert roster.json()["work"] == []
    assert store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["dismissed_at"]


def test_active_work_dismiss_remains_available_after_permanent_work_stop(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-then-dismiss"),
        json=delegation_payload(title="Stopped mission to dismiss"),
    ).json()

    stopped = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-before-dismiss"},
    )
    dismissed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "dismiss", "idempotencyKey": "action-dismiss-after-stop"},
    )

    assert stopped.status_code == 202
    assert stopped.json()["state"] == "cancelled"
    assert dismissed.status_code == 202
    assert dismissed.json()["state"] == "cancelled"
    assert account_client.get("/v1/active-work", headers=account_headers()).json()["work"] == []


@pytest.mark.parametrize(
    ("action", "instruction"),
    [
        ("queue", "Queue the durable follow-up."),
        ("message", "Send the durable message."),
        ("steer", "Steer to the durable objective."),
        ("pause", None),
        ("resume", None),
        ("stop", None),
        ("retry", None),
        ("dismiss", None),
    ],
)
def test_active_work_action_reconciles_crash_after_effect_without_duplicate(
    tmp_path,
    monkeypatch,
    action,
    instruction,
):
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    db_path = str(tmp_path / f"action-crash-{action}.sqlite3")
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-crash-{action}"),
            json=delegation_payload(title=f"Crash {action} mission"),
        ).json()
        store = app.state.store
        before = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        if action == "resume":
            _truthfully_pause_run(
                store,
                before["current_run_id"],
                suffix="action-crash-resume",
            )
            before = store.get_delegation(
                accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
            )
        elif action == "retry":
            store.finalize_run(
                before["current_run_id"],
                "failed",
                error_text="Temporary outage",
                failure_class="provider_temporarily_unavailable",
                failure_retryable=1,
                failure_structured=1,
                failure_user_message="Temporary outage",
            )
        elif action == "dismiss":
            store.finalize_run(before["current_run_id"], "completed", output_text="Done")

        request_body = {
            "action": action,
            "idempotencyKey": f"action-crash-{action}",
        }
        if instruction is not None:
            request_body["instruction"] = instruction
        action_request = {
            "action": action,
            "instruction": instruction or "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=f"action-crash-{action}",
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            executor_id=app.state.service.executor_id,
        )
        result = app.state.service.execute_active_work_action(
            before,
            action=action,
            instruction=instruction or "",
            idempotency_key=f"action-crash-{action}",
            action_use_id=reservation["action_use_id"],
        )
        assert result["run_id"]
        # Simulate a hard process exit after the durable effect and before the
        # separate action-ledger completion write.
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM active_work_action_uses WHERE action_use_id = ?",
                (reservation["action_use_id"],),
            ).fetchone()
        assert row == ("pending",)

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=request_body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotentReplay"] is True
        if action in {"queue", "message"}:
            assert replay.json()["deliveryMode"] == (
                "queued_next_boundary" if action == "message" else "queued"
            )
        with sqlite3.connect(restarted.state.store.db_path) as conn:
            action_rows = conn.execute(
                "SELECT status FROM active_work_action_uses WHERE work_ref = ?",
                (accepted["workRef"],),
            ).fetchall()
        assert action_rows == [("completed",)]
        if action in {"queue", "message", "steer", "retry"}:
            after = restarted.state.store.get_delegation(
                accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
            )
            runs = restarted.state.store.list_runs_for_worker(after["worker_id"])
            assert len(runs) == 2


def test_steer_terminal_winner_lost_response_replays_authoritative_source(
    tmp_path,
    monkeypatch,
):
    """A cancelled steer reservation must never replace terminal source truth."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / "steer-terminal-winner-lost-response.sqlite3")
    runtime = StubRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-steer-terminal-lost-response"
            ),
            json=delegation_payload(title="Steer terminal winner lost response"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        preaccept_lease = store.get_active_host_run_lease_for_run(source["run_id"])
        assert preaccept_lease is not None
        store.release_host_run_lease(
            preaccept_lease["lease_id"],
            executor_id=None,
            reason="synthetic_running_fixture",
        )
        _truthfully_invoke_run(
            store,
            source["run_id"],
            suffix="steer-terminal-winner",
        )
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )

        instruction = "Use the corrected terminal-safe objective."
        idempotency_key = "action-steer-terminal-lost-response"
        action_request = {
            "action": "steer",
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action="steer",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )

        original_interrupt = runtime.interrupt_worker
        interrupt_calls = 0

        def complete_during_interrupt(*args, **kwargs):
            nonlocal interrupt_calls
            interrupt_calls += 1
            assert store.finalize_run_if_state(
                source["run_id"],
                "running",
                "completed",
                output_text="Authoritative completion",
                **_exact_terminal_generation(store, source["run_id"]),
            )
            return original_interrupt(*args, **kwargs)

        runtime.interrupt_worker = complete_during_interrupt
        direct = app.state.service.execute_active_work_action(
            source,
            action="steer",
            instruction=instruction,
            idempotency_key=idempotency_key,
            action_use_id=reservation["action_use_id"],
        )
        replacement_run_id = app.state.service.active_work_effect_run_id(
            source, idempotency_key=idempotency_key
        )
        assert direct["control_outcome"] == "terminal_won"
        assert direct["run_id"] == source["run_id"]
        assert direct["state"] == "completed"
        assert store.get_run(replacement_run_id)["state"] == "cancelled"
        assert store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )["current_run_id"] == source["run_id"]
        assert store.get_active_work_action(reservation["action_use_id"])[
            "status"
        ] == "pending"
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        current = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        reconciled = restarted.state.service.reconcile_active_work_action(
            current,
            action="steer",
            instruction=instruction,
            idempotency_key=idempotency_key,
            source_run_id=source["run_id"],
            action_use_id=reservation["action_use_id"],
        )
        assert reconciled["control_outcome"] == "terminal_won"
        assert reconciled["run_id"] == source["run_id"]
        assert reconciled["state"] == "completed"

        body = {
            "action": "steer",
            "instruction": instruction,
            "idempotencyKey": idempotency_key,
        }
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "completed"
        assert replay.json()["idempotentReplay"] is True
        assert repeated.status_code == 202
        assert repeated.json() == replay.json()
        assert interrupt_calls == 1
        authoritative = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert authoritative["current_run_id"] == source["run_id"]
        assert authoritative["run_state"] == "completed"
        assert restarted.state.store.get_run(replacement_run_id)["state"] == "cancelled"
        action_row = restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )
        assert action_row["status"] == "completed"
        assert json.loads(action_row["response_json"])["state"] == "completed"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_pause_resume_terminal_winner_lost_response_replays_authoritative_source(
    tmp_path,
    monkeypatch,
    action,
):
    """A terminal commit during control remains the replayed public truth."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / f"{action}-terminal-winner-lost-response.sqlite3")
    runtime = StubRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key=f"delegation-{action}-terminal-lost-response"
            ),
            json=delegation_payload(title=f"{action.title()} terminal winner"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        preaccept_lease = store.get_active_host_run_lease_for_run(source["run_id"])
        assert preaccept_lease is not None
        store.release_host_run_lease(
            preaccept_lease["lease_id"],
            executor_id=None,
            reason="synthetic_running_fixture",
        )
        if action == "pause":
            _truthfully_invoke_run(
                store,
                source["run_id"],
                suffix="pause-terminal-winner",
            )
        else:
            _truthfully_pause_run(
                store,
                source["run_id"],
                suffix="resume-terminal-winner",
            )
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        idempotency_key = f"action-{action}-terminal-lost-response"
        action_request = {
            "action": action,
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"]),
            executor_id=app.state.service.executor_id,
        )

        runtime_calls = 0
        original_runtime_control = (
            runtime.pause_worker
            if action == "pause"
            else runtime.ensure_worker_ready
        )

        def complete_during_control(*args, **kwargs):
            nonlocal runtime_calls
            runtime_calls += 1
            assert store.finalize_run_if_state(
                source["run_id"],
                "running" if action == "pause" else "paused",
                "completed",
                output_text="Authoritative completion",
                **_exact_terminal_generation(store, source["run_id"]),
            )
            return original_runtime_control(*args, **kwargs)

        if action == "pause":
            runtime.pause_worker = complete_during_control
        else:
            runtime.ensure_worker_ready = complete_during_control
        direct = app.state.service.execute_active_work_action(
            source,
            action=action,
            idempotency_key=idempotency_key,
            action_use_id=reservation["action_use_id"],
        )
        assert direct["control_outcome"] == "terminal_won"
        assert direct["run_id"] == source["run_id"]
        assert direct["state"] == "completed"
        assert store.get_active_work_action(reservation["action_use_id"])[
            "status"
        ] == "pending"
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {"action": action, "idempotencyKey": idempotency_key}
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "completed"
        assert replay.json()["controlOutcome"] == "terminal_won"
        assert replay.json()["runId"] == source["run_id"]
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert runtime_calls == 1
        authoritative = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert authoritative["current_run_id"] == source["run_id"]
        assert authoritative["run_state"] == "completed"


def test_stop_completion_winner_lost_response_replays_settled_work_tombstone(
    tmp_path,
    monkeypatch,
):
    """Public Stop succeeds when completion wins and replays without a second RPC."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / "stop-completion-winner-lost-response.sqlite3")
    runtime = StubRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-stop-completion-lost-response"
            ),
            json=delegation_payload(title="Stop completion winner"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        preaccept_lease = store.get_active_host_run_lease_for_run(source["run_id"])
        assert preaccept_lease is not None
        store.release_host_run_lease(
            preaccept_lease["lease_id"],
            executor_id=None,
            reason="synthetic_running_fixture",
        )
        _truthfully_invoke_run(
            store,
            source["run_id"],
            suffix="stop-completion-winner",
        )
        sibling = store.create_run(
            source["worker_id"],
            source["project_id"],
            "Queued sibling must be settled",
            state="queued",
        )
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        idempotency_key = "action-stop-completion-lost-response"
        action_request = {
            "action": "stop",
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action="stop",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"]),
            executor_id=app.state.service.executor_id,
        )
        interrupt_calls = 0
        original_interrupt = runtime.interrupt_worker

        def complete_during_interrupt(*args, **kwargs):
            nonlocal interrupt_calls
            interrupt_calls += 1
            assert store.finalize_run_if_state(
                source["run_id"],
                "running",
                "completed",
                output_text="Authoritative completion",
                **_exact_terminal_generation(store, source["run_id"]),
            )
            return original_interrupt(*args, **kwargs)

        runtime.interrupt_worker = complete_during_interrupt
        direct = app.state.service.execute_active_work_action(
            source,
            action="stop",
            idempotency_key=idempotency_key,
            action_use_id=reservation["action_use_id"],
        )
        worker = store.get_worker(source["worker_id"])
        action_row = store.get_active_work_action(reservation["action_use_id"])
        assert direct["control_outcome"] == "terminal_won"
        assert direct["state"] == "completed"
        assert worker["work_stop_id"] == action_row["lifecycle_operation_id"]
        assert worker["work_stop_settled_at"]
        assert worker["work_stop_outcome"] == "completion_won"
        assert store.get_run(sibling["run_id"])["state"] == "cancelled"
        assert action_row["status"] == "pending"
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {"action": "stop", "idempotencyKey": idempotency_key}
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "completed"
        assert replay.json()["controlOutcome"] == "terminal_won"
        assert replay.json()["runId"] == source["run_id"]
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert interrupt_calls == 1
        authoritative = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert authoritative["current_run_id"] == source["run_id"]
        assert authoritative["run_state"] == "completed"


def test_needs_input_resume_lost_response_repairs_from_atomic_action_proof(
    tmp_path,
    monkeypatch,
):
    """Queued state alone is insufficient; replay uses the atomic resume receipt."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    # Hold the normal queued-run scheduler so this case isolates the crash seam
    # before any processor wake. Reconciliation below owns the recovery wake.
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    db_path = str(tmp_path / "needs-input-resume-lost-response.sqlite3")
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-needs-input-resume-lost-response"
            ),
            json=delegation_payload(title="Needs input resume lost response"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert store.transition_run_if_state(
            source["run_id"],
            "queued",
            "needs_input",
            error_text="Input required",
            failure_class="capability_policy_denied",
            failure_user_message="Input required",
        )
        store.update_worker_state(source["worker_id"], "needs_input", last_error="")
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        idempotency_key = "action-needs-input-resume-lost-response"
        action_request = {
            "action": "resume",
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action="resume",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state="needs_input",
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )
        # Simulate process death at the durable seam: the store transaction has
        # re-admitted the exact run, but the API response/action finish is lost.
        effect = store.resume_needs_input_active_work_action(
            reservation["action_use_id"],
            worker_id=source["worker_id"],
            run_id=source["run_id"],
            executor_id=app.state.service.executor_id,
        )
        assert effect
        assert store.get_run(source["run_id"])["state"] == "queued"
        assert store.get_worker(source["worker_id"])["state"] == "starting"
        action_row = store.get_active_work_action(reservation["action_use_id"])
        assert action_row["status"] == "pending"
        assert action_row["effect_phase"] == "authorization_re_admitted"
        with sqlite3.connect(store.db_path) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'run.authorization_resumed'",
                (source["run_id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
        assert event_count == 1

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    processor_wakes: list[str] = []
    restarted.state.service._ensure_worker_processor = processor_wakes.append
    with TestClient(restarted) as client:
        body = {"action": "resume", "idempotencyKey": idempotency_key}
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "queued"
        assert replay.json()["resumeMode"] == "authorization_re_admission"
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert processor_wakes == [source["worker_id"]]
        with sqlite3.connect(restarted.state.store.db_path) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'run.authorization_resumed'",
                (source["run_id"],),
            ).fetchone()[0]
        assert event_count == 1
        assert restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )["status"] == "completed"


def test_provider_progress_stalled_resume_lost_response_replays_same_run_without_drift(
    tmp_path,
    monkeypatch,
):
    """Provider attention resumes the exact durable work after a lost response."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    db_path = str(tmp_path / "provider-progress-resume-lost-response.sqlite3")
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-provider-progress-resume-lost-response"
            ),
            json=delegation_payload(title="Provider progress resume lost response"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        run_id = str(source["run_id"])
        worker_id = str(source["worker_id"])
        workspace_dir = str(tmp_path / "provider-progress-workspace")
        native_session_id = "native_session_provider_progress_synthetic"
        route = {
            "provider_route_profile": "codex-cli",
            "provider_route_runtime": "codex-cli",
            "provider_route_model": "gpt-5.6-sol",
            "provider_route_decision": "configured",
            "provider_route_from_profile": "codex-cli",
            "provider_route_from_runtime": "codex-cli",
            "provider_route_from_model": "gpt-5.6-sol",
            "provider_route_failure_class": "",
            "provider_route_cooldown_until": None,
        }
        store.update_worker(
            worker_id,
            workspace_dir=workspace_dir,
            profile="codex-cli",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        store.update_run(run_id, native_session_id=native_session_id, **route)
        invoked = _truthfully_invoke_run(
            store,
            run_id,
            suffix="provider-progress-resume-lost-response",
        )
        attempt_id = str(invoked["active_attempt_id"])
        observed_at = datetime.now(timezone.utc)
        for source_sequence in range(1, 4):
            transition = store.observe_provider_liveness(
                run_id=run_id,
                expected_attempt_id=attempt_id,
                kind="internal_retry",
                failure_class="provider_internal_retry",
                runtime="codex-cli",
                model="gpt-5.6-sol",
                source_sequence=source_sequence,
                source_digest=hashlib.sha256(
                    f"provider-progress-retry-{source_sequence}".encode("utf-8")
                ).hexdigest(),
                observed_at=(
                    observed_at + timedelta(seconds=source_sequence)
                ).isoformat(),
                retry_limit=3,
            )
            assert transition is not None
        assert transition["state"] == "needs_input"
        store.update_worker(worker_id, compute_released_at=observed_at.isoformat())

        detail = client.get(
            f"/v1/work/{accepted['workRef']}", headers=account_headers()
        )
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["state"] == "needs_input"
        assert detail_body["statusSummary"].startswith("Needs attention")
        assert detail_body["attention"]["code"] == "provider_progress_stalled"
        assert "resume" in detail_body["actions"]
        assert "retry" not in detail_body["actions"]

        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        before_run = store.get_run(run_id)
        before_worker = store.get_worker(worker_id)
        preserved_run = {
            "native_session_id": before_run["native_session_id"],
            **{key: before_run[key] for key in route},
        }
        preserved_worker = {
            "workspace_dir": before_worker["workspace_dir"],
            "profile": before_worker["profile"],
            "runtime": before_worker["runtime"],
            "model": before_worker["model"],
        }
        assert preserved_run["native_session_id"] == native_session_id
        assert preserved_worker == {
            "workspace_dir": workspace_dir,
            "profile": "codex-cli",
            "runtime": "codex-cli",
            "model": "gpt-5.6-sol",
        }

        idempotency_key = "action-provider-progress-resume-lost-response"
        action_request = {
            "action": "resume",
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action="resume",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=run_id,
            expected_source_run_id=run_id,
            expected_source_state="needs_input",
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )
        effect = store.resume_needs_input_active_work_action(
            reservation["action_use_id"],
            worker_id=worker_id,
            run_id=run_id,
            executor_id=app.state.service.executor_id,
        )
        assert effect is not None
        action_row = store.get_active_work_action(reservation["action_use_id"])
        assert action_row["status"] == "pending"
        assert action_row["effect_phase"] == "provider_progress_re_admitted"
        with sqlite3.connect(store.db_path) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'run.resumed'",
                (run_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
        assert event_count == 1

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    processor_wakes: list[str] = []
    restarted.state.service._ensure_worker_processor = processor_wakes.append
    with TestClient(restarted) as client:
        body = {"action": "resume", "idempotencyKey": idempotency_key}
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["workRef"] == accepted["workRef"]
        assert replay.json()["state"] == "queued"
        assert replay.json()["resumeMode"] == "provider_restart_same_run"
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert processor_wakes == [worker_id]

        after = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        after_run = restarted.state.store.get_run(run_id)
        after_worker = restarted.state.store.get_worker(worker_id)
        assert after["work_ref"] == accepted["workRef"]
        assert after["current_run_id"] == run_id
        assert [
            item["run_id"]
            for item in restarted.state.store.list_runs_for_worker(worker_id)
        ] == [run_id]
        assert {
            "native_session_id": after_run["native_session_id"],
            **{key: after_run[key] for key in route},
        } == preserved_run
        assert {
            "workspace_dir": after_worker["workspace_dir"],
            "profile": after_worker["profile"],
            "runtime": after_worker["runtime"],
            "model": after_worker["model"],
        } == preserved_worker
        with sqlite3.connect(restarted.state.store.db_path) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'run.resumed'",
                (run_id,),
            ).fetchone()[0]
        assert event_count == 1
        assert restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )["status"] == "completed"


def test_stop_reconciliation_requires_bound_settled_work_tombstone(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-proof"),
        json=delegation_payload(title="Stop proof"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    sibling = store.create_run(
        record["worker_id"], record["project_id"], "Queued sibling", state="queued"
    )
    reservation = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=accepted["workRef"],
        idempotency_key="action-stop-proof",
        action="stop",
        payload_digest=hashlib.sha256(
            json.dumps(
                {
                    "action": "stop",
                    "instruction": "",
                    "capabilityReauthorization": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        executor_id=account_client.app.state.service.executor_id,
    )
    assert store.transition_run_if_state(record["run_id"], "queued", "cancelled")

    reconciled = account_client.app.state.service.reconcile_active_work_action(
        record,
        action="stop",
        idempotency_key="action-stop-proof",
        source_run_id=record["run_id"],
        action_use_id=reservation["action_use_id"],
    )

    assert reconciled is None
    assert store.get_run(sibling["run_id"])["state"] == "queued"
    assert not store.get_worker(record["worker_id"])["work_stop_id"]
    assert store.get_active_work_action(reservation["action_use_id"])[
        "status"
    ] == "pending"


def test_stop_does_not_accept_an_unrelated_live_control_claim(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-other-claim"),
        json=delegation_payload(title="Stop other claim"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    invoked = _truthfully_invoke_run(
        store,
        record["run_id"],
        suffix="stop-other-claim",
    )
    started_at = str(invoked["started_at"])
    current_worker = store.get_worker(record["worker_id"])
    claim = store.try_claim_worker_compute_release(
        record["worker_id"],
        expected_updated_at=current_worker["updated_at"],
        expected_last_run_id=str(current_worker.get("last_run_id") or ""),
        expected_state="running",
        expected_container_id="",
        owner="other-control",
        ttl_s=300,
        kind="pause_run",
        target_run_id=record["run_id"],
        expected_target_started_at=started_at,
    )
    assert claim

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-other-claim"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_work_stop_not_accepted"
    with sqlite3.connect(store.db_path) as conn:
        action_status = conn.execute(
            "SELECT status FROM active_work_action_uses WHERE work_ref = ?",
            (accepted["workRef"],),
        ).fetchone()[0]
    assert action_status != "completed"
    assert store.get_run(record["run_id"])["state"] == "running"
    durable_worker = store.get_worker(record["worker_id"])
    assert durable_worker["compute_release_kind"] == "pause_run"
    assert not durable_worker["work_stop_id"]


def test_active_work_action_concurrent_replay_stays_pending_and_changed_request_conflicts(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-action-race"),
        json=delegation_payload(title="Action race mission"),
    ).json()
    service = account_client.app.state.service
    original_execute = service.execute_active_work_action
    entered = threading.Event()
    release = threading.Event()

    def blocked_execute(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_execute(*args, **kwargs)

    service.execute_active_work_action = blocked_execute
    result: dict[str, object] = {}

    def first_request():
        result["response"] = account_client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json={
                "action": "queue",
                "instruction": "First exact follow-up.",
                "idempotencyKey": "action-race-shared",
            },
        )

    thread = threading.Thread(target=first_request)
    thread.start()
    assert entered.wait(timeout=5), getattr(result.get("response"), "text", result)
    replay = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "First exact follow-up.",
            "idempotencyKey": "action-race-shared",
        },
    )
    changed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Different follow-up must conflict.",
            "idempotencyKey": "action-race-shared",
        },
    )
    release.set()
    thread.join(timeout=5)
    service.execute_active_work_action = original_execute

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "active_work_action_in_progress"
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "active_work_idempotency_conflict"
    assert result["response"].status_code == 202


def test_definitive_action_failure_replays_the_same_receipt_without_reexecution(
    account_client, monkeypatch
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-action-definitive-failure"),
        json=delegation_payload(title="Definitive action failure"),
    ).json()
    service = account_client.app.state.service
    calls = 0

    def generation_changed(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("active_work_generation_changed")

    monkeypatch.setattr(service, "execute_active_work_action", generation_changed)
    body = {
        "action": "pause",
        "idempotencyKey": "action-definitive-failure-shared",
    }

    first = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json=body,
    )
    replay = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json=body,
    )

    assert first.status_code == 409
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert first.json()["detail"] == {
        "code": "active_work_generation_changed",
        "message": "active work generation changed",
    }
    assert calls == 1


@pytest.mark.parametrize("action", ["steer", "pause", "resume"])
def test_active_work_action_recovers_crash_between_internal_subeffects(
    tmp_path,
    monkeypatch,
    action,
):
    """A committed first subeffect must be finished, never repeated, after restart."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    db_path = str(tmp_path / f"action-internal-crash-{action}.sqlite3")
    runtime = StubRuntime()
    runtime_calls = {"interrupt": 0, "pause": 0, "resume": 0}
    original_interrupt = runtime.interrupt_worker
    original_pause = runtime.pause_worker
    original_resume = runtime.ensure_worker_ready

    def counted_interrupt(*args, **kwargs):
        runtime_calls["interrupt"] += 1
        return original_interrupt(*args, **kwargs)

    def counted_pause(*args, **kwargs):
        runtime_calls["pause"] += 1
        return original_pause(*args, **kwargs)

    def counted_resume(*args, **kwargs):
        runtime_calls["resume"] += 1
        return original_resume(*args, **kwargs)

    runtime.interrupt_worker = counted_interrupt
    runtime.pause_worker = counted_pause
    runtime.ensure_worker_ready = counted_resume
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-internal-{action}"),
            json=delegation_payload(title=f"Internal {action} crash"),
        ).json()
        store = app.state.store
        record = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        run_id = record["current_run_id"]
        if action in {"steer", "pause"}:
            _truthfully_invoke_run(
                store,
                run_id,
                suffix=f"internal-crash-{action}",
            )
        else:
            _truthfully_pause_run(
                store,
                run_id,
                suffix="internal-crash-resume",
            )
        record = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )

        instruction = "Use the corrected objective." if action == "steer" else ""
        action_request = {
            "action": action,
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=f"action-internal-{action}",
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            executor_id=app.state.service.executor_id,
        )

        original_run_finalizer = store.finalize_worker_run_control_claim
        original_steer_finalizer = store.finalize_worker_steer_claim

        def crash_run_finalizer(*args, **kwargs):
            raise SystemExit("simulated crash after runtime receipt and before run finalizer")

        def crash_steer_finalizer(*args, **kwargs):
            raise SystemExit("simulated crash after runtime receipt and before steer finalizer")

        if action == "steer":
            store.finalize_worker_steer_claim = crash_steer_finalizer
        else:
            store.finalize_worker_run_control_claim = crash_run_finalizer
        with pytest.raises(SystemExit):
            app.state.service.execute_active_work_action(
                record,
                action=action,
                instruction=instruction,
                idempotency_key=f"action-internal-{action}",
                action_use_id=reservation["action_use_id"],
            )
        store.finalize_worker_steer_claim = original_steer_finalizer
        store.finalize_worker_run_control_claim = original_run_finalizer

        with sqlite3.connect(store.db_path) as conn:
            pending = conn.execute(
                "SELECT status, effect_phase FROM active_work_action_uses WHERE action_use_id = ?",
                (reservation["action_use_id"],),
            ).fetchone()
        assert pending[0] == "pending"
        assert pending[1] or (
            store.get_worker(record["worker_id"])["compute_release_runtime_confirmed_at"]
        )
        # A different process may take over only after both durable owner
        # leases expire. The runtime receipt then lets it finalize without
        # repeating the provider action.
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", record["worker_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {
            "action": action,
            "idempotencyKey": f"action-internal-{action}",
        }
        if instruction:
            body["instruction"] = instruction
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotentReplay"] is True
        if replay.json()["confirmationPending"]:
            # Startup recovery and the replay request may race to claim the
            # same expired operation.  The public contract truthfully returns
            # the exact bound pending receipt; wait for that owner to settle,
            # then prove the same key converges without another runtime call.
            for _ in range(200):
                if not restarted.state.store.get_worker(record["worker_id"])[
                    "compute_release_token"
                ]:
                    break
                time.sleep(0.01)
            replay = client.post(
                f"/v1/work/{accepted['workRef']}/actions",
                headers=account_headers(),
                json=body,
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["idempotentReplay"] is True
        assert replay.json()["confirmationPending"] is False
        durable = restarted.state.store.get_run(run_id)
        assert durable["state"] == (
            "interrupted" if action == "steer" else "paused" if action == "pause" else "running"
        ) or (action == "resume" and durable["state"] == "queued")
        if action == "steer":
            worker_runs = restarted.state.store.list_runs_for_worker(record["worker_id"])
            assert len(worker_runs) == 2
            assert runtime_calls["interrupt"] == 1
        elif action == "pause":
            assert runtime_calls["pause"] == 1
        else:
            assert runtime_calls["resume"] == 1


@pytest.mark.parametrize("action", ["pause", "resume", "steer", "stop"])
def test_control_claim_and_action_binding_survive_immediate_process_exit(
    tmp_path,
    monkeypatch,
    action,
):
    """Claim publication and receipt binding are one durable transaction."""

    class TrackingRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.pause_calls = 0
            self.resume_calls = 0
            self.interrupt_calls = 0

        def pause_worker(self, worker):
            self.pause_calls += 1
            return super().pause_worker(worker)

        def ensure_worker_ready(self, worker):
            self.resume_calls += 1
            return super().ensure_worker_ready(worker)

        def interrupt_worker(self, worker, run_id=None):
            self.interrupt_calls += 1
            return super().interrupt_worker(worker, run_id=run_id)

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_startup_recovery",
        lambda self: None,
    )
    db_path = str(tmp_path / f"atomic-claim-binding-{action}.sqlite3")
    runtime = TrackingRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    instruction = "Use the atomic replacement objective." if action == "steer" else ""
    idempotency_key = f"action-atomic-claim-binding-{action}"
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key=f"delegation-atomic-claim-binding-{action}"
            ),
            json=delegation_payload(title=f"Atomic claim binding {action}"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        if action == "resume":
            preaccept_lease = store.get_active_host_run_lease_for_run(
                source["run_id"]
            )
            assert preaccept_lease is not None
            store.release_host_run_lease(
                preaccept_lease["lease_id"],
                executor_id=None,
                reason="synthetic_paused_fixture",
            )
            assert store.transition_run_if_state(
                source["run_id"], "queued", "paused"
            )
            store.update_worker_state(source["worker_id"], "paused", last_error="")
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        action_request = {
            "action": action,
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )

        if action == "stop":
            original_claim = store.try_claim_worker_compute_release

            def crash_after_claim(*args, **kwargs):
                claim = original_claim(*args, **kwargs)
                assert claim
                raise SystemExit("simulated exit immediately after claim transaction")

            store.try_claim_worker_compute_release = crash_after_claim
        else:
            original_claim = app.state.service._claim_exact_run_control

            def crash_after_claim(*args, **kwargs):
                claim = original_claim(*args, **kwargs)
                assert claim
                raise SystemExit("simulated exit immediately after claim transaction")

            app.state.service._claim_exact_run_control = crash_after_claim
        with pytest.raises(SystemExit):
            app.state.service.execute_active_work_action(
                source,
                action=action,
                instruction=instruction,
                idempotency_key=idempotency_key,
                action_use_id=reservation["action_use_id"],
            )
        action_row = store.get_active_work_action(reservation["action_use_id"])
        claimed_worker = store.get_worker(source["worker_id"])
        assert action_row["status"] == "pending"
        assert action_row["lifecycle_operation_id"]
        assert (
            action_row["lifecycle_operation_id"]
            == claimed_worker["compute_release_operation_id"]
        )
        assert action_row["lifecycle_target_run_id"] == source["run_id"]
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", source["worker_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {"action": action, "idempotencyKey": idempotency_key}
        if instruction:
            body["instruction"] = instruction
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotentReplay"] is True
        if replay.json()["confirmationPending"]:
            for _ in range(200):
                if not restarted.state.store.get_worker(source["worker_id"])[
                    "compute_release_token"
                ]:
                    break
                time.sleep(0.01)
            replay = client.post(
                f"/v1/work/{accepted['workRef']}/actions",
                headers=account_headers(),
                json=body,
            )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["confirmationPending"] is False
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )["status"] == "completed"
        assert not restarted.state.store.get_worker(source["worker_id"])[
            "compute_release_token"
        ]
        if action == "pause":
            assert replay.json()["state"] == "paused"
            assert runtime.pause_calls == 0
        elif action == "resume":
            assert replay.json()["state"] in {"queued", "running"}
            assert runtime.resume_calls == 1
        elif action == "steer":
            assert replay.json()["state"] == "queued"
            assert runtime.interrupt_calls == 0
        else:
            assert replay.json()["state"] == "cancelled"
            assert runtime.interrupt_calls == 0


def test_legacy_active_work_action_schema_migrates_source_run_receipt(tmp_path):
    db_path = tmp_path / "legacy-action-ledger.sqlite3"
    app = create_app(db_path=str(db_path), runtime_backend="stub", runtime=StubRuntime())
    app.state.service.shutdown()
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE active_work_action_uses DROP COLUMN source_run_id")
        conn.execute("ALTER TABLE active_work_action_uses DROP COLUMN effect_phase")

    migrated = create_app(
        db_path=str(db_path), runtime_backend="stub", runtime=StubRuntime()
    )
    migrated.state.service.shutdown()
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(active_work_action_uses)"
            ).fetchall()
        }
    assert "source_run_id" in columns
    assert "effect_phase" in columns


def test_legacy_pending_control_receipt_without_operation_proof_fails_closed(
    tmp_path,
    monkeypatch,
):
    """Migration defaults must not turn unrelated paused state into replay success."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / "legacy-control-operation-receipt.sqlite3")
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-legacy-control-operation"
            ),
            json=delegation_payload(title="Legacy operation receipt"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        action_request = {
            "action": "pause",
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key="action-legacy-control-operation",
            action="pause",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )
        # This paused state is deliberately not causally owned by the receipt.
        assert store.transition_run_if_state(source["run_id"], "queued", "paused")
        store.update_worker_state(source["worker_id"], "paused", last_error="")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE active_work_action_uses DROP COLUMN lifecycle_operation_id"
        )
        conn.execute(
            "ALTER TABLE active_work_action_uses DROP COLUMN lifecycle_operation_kind"
        )
        conn.execute(
            "ALTER TABLE active_work_action_uses DROP COLUMN lifecycle_target_run_id"
        )

    migrated = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    migrated.state.service.start_assigned_run = lambda _worker_id: None
    migrated.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(migrated):
        migrated_action = migrated.state.store.get_active_work_action(
            reservation["action_use_id"]
        )
        assert migrated_action["status"] == "pending"
        assert migrated_action["lifecycle_operation_id"] == ""
        assert migrated_action["lifecycle_operation_kind"] == ""
        assert migrated_action["lifecycle_target_run_id"] == ""
        current = migrated.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert migrated.state.service.reconcile_active_work_action(
            current,
            action="pause",
            idempotency_key="action-legacy-control-operation",
            source_run_id=source["run_id"],
            action_use_id=reservation["action_use_id"],
        ) is None


@pytest.mark.parametrize("action", ["pause", "resume", "steer"])
def test_unbound_control_receipt_pending_and_failed_replays_require_reissue(
    tmp_path,
    monkeypatch,
    action,
):
    """An unbound legacy control receipt never infers or re-executes an effect."""

    class TrackingRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.pause_calls = 0
            self.resume_calls = 0
            self.interrupt_calls = 0

        def pause_worker(self, worker):
            self.pause_calls += 1
            return super().pause_worker(worker)

        def ensure_worker_ready(self, worker):
            self.resume_calls += 1
            return super().ensure_worker_ready(worker)

        def interrupt_worker(self, worker, run_id=None):
            self.interrupt_calls += 1
            return super().interrupt_worker(worker, run_id=run_id)

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    db_path = str(tmp_path / f"unbound-{action}-receipt.sqlite3")
    runtime = TrackingRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    instruction = "Use the replacement objective." if action == "steer" else ""
    idempotency_key = f"action-unbound-{action}"
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-unbound-{action}"),
            json=delegation_payload(title=f"Unbound {action} receipt"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        if action == "resume":
            _truthfully_pause_run(
                store,
                source["run_id"],
                suffix="unbound-resume",
            )
        elif action == "steer":
            _truthfully_invoke_run(
                store,
                source["run_id"],
                suffix="unbound-steer",
            )
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        action_request = {
            "action": action,
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )
        if action == "pause":
            assert store.transition_run_if_state(
                source["run_id"], "queued", "paused"
            )
            store.update_worker_state(source["worker_id"], "paused", last_error="")
        elif action == "resume":
            assert store.transition_run_if_state(
                source["run_id"],
                "paused",
                "running",
            )
            store.update_worker_state(source["worker_id"], "running", last_error="")
        else:
            replacement_run_id = app.state.service.active_work_effect_run_id(
                source, idempotency_key=idempotency_key
            )
            store.create_idempotent_run(
                run_id=replacement_run_id,
                worker_id=source["worker_id"],
                project_id=source["project_id"],
                instruction=app.state.service._instruction_for_steer(instruction),
            )
            assert store.transition_run_if_state(
                source["run_id"], "running", "interrupted"
            )
        assert app.state.service.reconcile_active_work_action(
            source,
            action=action,
            instruction=instruction,
            idempotency_key=idempotency_key,
            source_run_id=source["run_id"],
            action_use_id=reservation["action_use_id"],
        ) is None
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
        before_states = {
            run["run_id"]: run["state"]
            for run in store.list_runs_for_worker(source["worker_id"])
        }

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        before_replay_calls = (
            runtime.pause_calls,
            runtime.resume_calls,
            runtime.interrupt_calls,
        )
        body = {"action": action, "idempotencyKey": idempotency_key}
        if instruction:
            body["instruction"] = instruction
        pending_replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        failed_replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert pending_replay.status_code == 409
        assert failed_replay.status_code == 409
        assert pending_replay.json()["detail"]["code"] == (
            "active_work_action_binding_unavailable"
        )
        assert failed_replay.json()["detail"] == pending_replay.json()["detail"]
        action_row = restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )
        assert action_row["status"] == "failed"
        assert action_row["last_error"] == "active_work_action_binding_unavailable"
        assert {
            run["run_id"]: run["state"]
            for run in restarted.state.store.list_runs_for_worker(source["worker_id"])
        } == before_states
        assert (
            runtime.pause_calls,
            runtime.resume_calls,
            runtime.interrupt_calls,
        ) == before_replay_calls


def test_active_work_roster_caps_rows_and_reports_overflow(account_client):
    for index in range(3):
        account_client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-overflow-{index}"),
            json=delegation_payload(title=f"Overflow {index}"),
        )

    roster = account_client.get("/v1/active-work?limit=2", headers=account_headers())

    assert roster.status_code == 200
    assert roster.json()["snapshot"] == "fresh"
    assert len(roster.json()["work"]) == 2
    assert roster.json()["overflowCount"] == 1


def test_active_work_cursor_pagination_is_stable_and_complete(
    account_client, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "8")
    monkeypatch.setenv("WPR_HOST_ACCOUNT_ACTIVE_LIMIT", "8")
    monkeypatch.setenv("WPR_HOST_MAX_CHILD_PROCESSES", "256")
    monkeypatch.setenv("WPR_HOST_MAX_THREADS", "4096")
    for index in range(4):
        response = account_client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-cursor-{index}"),
            json=delegation_payload(title=f"Cursor {index}"),
        )
        assert response.status_code == 202

    # Force identical timestamps so the opaque cursor must use workRef as its
    # deterministic final key, not rely on insertion-order accidents.
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        conn.execute(
            "UPDATE delegations SET updated_at = ?, created_at = ?",
            ("2026-08-12T12:00:00+00:00", "2026-08-12T12:00:00+00:00"),
        )

    collected: list[str] = []
    cursor: str | None = None
    remaining = [3, 2, 1, 0]
    for expected_overflow in remaining:
        suffix = f"&cursor={cursor}" if cursor else ""
        page = account_client.get(
            f"/v1/active-work?limit=1{suffix}",
            headers=account_headers(),
        )
        assert page.status_code == 200
        body = page.json()
        assert len(body["work"]) == 1
        assert body["overflowCount"] == expected_overflow
        collected.append(body["work"][0]["workRef"])
        cursor = body.get("cursor")
        assert bool(cursor) is (expected_overflow > 0)

    assert len(collected) == len(set(collected)) == 4

    invalid = account_client.get(
        "/v1/active-work?limit=1&cursor=not-a-valid-cursor",
        headers=account_headers(),
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "active_work_cursor_invalid"


def test_callback_association_is_authoritative_owner_scoped_and_non_oracular(account_client):
    service = account_client.app.state.service
    service.executor.submit = lambda *_args, **_kwargs: None
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-callback-association"),
        json=delegation_payload_with_origin(),
    )
    assert accepted.status_code == 202

    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    initial_run_id = delegation["initial_run_id"]
    request_body = {
        "originRef": "ghi_synthetic_origin_0001",
        "workRef": accepted.json()["workRef"],
        "workerId": delegation["worker_id"],
        "runId": initial_run_id,
    }
    verified = account_client.post(
        "/v1/callback-associations/verify",
        headers=account_headers(),
        json=request_body,
    )
    assert verified.status_code == 200
    assert verified.json() == {
        "valid": True,
        "originRef": "ghi_synthetic_origin_0001",
        "workRef": accepted.json()["workRef"],
    }

    linked_run = store.create_run(
        delegation["worker_id"], delegation["project_id"], "Linked continuation"
    )
    linked = account_client.post(
        "/v1/callback-associations/verify",
        headers=account_headers(),
        json={**request_body, "runId": linked_run["run_id"]},
    )
    assert linked.status_code == 200

    mismatches = [
        {**request_body, "originRef": "ghi_synthetic_origin_wrong"},
        {**request_body, "workRef": "work_synthetic_wrong"},
        {**request_body, "workerId": "wrk_synthetic_wrong"},
        {**request_body, "runId": "run_synthetic_wrong"},
    ]
    for mismatch in mismatches:
        response = account_client.post(
            "/v1/callback-associations/verify",
            headers=account_headers(),
            json=mismatch,
        )
        assert response.status_code == 404
        assert response.json() == {
            "detail": {
                "code": "callback_association_not_found",
                "message": "The callback association was not found.",
            }
        }

    foreign = account_client.post(
        "/v1/callback-associations/verify",
        headers=account_headers(owner_id="owner-b"),
        json=request_body,
    )
    assert foreign.status_code == 404
    assert foreign.json() == {
        "detail": {
            "code": "callback_association_not_found",
            "message": "The callback association was not found.",
        }
    }

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM callback_outbox ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    callback_payload = json.loads(row[0])
    assert callback_payload["origin_ref"] == "ghi_synthetic_origin_0001"
    assert callback_payload["work_ref"] == accepted.json()["workRef"]


def test_viventium_callback_uses_durable_origin_binding_without_parent_identity(account_client):
    service = account_client.app.state.service
    service.executor.submit = lambda *_args, **_kwargs: None
    payload = delegation_payload_with_origin(
        origin_ref="ghi_synthetic_opaque_callback_origin",
        title="Opaque callback routing",
    )
    payload["bootstrapBundle"]["callbacks"]["events_webhook_url"] = (
        "http://localhost:3080/api/viventium/glasshive/callback"
    )
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-opaque-callback-routing"),
        json=payload,
    )
    assert accepted.status_code == 202, accepted.text

    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    worker = store.get_worker(delegation["worker_id"])
    run = store.get_run(delegation["initial_run_id"])

    record = service._emit_callback(
        worker,
        "run.needs_input",
        run=run,
        message="Connected account authorization is required.",
        submit_delivery=False,
    )

    assert record is not None
    callback_payload = json.loads(record["payload_json"])
    assert callback_payload["origin_ref"] == "ghi_synthetic_opaque_callback_origin"
    assert callback_payload["work_ref"] == accepted.json()["workRef"]
    assert callback_payload["user_id"] is None
    assert callback_payload["conversation_id"] is None
    assert callback_payload["parent_message_id"] is None
    assert callback_payload["message_id"] is None

    forged_worker = dict(worker)
    forged_bundle = json.loads(forged_worker["bootstrap_bundle_json"])
    forged_bundle["callbacks"]["origin_ref"] = "ghi_synthetic_foreign_origin"
    forged_worker["bootstrap_bundle_json"] = json.dumps(forged_bundle)
    assert (
        service._emit_callback(
            forged_worker,
            "run.failed",
            run=run,
            message="This callback is not bound to the durable delegation.",
            submit_delivery=False,
        )
        is None
    )


def test_delegation_rejects_conflicting_explicit_and_callback_origin_refs(account_client):
    payload = delegation_payload_with_origin(origin_ref="ghi_synthetic_origin_callback")
    payload["originRef"] = "ghi_synthetic_origin_explicit"
    response = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-origin-conflict"),
        json=payload,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "delegation_origin_ref_conflict"


def test_delegation_can_be_reconciled_by_origin_without_identity_oracle(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-origin-reconcile"),
        json=delegation_payload_with_origin(origin_ref="ghi_synthetic_reconcile_0001"),
    )
    assert accepted.status_code == 202

    found = account_client.get(
        "/v1/delegations/by-origin/ghi_synthetic_reconcile_0001",
        headers=account_headers(),
    )
    foreign = account_client.get(
        "/v1/delegations/by-origin/ghi_synthetic_reconcile_0001",
        headers=account_headers(owner_id="owner-b"),
    )
    absent = account_client.get(
        "/v1/delegations/by-origin/ghi_synthetic_reconcile_absent",
        headers=account_headers(),
    )

    assert found.status_code == 200
    assert found.json() == {
        "workRef": accepted.json()["workRef"],
        "state": "accepted",
    }
    for response in (foreign, absent):
        assert response.status_code == 404
        assert response.json() == {
            "detail": {
                "code": "delegation_not_found",
                "message": "The delegation was not found.",
            }
        }


def test_service_assertion_cross_language_canonical_vector():
    # Fixed vector shared with the Core signer: JSON keys are UTF-8 canonical
    # lexicographic order with no insignificant whitespace.
    secret = "synthetic-cross-language-secret"
    claims = {
        "aud": "glasshive-account-api",
        "exp": 1786543260,
        "iat": 1786543200,
        "nonce": "nonce_cross_language_0001",
        "owner_id": "owner-synthetic",
        "tenant_id": "tenant-synthetic",
        "v": 1,
    }
    canonical = json.dumps(
        claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    payload_segment = _b64url(canonical)
    signature = _b64url(
        hmac.new(secret.encode(), payload_segment.encode(), hashlib.sha256).digest()
    )
    vector = f"{payload_segment}.{signature}"

    assert vector == (
        "eyJhdWQiOiJnbGFzc2hpdmUtYWNjb3VudC1hcGkiLCJleHAiOjE3ODY1NDMyNjAsImlhdCI6"
        "MTc4NjU0MzIwMCwibm9uY2UiOiJub25jZV9jcm9zc19sYW5ndWFnZV8wMDAxIiwib3duZXJf"
        "aWQiOiJvd25lci1zeW50aGV0aWMiLCJ0ZW5hbnRfaWQiOiJ0ZW5hbnQtc3ludGhldGljIiwidiI6MX0."
        "sw7mRhWFP9xCoWtZpOzZKq65w3itqWE4-YsIpe92FNM"
    )
    assert verify_service_assertion(vector, secret=secret, now_epoch=1786543201) == claims


@pytest.mark.parametrize(
    ("method", "suffix", "json_body"),
    [
        ("post", "assign", {"instruction": "malicious assign"}),
        ("post", "message", {"message": "malicious message"}),
        ("post", "interrupt", None),
        ("post", "pause", None),
        ("post", "resume", None),
        ("post", "terminate", None),
        ("post", "desktop-action", {"action": "terminal"}),
    ],
)
def test_worker_view_token_cannot_call_legacy_mutations(
    account_client,
    method,
    suffix,
    json_body,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-view-abuse-{suffix}"),
        json=delegation_payload(title=f"View abuse {suffix}"),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    params = sign_link_params(
        kind="worker_view",
        worker_id=delegation["worker_id"],
        tenant_id="tenant-a",
        owner_id="owner-a",
    )

    response = getattr(account_client, method)(
        f"/v1/workers/{delegation['worker_id']}/{suffix}",
        params=params,
        json=json_body,
    )

    assert response.status_code in {401, 403}
    assert store.get_worker(delegation["worker_id"])["state"] == "created"
    assert len(store.list_runs_for_worker(delegation["worker_id"])) == 1


def test_public_view_ref_is_absolute_read_only_and_cannot_control_workspace(account_client, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.delenv("GLASSHIVE_RUNTIME_BASE_URL", raising=False)
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-read-only-view"),
        json=delegation_payload(title="Read-only view mission"),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    workspace = Path(store.db_path).parent / "read-only-view-workspace"
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET workspace_dir = ? WHERE worker_id = ?",
            (str(workspace), delegation["worker_id"]),
        )
    worker = store.get_worker(delegation["worker_id"])
    artifact = workspace / "artifacts" / "result.html"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        "<html><body>Synthetic mission result"
        "<script>window.parent.syntheticArtifactEscape=true</script>"
        "</body></html>",
        encoding="utf-8",
    )
    view_ref = accepted["viewRef"]
    parsed = urlsplit(view_ref)

    assert parsed.scheme == "https"
    assert parsed.netloc == "glasshive.example.test"
    assert parsed.path.startswith("/w/ghr_")

    view = account_client.get(parsed.path)
    ref_id = parsed.path.rsplit("/", 1)[-1]
    terminate = account_client.post(f"/w/{ref_id}/actions/terminate")
    desktop_action = account_client.post(
        f"/w/{ref_id}/desktop-action",
        json={"action": "terminal"},
    )

    assert view.status_code == 200
    assert '<meta http-equiv="refresh" content="5">' in view.text
    assert "Read-only mission view" in view.text
    assert "Mission state" in view.text
    assert "Worker state" in view.text
    assert "Mission state describes this result" in view.text
    assert "Worker state describes the reusable workspace" in view.text
    assert "artifacts/result.html" in view.text
    assert "Open" in view.text
    assert "Download" in view.text
    assert 'aria-label="Open artifacts/result.html"' in view.text
    assert 'aria-label="Download artifacts/result.html"' in view.text
    assert "Terminate" not in view.text
    assert "Pause" not in view.text
    assert str(worker["workspace_dir"]) not in view.text
    artifact_hrefs = re.findall(r'href="([^\"]*/v1/link-refs/[^\"]+)"', view.text)
    assert len(artifact_hrefs) == 2
    opened = account_client.get(urlsplit(artifact_hrefs[0]).path)
    downloaded = account_client.get(urlsplit(artifact_hrefs[1]).path)
    assert opened.status_code == 200
    assert "Synthetic mission result" in opened.text
    assert " sandbox" in opened.text
    assert "allow-scripts" not in opened.text
    assert "allow-same-origin" not in opened.text
    assert "default-src 'none'" in opened.headers["content-security-policy"]
    assert "&lt;script&gt;window.parent.syntheticArtifactEscape" in opened.text
    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]
    assert terminate.status_code == 403
    assert desktop_action.status_code == 403

    # A read-only view follows active work without another click, then stops reloading.
    run_id = store.list_runs_for_worker(worker["worker_id"])[0]["run_id"]
    for state in ("running", "completed", "failed", "cancelled"):
        # This fixture exercises rendering, not the separately tested provider admission lifecycle.
        with store._connect() as conn:
            conn.execute("UPDATE runs SET state = ? WHERE run_id = ?", (state, run_id))
        current = account_client.get(parsed.path)
        assert current.status_code == 200
        assert ('<meta http-equiv="refresh" content="5">' in current.text) == (state == "running")
        assert f"<dd>{state.title()}</dd>" in current.text
        assert "To change this work, return to your chat or open Active work." in current.text


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "running", "queued"])
def test_mission_view_shows_current_written_result_with_honest_lifecycle(account_client, state):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"written-result-{state}"),
        json=delegation_payload(title="Read-only recovery review"),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    result = (
        "The supported recovery is to run doctor, then start.\n"
        "Evidence: [startup.py:12](/Users/example/work/startup.py:12).\n"
        "<script>unsafeResult()</script> bearer synthetic-token-0123456789"
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE runs SET state = ?, output_text = ?, failure_user_message = ? WHERE run_id = ?",
            (state, result, "The connection was interrupted." if state == "failed" else "", delegation["current_run_id"]),
        )
        # View must follow the mission even if the reusable worker's pointer changes.
        conn.execute("UPDATE workers SET last_run_id = NULL WHERE worker_id = ?", (delegation["worker_id"],))
    view = account_client.get(urlsplit(accepted["viewRef"]).path)
    assert view.status_code == 200
    assert f"<dd>{state.title()}</dd>" in view.text
    terminal = state in {"completed", "failed", "cancelled"}
    assert ("supported recovery is to run doctor" in view.text) == terminal
    assert ("Progress before interruption" in view.text) == (state in {"failed", "cancelled"})
    assert ('<meta http-equiv="refresh" content="5">' in view.text) == (not terminal)
    assert "/Users/example" not in view.text
    assert "synthetic-token-0123456789" not in view.text
    assert "<script>unsafeResult()" not in view.text
    if terminal:
        assert "startup.py:12" in view.text
        assert "&lt;script&gt;unsafeResult()&lt;/script&gt;" in view.text
    if state == "failed":
        assert "The connection was interrupted." in view.text
    # Reads do not complete, rerun, or otherwise rewrite the mission.
    assert store.get_run(delegation["current_run_id"])["state"] == state
    assert len(store.list_runs_for_worker(delegation["worker_id"])) == 1


@pytest.mark.parametrize("other_owner", [False, True])
def test_mission_view_rejects_a_current_run_from_another_worker(account_client, other_owner):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="written-result-owner"),
        json=delegation_payload(),
    ).json()
    other = account_client.post(
        "/v1/delegations",
        headers=account_headers(owner_id="owner-b" if other_owner else "owner-a", idempotency_key="other-written-result"),
        json=delegation_payload(title="Other mission"),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    other_delegation = store.get_delegation(other["workRef"], tenant_id="tenant-a", owner_id="owner-b" if other_owner else "owner-a")
    with store._connect() as conn:
        conn.execute("UPDATE runs SET state = 'completed', output_text = ? WHERE run_id = ?", ("Unrelated result must stay private", other_delegation["current_run_id"]))
        conn.execute("UPDATE delegations SET current_run_id = ? WHERE work_ref = ?", (other_delegation["current_run_id"], delegation["work_ref"]))
    view = account_client.get(urlsplit(accepted["viewRef"]).path)
    assert view.status_code == 200
    assert "Unrelated result must stay private" not in view.text
    assert "No result is available yet." in view.text
    assert "<dd>Completed</dd>" not in view.text


def test_active_work_view_allows_an_explicit_client_reachable_origin_override(
    account_client,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_ACTIVE_WORK_VIEW_BASE_URL", "http://127.0.0.1:8766")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-local-view-origin"),
        json=delegation_payload(title="Reachable local view"),
    ).json()

    assert accepted["viewRef"].startswith("http://127.0.0.1:8766/w/ghr_")


def test_active_work_view_defaults_to_the_authenticated_request_origin(
    account_client,
    monkeypatch,
):
    for name in (
        "GLASSHIVE_ACTIVE_WORK_VIEW_BASE_URL",
        "GLASSHIVE_OPERATOR_BASE_URL",
        "WPR_OPERATOR_BASE_URL",
        "GLASSHIVE_RUNTIME_PUBLIC_BASE_URL",
        "WPR_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-request-view-origin"),
        json=delegation_payload(title="Authenticated local mission view"),
    ).json()
    active = account_client.get("/v1/active-work", headers=account_headers()).json()
    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}",
        headers=account_headers(),
    ).json()

    assert accepted["viewRef"].startswith("http://testserver/w/ghr_")
    assert active["work"][0]["viewRef"].startswith("http://testserver/w/ghr_")
    assert detail["viewRef"].startswith("http://testserver/w/ghr_")


@pytest.mark.parametrize(
    "untrusted_origin",
    [
        "https://synthetic-user@untrusted.example.test",
        "javascript:alert(1)",
        "https://untrusted.example.test/?traceparent=untrusted",
    ],
)
def test_active_work_view_skips_credentialed_or_untrusted_origin_values(
    account_client,
    monkeypatch,
    untrusted_origin,
):
    monkeypatch.setenv("GLASSHIVE_ACTIVE_WORK_VIEW_BASE_URL", untrusted_origin)
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-safe-view-origin"),
        json=delegation_payload(title="Safe mission view origin"),
    ).json()

    assert accepted["viewRef"].startswith("https://glasshive.example.test/w/ghr_")
    assert "synthetic-user" not in accepted["viewRef"]


def test_active_work_history_is_owner_scoped_terminal_and_read_only(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-history-owner-a"),
        json=delegation_payload(title="Historical mission"),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE runs SET state = 'completed', ended_at = ? WHERE run_id = ?",
            (now, delegation["current_run_id"]),
        )
        conn.execute(
            "UPDATE delegations SET dismissed_at = ?, updated_at = ? WHERE work_ref = ?",
            (now, now, accepted["workRef"]),
        )

    active = account_client.get("/v1/active-work", headers=account_headers()).json()
    history = account_client.get(
        "/v1/active-work/history",
        headers=account_headers(),
    )
    foreign = account_client.get(
        "/v1/active-work/history",
        headers=account_headers(owner_id="owner-b"),
    )

    assert active["work"] == []
    assert history.status_code == 200
    assert len(history.json()["work"]) == 1
    assert history.json()["work"][0]["workRef"] == accepted["workRef"]
    assert history.json()["work"][0]["state"] == "completed"
    assert history.json()["work"][0]["actions"] == []
    assert foreign.status_code == 200
    assert foreign.json()["work"] == []


@pytest.mark.parametrize("terminal_state", ["completed", "failed", "cancelled"])
def test_active_work_exposes_existing_signed_artifact_links_for_the_exact_owner(
    account_client, monkeypatch, terminal_state, tmp_path,
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-artifact-link-secret")
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="artifact-mission"),
        json=delegation_payload(title="Document delivery"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    worker = store.get_worker(record["worker_id"])
    workspace = tmp_path / "mission-workspace"
    store.update_worker(worker["worker_id"], workspace_dir=str(workspace))
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "result.pdf").write_bytes(b"%PDF-1.4\nSynthetic test document\n")
    (workspace / ".mcp.json").write_text("private configuration")
    outside = workspace.parent / "outside-private.txt"
    outside.write_text("must-not-leak")
    (workspace / "outside-link.txt").symlink_to(outside)
    store.finalize_run(record["current_run_id"], terminal_state, output_text="Document available")

    roster = account_client.get("/v1/active-work", headers=account_headers()).json()
    item = next(item for item in roster["work"] if item["workRef"] == accepted["workRef"])
    artifacts = item["artifactLinks"]
    assert artifacts["status"] == "ok"
    assert artifacts["scope"] == "workspace"
    assert artifacts["truncated"] is False
    assert artifacts["workspaceUrl"] == item["viewRef"]
    assert [item["path"] for item in artifacts["items"]] == ["result.pdf"]
    file = artifacts["items"][0]
    assert file["sizeBytes"] == len((workspace / "result.pdf").read_bytes())
    assert file["openUrl"].startswith("http://testserver/v1/link-refs/ghr_")
    assert file["downloadUrl"].startswith("http://testserver/v1/link-refs/ghr_")
    downloaded = account_client.get(file["downloadUrl"])
    assert downloaded.status_code == 200
    assert downloaded.content == (workspace / "result.pdf").read_bytes()
    assert account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers(owner_id="owner-b"),
    ).status_code == 404
    serialized = json.dumps(item)
    for private_value in (record["worker_id"], record["current_run_id"], str(workspace), "outside-link", ".mcp.json"):
        assert private_value not in serialized
    repeated = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers(),
    ).json()
    assert repeated["artifactLinks"]["items"] == artifacts["items"]


def test_active_work_artifact_inventory_is_bounded_and_does_not_claim_completeness(
    account_client, monkeypatch, tmp_path,
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-artifact-link-secret")
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="artifact-overflow"),
        json=delegation_payload(),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    workspace = tmp_path / "mission-workspace"
    store.update_worker(record["worker_id"], workspace_dir=str(workspace))
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(30):
        (workspace / f"file-{index:02}.txt").write_text(f"File {index}")
    store.finalize_run(record["current_run_id"], "completed", output_text="Files available")
    item = account_client.get(f"/v1/work/{accepted['workRef']}", headers=account_headers()).json()
    assert len(item["artifactLinks"]["items"]) == 5
    assert item["artifactLinks"]["truncated"] is True
    assert item["artifactLinks"]["workspaceUrl"] == item["viewRef"]


def test_active_work_artifact_links_fail_closed_if_worker_binding_changes(account_client, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-artifact-link-secret")
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="artifact-binding"),
        json=delegation_payload(),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    store.finalize_run(record["current_run_id"], "completed", output_text="Done")
    get_worker = store.get_worker
    monkeypatch.setattr(store, "get_worker", lambda worker_id: {
        **get_worker(worker_id), "owner_id": "owner-b",
    })
    item = account_client.get(f"/v1/work/{accepted['workRef']}", headers=account_headers()).json()
    assert item["artifactLinks"] == {"status": "unavailable", "items": []}


def test_active_work_never_exposes_unsigned_internal_artifact_routes(account_client, monkeypatch, tmp_path):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-artifact-link-secret")
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="artifact-signer"),
        json=delegation_payload(),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    workspace = tmp_path / "mission-workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("File content")
    store.update_worker(record["worker_id"], workspace_dir=str(workspace))
    store.finalize_run(record["current_run_id"], "completed", output_text="Done")
    sign_link = api_module.sign_link_token
    monkeypatch.setattr(api_module, "sign_link_token", lambda **values: (
        sign_link(**values) if values["kind"] == "worker_view" else ""
    ))
    response = account_client.get(f"/v1/work/{accepted['workRef']}", headers=account_headers())
    assert response.status_code == 200
    assert response.json()["artifactLinks"] == {"status": "unavailable", "items": []}
    assert "/v1/workers/" not in response.text


@pytest.mark.parametrize("action", ["steer", "pause", "resume"])
def test_active_work_finish_replays_concurrent_same_executor_receipt(
    tmp_path, monkeypatch, action,
):
    """Recovery finishing after reservation must not turn success into a 409."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    runtime = StubRuntime()
    app = create_app(
        db_path=str(tmp_path / "same-owner-finish.sqlite3"),
        runtime_backend="stub", runtime=runtime, reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key="finish-race-delegation"),
            json=delegation_payload(title="Concurrent action completion"),
        ).json()
        store = app.state.store
        record = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a",
        )
        if action == "resume":
            _truthfully_pause_run(store, record["current_run_id"], suffix="finish-race")
        else:
            _truthfully_invoke_run(store, record["current_run_id"], suffix="finish-race")
        original_finish = store.finish_active_work_action
        first_receipts = []

        def concurrent_finish(action_use_id, **kwargs):
            if not first_receipts:
                # The recovery owner commits between the API's reservation and
                # finalization. Its canonical receipt wins; the loser cannot
                # rewrite it or advance the current run a second time.
                durable = original_finish(action_use_id, **kwargs)
                assert durable is not None
                first_receipts.append(durable)
                kwargs = {**kwargs, "response": {**kwargs["response"], "state": "stale"}}
            return original_finish(action_use_id, **kwargs)

        monkeypatch.setattr(store, "finish_active_work_action", concurrent_finish)
        body = {"action": action, "idempotencyKey": "finish-race-action"}
        if action == "steer":
            body["instruction"] = "Use the corrected objective."
        response = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(), json=body,
        )
        assert response.status_code == 202, response.text
        first = first_receipts[0]
        canonical = json.loads(first["response_json"])
        assert response.json() == canonical
        assert store.get_active_work_action(first["action_use_id"]) == first
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(), json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json() == {**canonical, "idempotentReplay": True}
        assert store.get_active_work_action(first["action_use_id"]) == first
        assert original_finish(
            first["action_use_id"], response={"state": "forged"},
            executor_id="different-executor",
        ) is None
        assert original_finish(
            first["action_use_id"], response={"state": "unbound"},
        ) is None
        assert store.get_active_work_action(first["action_use_id"]) == first


@pytest.mark.parametrize("action", ["queue", "message"])
@pytest.mark.parametrize("closed_state", ["terminating", "terminated", "termination_failed"])
def test_terminal_follow_up_does_not_reopen_closed_workspace(account_client, action, closed_state):
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="closed-follow-up"),
        json=delegation_payload(title="Closed review"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    store.finalize_run(record["current_run_id"], "failed", failure_class="unknown", failure_retryable=0)
    store.update_worker_state(record["worker_id"], closed_state)
    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions", headers=account_headers(),
        json={"action": action, "instruction": "Review revised source.", "idempotencyKey": "closed-correction"},
    )
    assert response.status_code == 409, response.text
    assert len(store.list_runs_for_worker(record["worker_id"])) == 1
    assert store.get_worker(record["worker_id"])["state"] == closed_state


@pytest.mark.parametrize("output_mode", ["inherit", "replace"])
def test_queue_projects_trusted_prior_output_source_without_rewriting_new_goal(account_client, output_mode):
    original = "Create the requested HTML report. Preserve the original data."
    correction = "Shorten the explanation."
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="queue-output-source"),
        json=delegation_payload(title="Existing report", instruction=original),
    ).json()
    service = account_client.app.state.service
    store = service.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    store.finalize_run(record["current_run_id"], "completed", output_text="Prior report")
    store.update_worker_state(record["worker_id"], "ready")
    output = {"mode": output_mode, "required": [], "forbidden": [], "formats": [], "forbiddenFormats": []}
    if output_mode == "replace":
        output.update(required=["Create the revised PDF report."], formats=["pdf"])
    result = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions", headers=account_headers(),
        json={"action": "queue", "instruction": correction, "idempotencyKey": "queue-correction",
              "sourceContext": {"version": 1, "originRef": "origin-corrected-report",
                                "sourceEventId": "event-corrected-report", "sourceRevision": 1,
                                "surface": "web", "outputContract": output}},
    )
    assert result.status_code == 202, result.text
    current = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    run = store.get_run(current["current_run_id"])
    runtime_worker = service._run_local_worker(store.get_worker(current["worker_id"]), run)
    ledger = build_constraint_ledger(instruction=run["instruction"], worker=runtime_worker, run_id=run["run_id"])
    assert run["instruction"] == correction
    assert ledger["outputs"]["format_expectations"] == ([] if output_mode == "inherit" else ["pdf"])
    assert ledger["original_output_source"] == original
    assert json.loads(run["continuation_context_json"]) == {
        "version": 1, "base_instruction": original, "guidance": [correction],
    }


def test_native_input_requires_exact_signed_web_body_and_preserves_run(account_client, monkeypatch):
    from workers_projects_runtime.service_assertions import mint_service_assertion
    accepted=account_client.post('/v1/delegations',headers=account_headers(idempotency_key='native-input-delegation'),json=delegation_payload()).json()
    store=account_client.app.state.store;service=account_client.app.state.service
    record=store.get_delegation(accepted['workRef'],tenant_id='tenant-a',owner_id='owner-a')
    _truthfully_invoke_run(store,record['current_run_id'],suffix='native-input')
    calls=[]
    def respond(worker,**values):
        calls.append((worker['worker_id'],values));return {'status':'accepted'}
    monkeypatch.setattr(service.runtime,'respond_native_input',respond,raising=False)
    monkeypatch.setattr(service.runtime,'pending_native_input',lambda worker,**kw:{'version':1,'requestId':'native-request','requestFingerprint':'a'*64,'kind':'elicitation','mode':'form','state':'pending','message':'Synthetic input','mcpServerName':'fixture','requestedSchema':{'type':'object','properties':{}}},raising=False)
    body={'action':'resume','idempotencyKey':'native-input-response','nativeInput':{'version':1,'requestId':'native-request','requestFingerprint':'a'*64,'action':'decline'}}
    raw=json.dumps(body,separators=(',',':')).encode()
    path=f"/v1/work/{accepted['workRef']}/actions"
    untrusted=account_client.post(path,headers={**account_headers(),'Content-Type':'application/json'},content=raw)
    assert untrusted.status_code==403,untrusted.text
    def headers(data=raw,owner='owner-a'):
        token=mint_service_assertion(ASSERTION_SECRET,tenant_id='tenant-a',owner_id=owner,native_input_digest=hashlib.sha256(data).hexdigest())
        return {**account_headers(assertion=token),'Content-Type':'application/json'}
    tampered=account_client.post(path,headers=headers(b'wrong'),content=raw)
    assert tampered.status_code==403,tampered.text
    foreign=account_client.post(path,headers=headers(owner='owner-b'),content=raw)
    assert foreign.status_code==404,foreign.text
    detail=account_client.get(f"/v1/work/{accepted['workRef']}",headers=account_headers())
    assert detail.json()['pendingNativeInput']['requestId']=='native-request'
    assert detail.json()['state']=='needs_input'
    result=account_client.post(path,headers=headers(),content=raw)
    assert result.status_code==202,result.text
    repeated=account_client.post(path,headers=headers(),content=raw)
    assert repeated.status_code==202,repeated.text
    assert repeated.json()['idempotentReplay'] is True
    changed=json.dumps({**body,'nativeInput':{**body['nativeInput'],'action':'accept'}},separators=(',',':')).encode()
    conflict=account_client.post(path,headers=headers(changed),content=changed)
    assert conflict.status_code==409,conflict.text
    assert len(calls)==1
    assert calls[0][1]['action']=='decline'
    assert store.get_run(record['current_run_id'])['state']=='running'
    assert len(store.list_runs_for_worker(record['worker_id']))==1



def test_native_input_response_receipt_recovers_after_run_finishes(account_client,monkeypatch,tmp_path):
    from workers_projects_runtime import native_input
    from workers_projects_runtime.models import NativeInputResponseRequest
    from workers_projects_runtime.service_assertions import mint_service_assertion
    accepted=account_client.post('/v1/delegations',headers=account_headers(idempotency_key='native-input-recovery'),json=delegation_payload()).json()
    store=account_client.app.state.store;service=account_client.app.state.service
    record=store.get_delegation(accepted['workRef'],tenant_id='tenant-a',owner_id='owner-a')
    run_id=record['current_run_id'];_truthfully_invoke_run(store,run_id,suffix='native-input-recovery')
    record=store.get_delegation(accepted['workRef'],tenant_id='tenant-a',owner_id='owner-a')
    root=tmp_path/'input';root.mkdir()
    native_input.publish(root/'native-input-context.json',{'version':1,'workerId':record['worker_id'],'runId':run_id,'contextToken':'context-a'})
    native_input.publish(native_input.request_path(root,'request-a'),{'requestId':'request-a','requestFingerprint':'a'*64,'state':'pending','contextToken':'context-a','mode':'form','requestedSchema':{'type':'object','properties':{}}})
    def respond(worker,**values):
        return native_input.respond(root,worker_id=worker['worker_id'],**values)
    monkeypatch.setattr(service.runtime,'respond_native_input',respond,raising=False)
    answer={'version':1,'requestId':'request-a','requestFingerprint':'a'*64,'action':'decline'}
    parsed=NativeInputResponseRequest(**answer).model_dump()
    action_request={'action':'resume','instruction':'','capabilityReauthorization':None,'nativeInput':parsed}
    reservation=store.reserve_active_work_action(tenant_id='tenant-a',owner_id='owner-a',work_ref=accepted['workRef'],idempotency_key='native-response-recovery',action='resume',payload_digest=native_input.digest(action_request),expected_current_run_id=run_id,expected_source_run_id=run_id,expected_source_state=record['run_state'],expected_source_started_at=record.get('run_started_at') or '',executor_id=service.executor_id)
    initial=service.execute_active_work_action(record,action='resume',instruction='',idempotency_key='native-response-recovery',action_use_id=reservation['action_use_id'],native_input=parsed)
    assert initial['status']=='accepted'
    assert store.finalize_run_if_state(run_id,'running','completed',output_text='Useful result after owner response.',**_exact_terminal_generation(store,run_id))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?',('2000-01-01T00:00:00+00:00',reservation['action_use_id']))
    # A recovered API executor takes over the expired action, with the native reply already durable.
    monkeypatch.setattr(service,'_executor_id','recovered-native-input-executor')
    body={'action':'resume','idempotencyKey':'native-response-recovery','nativeInput':answer};raw=json.dumps(body,separators=(',',':')).encode()
    token=mint_service_assertion(ASSERTION_SECRET,tenant_id='tenant-a',owner_id='owner-a',native_input_digest=hashlib.sha256(raw).hexdigest())
    result=account_client.post(f"/v1/work/{accepted['workRef']}/actions",headers={**account_headers(assertion=token),'Content-Type':'application/json'},content=raw)
    assert result.status_code==202,result.text
    assert result.json()['status']=='already_accepted'
    assert result.json()['state']=='completed'
    assert result.json()['idempotentReplay'] is True
    assert store.get_run(run_id)['output_text']=='Useful result after owner response.'
    assert len(store.list_runs_for_worker(record['worker_id']))==1


def test_invalid_native_form_returns_editable_client_error(account_client,monkeypatch):
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    from workers_projects_runtime.service_assertions import mint_service_assertion
    accepted=account_client.post('/v1/delegations',headers=account_headers(idempotency_key='native-input-invalid'),json=delegation_payload()).json()
    store=account_client.app.state.store;service=account_client.app.state.service
    record=store.get_delegation(accepted['workRef'],tenant_id='tenant-a',owner_id='owner-a')
    _truthfully_invoke_run(store,record['current_run_id'],suffix='native-invalid')
    def invalid(*args,**kwargs):raise RuntimeErrorBase('native_input_invalid')
    monkeypatch.setattr(service.runtime,'respond_native_input',invalid,raising=False)
    body={'action':'resume','idempotencyKey':'native-input-invalid-response','nativeInput':{'version':1,'requestId':'request-a','requestFingerprint':'a'*64,'action':'accept','content':{'value':'invalid'}}}
    raw=json.dumps(body,separators=(',',':')).encode()
    token=mint_service_assertion(ASSERTION_SECRET,tenant_id='tenant-a',owner_id='owner-a',native_input_digest=hashlib.sha256(raw).hexdigest())
    result=account_client.post(f"/v1/work/{accepted['workRef']}/actions",headers={**account_headers(assertion=token),'Content-Type':'application/json'},content=raw)
    assert result.status_code==400,result.text
    assert store.get_run(record['current_run_id'])['state']=='running'


@pytest.mark.parametrize("action", ["queue", "message"])
@pytest.mark.parametrize("source_state", ["queued", "completed"])
def test_follow_up_is_durable_under_capacity_pressure(account_client, monkeypatch, action, source_state):
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="capacity-follow-up"),
        json=delegation_payload(title="Existing objective"),
    ).json()
    service = account_client.app.state.service
    store = service.store
    record = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    if source_state == "completed":
        store.finalize_run(record["current_run_id"], "completed", output_text="Prior result")
        store.update_worker_state(record["worker_id"], "ready")
    def no_capacity(*_args, **_kwargs):
        raise HostCapacityError("The mission lane is full.", capacity_class="family_lane")
    monkeypatch.setattr(service, "_reserved_runtime_preflight", no_capacity)
    request = {"action": action, "instruction": "Preserve the original goal and add source dates.", "idempotencyKey": "capacity-guidance"}
    response = account_client.post(f"/v1/work/{accepted['workRef']}/actions", headers=account_headers(), json=request)
    assert response.status_code == 202, response.text
    replay = account_client.post(f"/v1/work/{accepted['workRef']}/actions", headers=account_headers(), json=request)
    assert replay.status_code == 202, replay.text
    runs = store.list_runs_for_worker(record["worker_id"])
    assert len(runs) == 2
    queued = [run for run in runs if run["state"] == "queued"]
    assert len(queued) == (2 if source_state == "queued" and action == "queue" else 1)
    assert any(request["instruction"] in run["instruction"] for run in queued)


def test_work_action_capacity_failure_keeps_typed_cause(account_client, monkeypatch):
    accepted = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="typed-capacity"),
        json=delegation_payload(title="Capacity cause"),
    ).json()
    def capacity(*args, **kwargs):
        raise HostCapacityError("The configured lane is full.", capacity_class="host")
    monkeypatch.setattr(account_client.app.state.service, "execute_active_work_action", capacity)
    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions", headers=account_headers(),
        json={"action": "message", "instruction": "Keep the original scope.", "idempotencyKey": "capacity-action"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "host_capacity"
    assert int(response.headers["Retry-After"]) >= 1
    replay = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions", headers=account_headers(),
        json={"action": "message", "instruction": "Keep the original scope.", "idempotencyKey": "capacity-action"},
    )
    assert replay.status_code == 503
    assert replay.json() == response.json()
    assert replay.headers["Retry-After"] == response.headers["Retry-After"]


@pytest.mark.parametrize("execution_mode", ["host", "docker"])
def test_background_preferences_persist_exact_model_effort_and_fallback(account_client, monkeypatch, execution_mode):
    if execution_mode == "host":
        enable_native_orchestration(monkeypatch)
        payload = native_orchestrator_payload("Exact background route")
    else:
        monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
        payload = conversation_orchestrator_payload(title="Exact background route")
    authority = payload["bootstrapBundle"]["viventium_launch_authority"]
    authority.update({
        "worker_model": "codex-cli:gpt-6-astra",
        "worker_reasoning_effort": "medium",
        "fallback_worker_model": "claude-code:claude-opus-5",
        "fallback_worker_reasoning_effort": "medium",
    })
    response = account_client.post("/v1/delegations", headers=account_headers(idempotency_key="exact-background"), json=payload)
    assert response.status_code == 202, response.text
    store = account_client.app.state.store
    service = account_client.app.state.service
    record = store.get_delegation(response.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    worker = store.get_worker(record["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert worker["model"] == bundle["provider_model"] == "gpt-6-astra"
    assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "medium"
    assert bundle["viventium_launch_authority"] == authority
    # Exercise the same resolved fallback and atomic store transition used by both recovery paths.
    model, fallback_bundle = service._configured_parallel_worker_route("claude-code", execution_mode, bundle, fallback=True)
    run = _truthfully_invoke_run(store, record["current_run_id"], suffix="exact-fallback")
    # A capability refresh between route selection and switch must survive the transaction.
    current_bundle = {**bundle, "project_definition": "Updated accepted context"}
    store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps(current_bundle))
    switched = store.switch_worker_profile_and_requeue_run(
        worker_id=worker["worker_id"], run_id=run["run_id"], expected_profile="codex-cli",
        fallback_profile="claude-code", fallback_backend="claude-code", fallback_runtime="claude-code",
        fallback_model=model, fallback_bootstrap_bundle=fallback_bundle,
        retry_after=datetime.now(timezone.utc).isoformat(), error_text="Synthetic quota failure",
    )
    assert switched is not None
    refreshed = store.get_worker(worker["worker_id"])
    effective = service._refresh_worker_model_for_profile(refreshed)
    assert effective["model"] == "claude-opus-5"
    persisted = json.loads(effective["bootstrap_bundle_json"])
    assert persisted["provider_model"] == "claude-opus-5"
    assert persisted["project_definition"] == "Updated accepted context"
    assert persisted["env"]["WPR_CLAUDE_CODE_EFFORT"] == "medium"
    assert store.get_run(run["run_id"])["state"] == "queued"


@pytest.mark.parametrize("preferences", [
    {"worker_model": "codex-cli:unknown"},
    {"worker_model": "claude-code:claude-opus-5"},
    {"worker_model": "codex-cli:gpt-6-astra", "worker_reasoning_effort": "none"},
    {"fallback_worker_model": "claude-code:claude-opus-5", "fallback_worker_reasoning_effort": "ultra"},
])
def test_background_preferences_reject_unknown_or_incompatible_before_admission(account_client, monkeypatch, preferences):
    enable_native_orchestration(monkeypatch)
    payload = native_orchestrator_payload("Invalid background route")
    payload["bootstrapBundle"]["viventium_launch_authority"].update(preferences)
    response = account_client.post("/v1/delegations", headers=account_headers(idempotency_key="invalid-background"), json=payload)
    assert response.status_code == 400, response.text
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 0
