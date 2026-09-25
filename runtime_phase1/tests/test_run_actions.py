from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event

import pytest
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.run_actions import (
    mint_run_action_capability,
    unverified_run_action_claims,
)
from workers_projects_runtime.store import RunRestorationState


class CountingInterruptRuntime(StubRuntime):
    def __init__(self) -> None:
        self.interrupts: list[tuple[str, str | None]] = []

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupts.append((str(worker["worker_id"]), run_id))
        return super().interrupt_worker(worker, run_id=run_id)


class FlakyInterruptRuntime(CountingInterruptRuntime):
    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupts.append((str(worker["worker_id"]), run_id))
        if len(self.interrupts) == 1:
            raise RuntimeError("synthetic owner interrupt failure")
        return StubRuntime.interrupt_worker(self, worker, run_id=run_id)


class BlockingInterruptRuntime(CountingInterruptRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupts.append((str(worker["worker_id"]), run_id))
        self.started.set()
        assert self.release.wait(2), "test did not release the blocking interrupt"
        return StubRuntime.interrupt_worker(self, worker, run_id=run_id)


class CompletingDuringInterruptRuntime(CountingInterruptRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.complete_run = lambda: None

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupts.append((str(worker["worker_id"]), run_id))
        self.complete_run()
        return StubRuntime.interrupt_worker(self, worker, run_id=run_id)


def _create_scoped_worker(app, *, tenant_id: str = "local", owner_id: str = "owner-a") -> tuple[dict, dict]:
    store = app.state.store
    project = store.create_project(
        owner_id,
        "Action project",
        "Prove scoped GlassHive actions.",
        "codex-cli",
        tenant_id=tenant_id,
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        tenant_id=tenant_id,
        owner_id=owner_id,
        name="Action worker",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.local/glasshive",
                "hmac_secret": "synthetic-callback-secret",
                "user_id": owner_id,
                "conversation_id": "conversation-synthetic",
                "parent_message_id": "message-parent",
                "message_id": "message-current",
                "voice_call_session_id": "call-synthetic",
            }
        },
    )
    store.update_worker_state(worker["worker_id"], "ready", last_error="")
    return project, store.get_worker(worker["worker_id"])


def _truthfully_invoke_run(app, worker: dict, run: dict) -> dict:
    store = app.state.store
    executor_id = app.state.service._executor_id
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=executor_id
    )
    assert claimed is not None and claimed["run_id"] == run["run_id"]
    lease = store.acquire_host_run_lease(
        runtime_family="openclaw",
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    assert store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=executor_id,
    )
    invoked = store.mark_run_runtime_invoked(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=executor_id,
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
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
    store.update_worker_state(worker["worker_id"], "running")
    return confirmed["run"]


def _exact_terminal_generation(app, run_id: str) -> dict[str, str]:
    store = app.state.store
    run = store.get_run(run_id)
    assert run is not None
    return {
        **(store.get_run_retry_generation(run_id) or {}),
        "expected_runtime_invoked_at": str(run.get("runtime_invoked_at") or ""),
    }


def _latest_callback_payload(app, *, event_type: str, run_id: str) -> dict:
    with app.state.store._connect() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM callback_outbox
            WHERE event_type = ? AND run_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (event_type, run_id),
        ).fetchone()
    assert row is not None
    return json.loads(row["payload_json"])


def _capture_callbacks(monkeypatch) -> list[dict]:
    payloads: list[dict] = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_post(_url, *, content, headers, timeout):
        _ = headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    return payloads


def _outbound_callback(payloads: list[dict], *, event_type: str, run_id: str) -> dict:
    deadline = time.time() + 2
    while time.time() < deadline:
        for payload in payloads:
            if payload.get("event") == event_type and payload.get("run_id") == run_id:
                return payload
        time.sleep(0.01)
    raise AssertionError(f"Missing outbound callback {event_type} for {run_id}")


def _action_request(capability: dict, *, idempotency_key: str, **overrides) -> dict:
    body = {
        "version": 1,
        "capabilityId": capability["capabilityId"],
        "action": capability["action"],
        "projectId": capability["projectId"],
        "workerId": capability["workerId"],
        "runId": capability["runId"],
        "idempotencyKey": idempotency_key,
    }
    body.update(overrides)
    return body


def _capability_headers(capability: dict, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "X-Viventium-Action-Capability": capability["capability"],
        **(extra or {}),
    }


def test_retryable_failed_callback_carries_signed_short_lived_retry_capability(
    tmp_path, monkeypatch, background_consumers_disabled
):
    _ = background_consumers_disabled
    outbound = _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app):
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        run = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Create the requested artifact.",
            state=RunRestorationState.RUNNING,
        )
        failed = app.state.store.finalize_run(
            run["run_id"],
            state="failed",
            error_text="Synthetic provider outage",
            failure_class="provider_temporarily_unavailable",
            failure_retryable=1,
            failure_structured=1,
        )

        app.state.service._emit_callback(worker, "run.failed", run=failed, message="Retry is available")
        payload = _outbound_callback(outbound, event_type="run.failed", run_id=run["run_id"])

        capabilities = payload["actionCapabilities"]
        assert len(capabilities) == 1
        capability = capabilities[0]
        assert capability == {
            **capability,
            "version": 1,
            "action": "retry",
            "operation": "workspace_continue",
            "endpoint": "/v1/run-actions",
            "projectId": project["project_id"],
            "workerId": worker["worker_id"],
            "runId": run["run_id"],
        }
        assert capability["capabilityId"].startswith("gac_")
        assert capability["capability"]
        expires_at = int(datetime.fromisoformat(capability["expiresAt"]).timestamp())
        assert 0 < expires_at - payload["callback_ts"] <= 900
        assert capability["capability"] not in payload["message"]
        assert capability["capability"] not in payload["full_message"]

        encoded = app.state.service._encode_callback_payload(payload)
        callbacks = app.state.service._callback_config_for(worker)
        headers = app.state.service._callback_headers(callbacks, payload, encoded)
        binding = f"{worker['worker_id']}:{run['run_id']}".encode()
        derived = hmac.new(b"synthetic-callback-secret", binding, hashlib.sha256).hexdigest().encode()
        expected = "sha256=" + hmac.new(derived, encoded, hashlib.sha256).hexdigest()
        assert headers["X-GlassHive-Signature"] == expected
        assert headers["X-GlassHive-Callback-Id"] == payload["callback_id"]
        assert headers["X-GlassHive-Result-Revision"] == str(payload["result_revision"])
        assert headers["X-GlassHive-Result-Digest"] == payload["result_digest"]
        stored_payload = _latest_callback_payload(app, event_type="run.failed", run_id=run["run_id"])
        assert "actionCapabilities" not in stored_payload
        assert capability["capability"] not in json.dumps(stored_payload)


def test_nonretryable_failure_and_unproven_checkpoint_never_mint_actions(tmp_path, monkeypatch):
    outbound = _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app):
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        failed_run = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Nonretryable task",
            state=RunRestorationState.RUNNING,
        )
        failed_run = app.state.store.finalize_run(
            failed_run["run_id"],
            state="failed",
            failure_class="invalid_configuration",
            failure_retryable=0,
        )
        app.state.service._emit_callback(worker, "run.failed", run=failed_run, message="Cannot retry")
        app.state.service._emit_callback(worker, "checkpoint.ready", run=failed_run, message="Unproven")

        failure_payload = _outbound_callback(outbound, event_type="run.failed", run_id=failed_run["run_id"])
        checkpoint_payload = _outbound_callback(
            outbound, event_type="checkpoint.ready", run_id=failed_run["run_id"]
        )
        assert "actionCapabilities" not in failure_payload
        assert "actionCapabilities" not in checkpoint_payload


def test_retry_action_is_atomic_idempotent_and_preserves_workspace(tmp_path, monkeypatch):
    outbound = _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Produce final-report.html",
            state=RunRestorationState.RUNNING,
        )
        source = app.state.store.finalize_run(
            source["run_id"],
            state="failed",
            error_text="Provider unavailable",
            failure_class="provider_temporarily_unavailable",
            failure_retryable=1,
            failure_structured=1,
            failure_recommended_recovery="Retry in the same workspace.",
        )
        app.state.service._emit_callback(worker, "run.failed", run=source, message="Retry available")
        capability = _outbound_callback(
            outbound, event_type="run.failed", run_id=source["run_id"]
        )["actionCapabilities"][0]
        request = _action_request(capability, idempotency_key="idem-retry-0001")

        first = client.post("/v1/run-actions", json=request, headers=_capability_headers(capability))
        replay = client.post("/v1/run-actions", json=request, headers=_capability_headers(capability))
        conflicting_replay = client.post(
            "/v1/run-actions",
            json={**request, "idempotencyKey": "idem-retry-0002"},
            headers=_capability_headers(capability),
        )

        assert first.status_code == 202
        assert first.json()["status"] == "queued"
        assert first.json()["idempotentReplay"] is False
        assert replay.status_code == 202
        assert replay.json()["idempotentReplay"] is True
        assert replay.json()["newRun"] == first.json()["newRun"]
        assert conflicting_replay.status_code == 409
        assert conflicting_replay.json()["detail"]["code"] == "capability_replayed"

        new_run = app.state.store.get_run(first.json()["newRun"]["runId"])
        assert new_run is not None
        assert new_run["worker_id"] == worker["worker_id"]
        assert new_run["project_id"] == project["project_id"]
        assert "Prior run task context:\nProduce final-report.html" in new_run["instruction"]
        assert new_run["instruction"].count("Prior run task context:") == 1
        assert app.state.store.get_worker(worker["worker_id"])["workspace_dir"] == worker["workspace_dir"]
        runs = app.state.store.list_runs_for_worker(worker["worker_id"])
        assert len([item for item in runs if item["run_id"] != source["run_id"]]) == 1


def test_retry_rejects_scope_mismatch_expiry_nonretryable_active_and_ended(tmp_path, monkeypatch):
    _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Retry guard task",
            state=RunRestorationState.RUNNING,
        )
        source = app.state.store.finalize_run(
            source["run_id"], state="failed", failure_retryable=1, failure_class="provider_temporarily_unavailable"
        )
        callbacks = app.state.service._callback_config_for(worker)
        valid = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="retry", now_epoch=int(time.time())
        )
        expired = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="retry", now_epoch=int(time.time()) - 1200
        )

        mismatched = client.post(
            "/v1/run-actions",
            json=_action_request(valid, idempotency_key="idem-mismatch", workerId="wrk_forged"),
            headers=_capability_headers(valid),
        )
        expired_response = client.post(
            "/v1/run-actions",
            json=_action_request(expired, idempotency_key="idem-expired"),
            headers=_capability_headers(expired),
        )
        app.state.store.update_run(source["run_id"], failure_retryable=0)
        nonretryable = client.post(
            "/v1/run-actions",
            json=_action_request(valid, idempotency_key="idem-nonretryable"),
            headers=_capability_headers(valid),
        )
        app.state.store.update_run(source["run_id"], failure_retryable=1)
        active = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Already active",
            state=RunRestorationState.RUNNING,
        )
        active_response = client.post(
            "/v1/run-actions",
            json=_action_request(valid, idempotency_key="idem-active"),
            headers=_capability_headers(valid),
        )
        app.state.store.finalize_run(active["run_id"], state="completed")
        app.state.store.update_worker_state(worker["worker_id"], "terminated")
        ended_response = client.post(
            "/v1/run-actions",
            json=_action_request(valid, idempotency_key="idem-ended"),
            headers=_capability_headers(valid),
        )

        assert mismatched.status_code == 403
        assert mismatched.json()["detail"]["code"] == "capability_scope_mismatch"
        assert expired_response.status_code == 401
        assert expired_response.json()["detail"]["code"] == "capability_expired"
        assert nonretryable.status_code == 409
        assert nonretryable.json()["detail"]["code"] == "run_not_retryable"
        assert active_response.status_code == 409
        assert active_response.json()["detail"]["code"] == "worker_has_active_run"
        assert ended_response.status_code == 409
        assert ended_response.json()["detail"]["code"] == "worker_ended"


def test_action_capability_rejects_signature_tampering_and_changed_db_owner(tmp_path, monkeypatch):
    outbound = _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Authenticated retry",
            state=RunRestorationState.RUNNING,
        )
        source = app.state.store.finalize_run(
            source["run_id"], state="failed", failure_retryable=1, failure_class="provider_temporarily_unavailable"
        )
        app.state.service._emit_callback(worker, "run.failed", run=source, message="Retry available")
        capability = _outbound_callback(
            outbound, event_type="run.failed", run_id=source["run_id"]
        )["actionCapabilities"][0]
        body = _action_request(capability, idempotency_key="idem-auth-0001")
        token = capability["capability"]
        payload_segment, signature_segment = token.split(".", 1)
        replacement = "A" if signature_segment[0] != "A" else "B"
        tampered_token = f"{payload_segment}.{replacement}{signature_segment[1:]}"

        tampered = client.post(
            "/v1/run-actions",
            json=body,
            headers={"X-Viventium-Action-Capability": tampered_token},
        )
        app.state.store.update_worker(worker["worker_id"], owner_id="owner-b")
        changed_owner = client.post(
            "/v1/run-actions", json=body, headers=_capability_headers(capability)
        )

        assert tampered.status_code == 401
        assert tampered.json()["detail"]["code"] == "capability_invalid"
        assert changed_owner.status_code == 403
        assert changed_owner.json()["detail"]["code"] == "capability_scope_mismatch"


def test_action_capability_rejects_noncanonical_base64url_signature_alias(tmp_path, monkeypatch):
    outbound = _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Canonical signature",
            state=RunRestorationState.RUNNING,
        )
        source = app.state.store.finalize_run(
            source["run_id"], state="failed", failure_retryable=1, failure_class="temporary"
        )
        app.state.service._emit_callback(worker, "run.failed", run=source, message="Retry available")
        capability = _outbound_callback(
            outbound, event_type="run.failed", run_id=source["run_id"]
        )["actionCapabilities"][0]
        token = capability["capability"]
        payload_segment, signature_segment = token.split(".", 1)
        canonical_bytes = base64.urlsafe_b64decode(signature_segment + "=")
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        alias = next(
            candidate
            for candidate in alphabet
            if candidate != signature_segment[-1]
            and base64.urlsafe_b64decode(signature_segment[:-1] + candidate + "=") == canonical_bytes
        )

        response = client.post(
            "/v1/run-actions",
            json=_action_request(capability, idempotency_key="idem-noncanonical-0001"),
            headers={
                "X-Viventium-Action-Capability": f"{payload_segment}.{signature_segment[:-1]}{alias}"
            },
        )

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "capability_invalid"


def test_unverified_unknown_scope_is_uniform_invalid_capability(tmp_path, monkeypatch):
    _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Unknown scope",
            state=RunRestorationState.RUNNING,
        )
        callbacks = app.state.service._callback_config_for(worker)
        unknown_scope = mint_run_action_capability(
            callbacks["hmac_secret"],
            worker={**worker, "worker_id": "wrk_unknown"},
            run={**source, "worker_id": "wrk_unknown"},
            action="cancel",
        )

        response = client.post(
            "/v1/run-actions",
            json=_action_request(unknown_scope, idempotency_key="idem-unknown-scope"),
            headers=_capability_headers(unknown_scope),
        )

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "capability_invalid"


def test_cancel_is_exact_run_idempotent_and_only_confirmed_by_terminal_callback(tmp_path, monkeypatch):
    outbound = _capture_callbacks(monkeypatch)
    runtime = CountingInterruptRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        # The test claims its queued run itself; no background processor may take it first.
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Long active task",
            state="queued",
        )
        source = _truthfully_invoke_run(app, worker, source)
        app.state.store.update_worker_state(worker["worker_id"], "running")
        app.state.service._emit_callback(worker, "run.started", run=source, message="Work started")
        capability = _outbound_callback(
            outbound, event_type="run.started", run_id=source["run_id"]
        )["actionCapabilities"][0]
        request = _action_request(capability, idempotency_key="idem-cancel-0001")

        first = client.post("/v1/run-actions", json=request, headers=_capability_headers(capability))
        replay = client.post("/v1/run-actions", json=request, headers=_capability_headers(capability))

        assert first.status_code == 202
        assert first.json()["status"] == "accepted"
        assert first.json()["confirmationPending"] is True
        assert first.json()["newRun"] is None
        assert first.json()["idempotentReplay"] is False
        assert replay.status_code == 202
        assert replay.json()["idempotentReplay"] is True
        assert len(runtime.interrupts) == 1
        assert runtime.interrupts[0] == (worker["worker_id"], source["run_id"])
        assert app.state.store.get_run(source["run_id"])["state"] == "cancelled"
        terminal = _outbound_callback(outbound, event_type="run.cancelled", run_id=source["run_id"])
        assert terminal["run_state"] == "cancelled"
        assert "actionCapabilities" not in terminal


def test_cancel_replay_waits_for_expired_exact_claim_recovery_without_false_acceptance(
    tmp_path, monkeypatch
):
    outbound = _capture_callbacks(monkeypatch)
    runtime = FlakyInterruptRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        # The test claims its queued run itself; no background processor may take it first.
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Flaky cancel task",
            state="queued",
        )
        source = _truthfully_invoke_run(app, worker, source)
        app.state.store.update_worker_state(worker["worker_id"], "running")
        app.state.service._emit_callback(worker, "run.started", run=source, message="Work started")
        capability = _outbound_callback(
            outbound, event_type="run.started", run_id=source["run_id"]
        )["actionCapabilities"][0]
        body = _action_request(capability, idempotency_key="idem-flaky-cancel")

        first = client.post("/v1/run-actions", json=body, headers=_capability_headers(capability))
        assert first.status_code == 503
        assert first.json()["detail"]["code"] == "cancellation_not_accepted"
        assert app.state.store.get_run(source["run_id"])["state"] == "running"

        replay = client.post("/v1/run-actions", json=body, headers=_capability_headers(capability))
        assert replay.status_code == 202
        assert replay.json()["status"] == "pending"
        assert replay.json()["idempotentReplay"] is True
        assert len(runtime.interrupts) == 1
        assert app.state.store.get_run(source["run_id"])["state"] == "running"
        pending_worker = app.state.store.get_worker(worker["worker_id"])
        assert pending_worker["compute_release_kind"] == "stop_run"
        assert pending_worker["compute_release_target_run_id"] == source["run_id"]

        with app.state.store._connect() as conn:
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
            )
        recovered = app.state.service.recover_expired_compute_release_claims_once()
        assert [item["kind"] for item in recovered] == ["stop_run"]

        settled_replay = client.post(
            "/v1/run-actions", json=body, headers=_capability_headers(capability)
        )
        assert settled_replay.status_code == 202
        assert settled_replay.json()["status"] == "accepted"
        assert settled_replay.json()["idempotentReplay"] is True
        assert len(runtime.interrupts) == 2
        assert app.state.store.get_run(source["run_id"])["state"] == "cancelled"
        assert not app.state.store.get_worker(worker["worker_id"])[
            "compute_release_token"
        ]

        final_replay = client.post(
            "/v1/run-actions", json=body, headers=_capability_headers(capability)
        )
        assert final_replay.status_code == 202
        assert final_replay.json()["status"] == "accepted"
        assert len(runtime.interrupts) == 2


def test_concurrent_cancel_replay_invokes_owner_once_and_reports_pending_until_accepted(tmp_path, monkeypatch):
    _capture_callbacks(monkeypatch)
    runtime = BlockingInterruptRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app):
        project, worker = _create_scoped_worker(app)
        # The test claims its queued run itself; no background processor may take it first.
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Concurrent cancel task",
            state="queued",
        )
        source = _truthfully_invoke_run(app, worker, source)
        app.state.store.update_worker_state(worker["worker_id"], "running")
        callbacks = app.state.service._callback_config_for(worker)
        capability = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="cancel"
        )
        claims = app.state.service.verify_action_capability(capability["capability"])
        kwargs = {
            "capability_id": capability["capabilityId"],
            "action": "cancel",
            "project_id": project["project_id"],
            "worker_id": worker["worker_id"],
            "run_id": source["run_id"],
            "idempotency_key": "idem-concurrent-cancel",
        }

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(app.state.service.execute_run_action, claims, **kwargs)
            assert runtime.started.wait(1)
            concurrent = executor.submit(app.state.service.execute_run_action, claims, **kwargs).result(timeout=1)
            assert concurrent["status"] == "pending"
            assert concurrent["confirmationPending"] is True
            assert len(runtime.interrupts) == 1
            runtime.release.set()
            first = first_future.result(timeout=2)

        assert first["status"] == "accepted"
        assert len(runtime.interrupts) == 1


def test_stale_cancel_execution_lease_recovers_after_process_crash(tmp_path, monkeypatch):
    _capture_callbacks(monkeypatch)
    runtime = CountingInterruptRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app):
        project, worker = _create_scoped_worker(app)
        # The test claims its queued run itself; no background processor may take it first.
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Crash recovery cancel",
            state="queued",
        )
        source = _truthfully_invoke_run(app, worker, source)
        app.state.store.update_worker_state(worker["worker_id"], "running")
        callbacks = app.state.service._callback_config_for(worker)
        capability = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="cancel"
        )
        claims = app.state.service.verify_action_capability(capability["capability"])
        app.state.store.reserve_cancel_run_action(
            capability_id=capability["capabilityId"],
            idempotency_key="idem-crashed-cancel",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            source_run_id=source["run_id"],
            tenant_id=worker["tenant_id"],
            owner_id=worker["owner_id"],
        )
        with app.state.store._connect() as conn:
            conn.execute(
                "UPDATE run_action_uses SET updated_at = ? WHERE capability_id = ?",
                ("2000-01-01T00:00:00+00:00", capability["capabilityId"]),
            )

        recovered = app.state.service.execute_run_action(
            claims,
            capability_id=capability["capabilityId"],
            action="cancel",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            run_id=source["run_id"],
            idempotency_key="idem-crashed-cancel",
        )

        assert recovered["status"] == "accepted"
        assert recovered["idempotentReplay"] is True
        assert len(runtime.interrupts) == 1
        assert app.state.store.get_run_action(capability["capabilityId"])["status"] == "accepted"


@pytest.mark.parametrize("completion_timing", ["before_reserve", "after_reserve"])
def test_cancel_completion_race_returns_exact_already_completed_outcome(
    tmp_path,
    monkeypatch,
    completion_timing,
):
    outbound = _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        # The test claims its queued run itself; no background processor may take it first.
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Completing task",
            state="queued",
        )
        source = _truthfully_invoke_run(app, worker, source)
        app.state.store.update_worker_state(worker["worker_id"], "running")
        app.state.service._emit_callback(worker, "run.started", run=source, message="Work started")
        capability = _outbound_callback(
            outbound, event_type="run.started", run_id=source["run_id"]
        )["actionCapabilities"][0]
        if completion_timing == "before_reserve":
            app.state.store.finalize_run(
                source["run_id"],
                state="completed",
                output_text="Done",
                **_exact_terminal_generation(app, source["run_id"]),
            )
            app.state.store.update_worker_state(worker["worker_id"], "ready")
        else:
            original_stop = app.state.service.stop_run

            def complete_then_stop(worker_id: str, run_id: str):
                app.state.store.finalize_run(
                    run_id,
                    state="completed",
                    output_text="Done",
                    **_exact_terminal_generation(app, run_id),
                )
                app.state.store.update_worker_state(worker_id, "ready")
                return original_stop(worker_id, run_id)

            app.state.service.stop_run = complete_then_stop
        body = _action_request(
            capability,
            idempotency_key=f"idem-completion-race-{completion_timing}",
        )

        response = client.post("/v1/run-actions", json=body, headers=_capability_headers(capability))

        assert response.status_code == 409
        assert response.json() == {
            "version": 1,
            "status": "already_completed",
            "action": "cancel",
            "projectId": project["project_id"],
            "workerId": worker["worker_id"],
            "sourceRunId": source["run_id"],
            "state": "completed",
        }
        assert app.state.store.get_run(source["run_id"])["state"] == "completed"
        action_record = app.state.store.get_run_action(capability["capabilityId"])
        if completion_timing == "before_reserve":
            assert action_record is None
        else:
            assert action_record["status"] == "conflict"
            assert action_record["result_code"] == "run_already_completed"


def test_cancel_completion_during_owner_interrupt_preserves_completed_result(tmp_path, monkeypatch):
    outbound = _capture_callbacks(monkeypatch)
    runtime = CompletingDuringInterruptRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        # The test claims its queued run itself; no background processor may take it first.
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Complete during cancel",
            state="queued",
        )
        source = _truthfully_invoke_run(app, worker, source)
        app.state.store.update_worker_state(worker["worker_id"], "running")
        app.state.service._emit_callback(worker, "run.started", run=source, message="Work started")
        capability = _outbound_callback(
            outbound, event_type="run.started", run_id=source["run_id"]
        )["actionCapabilities"][0]

        def complete_before_interrupt_returns() -> None:
            app.state.store.finalize_run(
                source["run_id"],
                state="completed",
                output_text="Durable completed result",
                **_exact_terminal_generation(app, source["run_id"]),
            )
            app.state.store.update_worker_state(worker["worker_id"], "ready")

        runtime.complete_run = complete_before_interrupt_returns

        response = client.post(
            "/v1/run-actions",
            json=_action_request(capability, idempotency_key="idem-completed-inside-interrupt"),
            headers=_capability_headers(capability),
        )

        assert response.status_code == 409
        assert response.json() == {
            "version": 1,
            "status": "already_completed",
            "action": "cancel",
            "projectId": project["project_id"],
            "workerId": worker["worker_id"],
            "sourceRunId": source["run_id"],
            "state": "completed",
        }
        persisted = app.state.store.get_run(source["run_id"])
        assert persisted["state"] == "completed"
        assert persisted["output_text"] == "Durable completed result"
        assert not any(
            event["event_type"] == "run.interrupted"
            for event in app.state.store.list_events(worker["worker_id"])
        )


def test_retry_replay_restarts_canonical_queued_run_after_post_commit_crash(
    tmp_path, monkeypatch, background_consumers_disabled
):
    _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app):
        project, worker = _create_scoped_worker(app)
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Crash-window retry",
            state=RunRestorationState.RUNNING,
        )
        source = app.state.store.finalize_run(
            source["run_id"], state="failed", failure_retryable=1, failure_class="provider_temporarily_unavailable"
        )
        callbacks = app.state.service._callback_config_for(worker)
        capability = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="retry"
        )
        claims = app.state.service.verify_action_capability(capability["capability"])
        processor_calls: list[str] = []

        def crash_once(worker_id: str) -> None:
            processor_calls.append(worker_id)
            if len(processor_calls) == 1:
                raise RuntimeError("synthetic crash after durable run creation")

        app.state.service._ensure_worker_processor = crash_once
        kwargs = {
            "capability_id": capability["capabilityId"],
            "action": "retry",
            "project_id": project["project_id"],
            "worker_id": worker["worker_id"],
            "run_id": source["run_id"],
            "idempotency_key": "idem-retry-crash-window",
        }

        with pytest.raises(RuntimeError, match="synthetic crash"):
            app.state.service.execute_run_action(claims, **kwargs)
        replay = app.state.service.execute_run_action(claims, **kwargs)

        assert replay["status"] == "queued"
        assert replay["idempotentReplay"] is True
        assert processor_calls == [worker["worker_id"], worker["worker_id"]]
        queued = [
            run for run in app.state.store.list_runs_for_worker(worker["worker_id"])
            if run["run_id"] != source["run_id"]
        ]
        assert len(queued) == 1
        assert queued[0]["run_id"] == replay["newRun"]["runId"]


def test_callback_redelivery_capabilities_cannot_retry_one_source_twice(tmp_path, monkeypatch):
    _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Retry once",
            state=RunRestorationState.RUNNING,
        )
        source = app.state.store.finalize_run(
            source["run_id"],
            state="failed",
            failure_retryable=1,
            failure_class="provider_temporarily_unavailable",
        )
        callbacks = app.state.service._callback_config_for(worker)
        first_capability = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="retry"
        )
        redelivered_capability = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="retry"
        )

        first = client.post(
            "/v1/run-actions",
            json=_action_request(first_capability, idempotency_key="idem-first-source-retry"),
            headers=_capability_headers(first_capability),
        )
        assert first.status_code == 202
        app.state.store.finalize_run(
            first.json()["newRun"]["runId"],
            state="completed",
            output_text="First retry finished",
        )

        duplicate = client.post(
            "/v1/run-actions",
            json=_action_request(redelivered_capability, idempotency_key="idem-redelivered-source-retry"),
            headers=_capability_headers(redelivered_capability),
        )

        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "run_already_retried"
        descendants = [
            run
            for run in app.state.store.list_runs_for_worker(worker["worker_id"])
            if run["run_id"] != source["run_id"]
        ]
        assert len(descendants) == 1


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected_status"),
    [(65 * 60, 202), (120 * 60, 202), (24 * 60 * 60, 401)],
)
def test_cancel_capability_covers_extended_calls_then_expires(
    tmp_path,
    monkeypatch,
    elapsed_seconds,
    expected_status,
):
    outbound = _capture_callbacks(monkeypatch)
    runtime = CountingInterruptRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app)
        # The test claims its queued run itself; no background processor may take it first.
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Extended active task",
            state="queued",
        )
        source = _truthfully_invoke_run(app, worker, source)
        app.state.store.update_worker_state(worker["worker_id"], "running")
        app.state.service._emit_callback(worker, "run.started", run=source, message="Work started")
        capability = _outbound_callback(
            outbound, event_type="run.started", run_id=source["run_id"]
        )["actionCapabilities"][0]
        claims = unverified_run_action_claims(capability["capability"])
        monkeypatch.setattr(
            "workers_projects_runtime.run_actions.time.time",
            lambda: int(claims["issuedAtEpoch"]) + elapsed_seconds,
        )

        response = client.post(
            "/v1/run-actions",
            json=_action_request(capability, idempotency_key=f"idem-extended-{elapsed_seconds}"),
            headers=_capability_headers(capability),
        )

        assert response.status_code == expected_status
        if expected_status == 202:
            assert response.json()["status"] == "accepted"
            assert len(runtime.interrupts) == 1
        else:
            assert response.json()["detail"]["code"] == "capability_expired"
            assert runtime.interrupts == []


def test_enterprise_action_capability_is_sole_auth_for_exact_action_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-link-secret-distinct")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-secret")
    outbound = _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(app, tenant_id="tenant-alpha", owner_id="owner-a")
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Enterprise retry",
            state=RunRestorationState.RUNNING,
        )
        source = app.state.store.finalize_run(
            source["run_id"], state="failed", failure_retryable=1, failure_class="provider_temporarily_unavailable"
        )
        app.state.service._emit_callback(worker, "run.failed", run=source, message="Retry available")
        capability = _outbound_callback(
            outbound, event_type="run.failed", run_id=source["run_id"]
        )["actionCapabilities"][0]
        body = _action_request(capability, idempotency_key="idem-enterprise")

        wrong_body_scope = client.post(
            "/v1/run-actions",
            json={**body, "workerId": "wrk_forged"},
            headers=_capability_headers(capability),
        )
        unrelated_path = client.get(
            "/v1/projects", headers=_capability_headers(capability)
        )
        capability_only = client.post(
            "/v1/run-actions",
            json=body,
            headers=_capability_headers(capability),
        )

        assert wrong_body_scope.status_code == 403
        assert wrong_body_scope.json()["detail"]["code"] == "capability_scope_mismatch"
        assert unrelated_path.status_code == 401
        assert capability_only.status_code == 202
        assert capability_only.json()["newRun"]["runId"]


def test_enterprise_action_rejects_capability_outside_deployment_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-link-secret-distinct")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-secret")
    _capture_callbacks(monkeypatch)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project, worker = _create_scoped_worker(
            app,
            tenant_id="tenant-beta",
            owner_id="owner-beta",
        )
        app.state.service._ensure_worker_processor = lambda _worker_id: None
        source = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Wrong deployment tenant",
            state=RunRestorationState.RUNNING,
        )
        callbacks = app.state.service._callback_config_for(worker)
        capability = mint_run_action_capability(
            callbacks["hmac_secret"], worker=worker, run=source, action="cancel"
        )

        response = client.post(
            "/v1/run-actions",
            json=_action_request(capability, idempotency_key="idem-wrong-deployment-tenant"),
            headers=_capability_headers(capability),
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "capability_scope_mismatch"
