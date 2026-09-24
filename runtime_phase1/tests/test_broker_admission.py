from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import workers_projects_runtime.broker_admission as broker_admission_module
from workers_projects_runtime.bootstrap import bootstrap_env_for
from workers_projects_runtime.broker_admission import (
    BrokerAdmissionError,
    admit_capability_grant,
    mint_admission_header,
    preflight_provider_authorization,
    revoke_capability_grant,
)
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store

BODY = {
    "authorizationRef": "gha_synthetic_authorization_0001",
    "containerGenerationId": "a" * 64,
    "originRef": "ghi_synthetic_origin_0001",
    "runId": "run_synthetic_0001",
    "workRef": "work_synthetic_0001",
    "workerId": "wrk_synthetic_0001",
}
SECRET = "synthetic-admission-secret"
SCHEDULED_GENERATION = "b" * 64
SCHEDULED_PREPARE_BODY = {
    "ownerId": "synthetic-owner",
    "originRef": "scheduled-run-synthetic-0001",
    "workRef": "scheduled-run-synthetic-0001",
    "workerId": "wrk_scheduled_synthetic_0001",
    "runId": "run_scheduled_synthetic_0001",
    "containerGenerationId": SCHEDULED_GENERATION,
}


def _scheduled_prepare_success(**overrides):
    payload = {
        "status": "prepared",
        **SCHEDULED_PREPARE_BODY,
        "authorizationRef": "gha_scheduled_synthetic_0001",
        "scopeFingerprint": "scope_scheduled_synthetic_0001",
        "brokerUrl": (
            "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"
        ),
        "maxExpiresAt": "2099-01-01T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _success_body(**overrides):
    payload = {
        "status": "authorized",
        **BODY,
        "scopeFingerprint": "scope_synthetic_0001",
        "brokerUrl": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
        "grantToken": "synthetic-run-local-grant",
        "grant": {
            "grantId": "grant_synthetic_0001",
            "expiresAt": int(time.time()) + 60,
            "allowedServers": ["google_workspace"],
            "allowedHostTools": ["browser"],
            "scopes": {"content_read": True},
        },
        "maxExpiresAt": "2099-01-01T00:00:00+00:00",
    }
    if "hostStartupLeaseId" in overrides:
        payload.pop("containerGenerationId")
    payload.update(overrides)
    return payload


def test_admission_header_cross_language_vector_is_canonical_base64url_hmac():
    header = mint_admission_header(
        BODY,
        secret=SECRET,
        timestamp=1_786_543_200,
        nonce="nonce_synthetic_0001",
    )
    canonical = json.dumps(BODY, sort_keys=True, separators=(",", ":"))
    signing_input = f"v1\n1786543200\nnonce_synthetic_0001\n{canonical}"
    expected_digest = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

    assert header == f"v1:1786543200:nonce_synthetic_0001:{expected_digest}"
    assert header == (
        "v1:1786543200:nonce_synthetic_0001:"
        "t_xPS_Gucm9tJ7dWfvNDW2EdVCViQq4SpRSUbfdOEss"
    )


def test_scheduled_provider_prepare_signs_exact_generation_binding(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, *, content, headers, timeout):
        calls.append(
            {
                "url": url,
                "content": content,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return httpx.Response(
            200,
            json=_scheduled_prepare_success(),
            headers={"Cache-Control": "private, no-store"},
        )

    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post", fake_post
    )
    prepare = broker_admission_module.prepare_scheduled_provider_authorization
    prepared = prepare(
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
        secret=SECRET,
        body=SCHEDULED_PREPARE_BODY,
    )

    assert calls == [
        {
            "url": (
                "http://127.0.0.1:3180/api/viventium/glasshive/"
                "capabilities/prepare-scheduled"
            ),
            "content": json.dumps(
                SCHEDULED_PREPARE_BODY,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode(),
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Viventium-GlassHive-Admission": calls[0]["headers"][
                    "X-Viventium-GlassHive-Admission"
                ],
            },
            "timeout": 5.0,
        }
    ]
    assert calls[0]["headers"]["X-Viventium-GlassHive-Admission"].startswith(
        "v1:"
    )
    assert prepared.authorization_ref == "gha_scheduled_synthetic_0001"
    assert prepared.scope_fingerprint == "scope_scheduled_synthetic_0001"
    assert prepared.max_expires_at == "2099-01-01T00:00:00+00:00"
    assert "grantToken" not in str(prepared)
    assert "synthetic-run-local-grant" not in str(prepared)


@pytest.mark.parametrize(
    "mutation",
    ["missing_authorization", "forged_bearer", "mismatched_binding"],
)
def test_scheduled_provider_prepare_response_fails_closed(mutation, monkeypatch):
    payload = _scheduled_prepare_success()
    if mutation == "missing_authorization":
        payload.pop("authorizationRef")
    elif mutation == "forged_bearer":
        payload["grantToken"] = "forged-persisted-bearer"
    else:
        payload["containerGenerationId"] = "c" * 64
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json=payload,
            headers={"Cache-Control": "no-store"},
        ),
    )
    prepare = broker_admission_module.prepare_scheduled_provider_authorization

    with pytest.raises(BrokerAdmissionError) as captured:
        prepare(
            "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
            secret=SECRET,
            body=SCHEDULED_PREPARE_BODY,
        )

    assert captured.value.code == "scheduled_provider_authorization_response_invalid"
    assert captured.value.retryable is True


@pytest.mark.parametrize("mutation", ["missing_owner", "different_work", "bad_generation"])
def test_scheduled_provider_prepare_request_fails_before_network(mutation, monkeypatch):
    body = dict(SCHEDULED_PREPARE_BODY)
    if mutation == "missing_owner":
        body.pop("ownerId")
    elif mutation == "different_work":
        body["workRef"] = "scheduled-run-synthetic-0002"
    else:
        body["containerGenerationId"] = "not-an-exact-generation"
    calls: list[str] = []
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda url, **_kwargs: calls.append(url),
    )

    with pytest.raises(BrokerAdmissionError) as captured:
        broker_admission_module.prepare_scheduled_provider_authorization(
            "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
            secret=SECRET,
            body=body,
        )

    assert captured.value.code == "scheduled_provider_authorization_request_invalid"
    assert calls == []


def test_admission_uses_fresh_nonce_and_exact_canonical_body(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, *, content, headers, timeout):
        calls.append(
            {"url": url, "content": content, "headers": dict(headers), "timeout": timeout}
        )
        return httpx.Response(
            200,
            json=_success_body(),
            headers={"Cache-Control": "private, no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    first = admit_capability_grant(
        "http://127.0.0.1:3180/internal/admission",
        secret=SECRET,
        body=BODY,
        expected_scope_fingerprint="scope_synthetic_0001",
    )
    second = admit_capability_grant(
        "http://127.0.0.1:3180/internal/admission",
        secret=SECRET,
        body=BODY,
        expected_scope_fingerprint="scope_synthetic_0001",
    )

    assert first.grant_token == second.grant_token == "synthetic-run-local-grant"
    assert calls[0]["content"] == json.dumps(
        BODY, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert calls[0]["headers"]["Content-Type"] == "application/json"
    assert calls[0]["headers"]["X-Viventium-GlassHive-Admission"] != calls[1][
        "headers"
    ]["X-Viventium-GlassHive-Admission"]


@pytest.mark.parametrize("status_code", [401, 403, 409, 410])
def test_structured_needs_input_error_is_preserved(status_code, monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            status_code,
            json={
                "error": {
                    "code": "connected_account_reauthorization_required",
                    "message": "Reconnect the account to continue.",
                    "needsInput": True,
                }
            },
            headers={"Cache-Control": "no-store"},
        ),
    )

    with pytest.raises(BrokerAdmissionError) as captured:
        admit_capability_grant(
            "http://127.0.0.1:3180/internal/admission",
            secret=SECRET,
            body=BODY,
            expected_scope_fingerprint="scope_synthetic_0001",
        )

    assert captured.value.needs_input is True
    assert captured.value.retryable is False
    assert captured.value.code == "connected_account_reauthorization_required"


def test_non_needs_input_control_plane_failure_is_not_misreported(monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            503,
            json={
                "error": {
                    "code": "admission_temporarily_unavailable",
                    "message": "Admission is temporarily unavailable.",
                    "needsInput": False,
                }
            },
            headers={"Cache-Control": "no-store"},
        ),
    )

    with pytest.raises(BrokerAdmissionError) as captured:
        admit_capability_grant(
            "http://127.0.0.1:3180/internal/admission",
            secret=SECRET,
            body=BODY,
            expected_scope_fingerprint="scope_synthetic_0001",
        )

    assert captured.value.needs_input is False
    assert captured.value.retryable is True


def test_provider_preflight_uses_exact_run_grant_without_forwarding_a_model_request(
    monkeypatch,
):
    calls: list[dict] = []

    def fake_post(url, *, content, headers, timeout):
        calls.append(
            {
                "url": url,
                "content": content,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return httpx.Response(
            200,
            json={
                "status": "authorized",
                "provider": "openai",
                "workerId": BODY["workerId"],
                "runId": BODY["runId"],
            },
            headers={"Cache-Control": "private, no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)

    preflight_provider_authorization(
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
        grant_token="synthetic-run-local-grant",
        provider="openai",
        expected_worker_id=BODY["workerId"],
        expected_run_id=BODY["runId"],
    )

    assert calls == [
        {
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/providers/openai/auth/preflight",
            "content": b'{"version":1}',
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer synthetic-run-local-grant",
            },
            "timeout": 3.0,
        }
    ]


def test_provider_preflight_preserves_actionable_missing_auth(monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            409,
            json={
                "error": {
                    "code": "provider_auth_missing",
                    "message": "Connect the configured model account, then resume this work.",
                    "needsInput": True,
                }
            },
            headers={"Cache-Control": "no-store"},
        ),
    )

    with pytest.raises(BrokerAdmissionError) as captured:
        preflight_provider_authorization(
            "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
            grant_token="synthetic-run-local-grant",
            provider="openai",
            expected_worker_id=BODY["workerId"],
            expected_run_id=BODY["runId"],
        )

    assert captured.value.code == "provider_auth_missing"
    assert captured.value.needs_input is True
    assert captured.value.retryable is False


@pytest.mark.parametrize(
    "response_override",
    [
        {"runId": "run_wrong"},
        {"originRef": "ghi_wrong"},
        {"scopeFingerprint": "scope_wrong"},
        {"grantToken": ""},
        {"brokerUrl": "file:///tmp/not-a-broker"},
    ],
)
def test_success_fails_closed_on_binding_scope_or_grant_mismatch(
    response_override, monkeypatch
):
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json=_success_body(**response_override),
            headers={"Cache-Control": "no-store"},
        ),
    )
    with pytest.raises(BrokerAdmissionError, match="invalid"):
        admit_capability_grant(
            "http://127.0.0.1:3180/internal/admission",
            secret=SECRET,
            body=BODY,
            expected_scope_fingerprint="scope_synthetic_0001",
        )


class _CapturingHostRuntime(StubRuntime):
    def __init__(self):
        self.run_workers: list[dict] = []
        self.process_observer = None

    def set_host_process_observer(self, observer):
        self.process_observer = observer

    def reconcile_worker(self, worker):
        return RuntimeInfo(
            runtime="codex-cli",
            model="test",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=None,
            workspace_dir=None,
            pid=None,
        )

    def prepare_run_authority_context(self, worker, run_id=None):
        from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
        return ProfiledWorkerRuntime.prepare_run_authority_context(self, worker, run_id=run_id)

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)
        self.run_workers.append(dict(worker))
        return "FINAL REPORT:\nAuthorized result"




def _reserve_pending_admission(store: Store, *, execution_mode: str = "host"):
    bundle = {
        "run_mode": "mission",
        "glasshive_capability_authorization": {
            "version": 1,
            "status": "pending_admission",
            "authorization_ref": BODY["authorizationRef"],
            "origin_ref": BODY["originRef"],
            "max_expires_at": "2099-01-01T00:00:00+00:00",
            "scope_fingerprint": "scope_synthetic_0001",
        },
    }
    if execution_mode == "docker":
        bundle["execution_policy"] = "parallel-clean-room-v1"
    return store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key="delegation-admission-test",
        request_digest="digest-admission-test",
        origin_ref=BODY["originRef"],
        title="Admission mission",
        goal="Prove deferred admission",
        instruction="Use connected account data.",
        origin_surface="telegram",
        worker_name="Admission worker",
        worker_role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
        bootstrap_bundle=bundle,
    )


def _run_processor(service: WorkersProjectsService, worker_id: str) -> None:
    service._ensure_worker_processor(worker_id)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not service._local_processor_owns(worker_id):
            return
        time.sleep(0.01)
    raise AssertionError("GlassHive worker processor did not finish within 5 seconds")


def test_core_pending_authorization_schema_is_accepted_exactly(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    record = _reserve_pending_admission(store)
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        worker = store.get_worker(record["worker_id"])
        assert service._deferred_capability_authorization(worker) == {
            "authorization_ref": BODY["authorizationRef"],
            "origin_ref": BODY["originRef"],
            "max_expires_at": "2099-01-01T00:00:00+00:00",
            "scope_fingerprint": "scope_synthetic_0001",
        }
        bundle = json.loads(worker["bootstrap_bundle_json"])
        bundle["glasshive_capability_authorization"]["status"] = "authorized"
        worker["bootstrap_bundle_json"] = json.dumps(bundle)
        with pytest.raises(BrokerAdmissionError) as captured:
            service._deferred_capability_authorization(worker)
        assert captured.value.code == "broker_authorization_invalid"
    finally:
        service.shutdown()


def test_automatic_clean_room_missing_deferred_authorization_fails_before_runtime(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    record = _reserve_pending_admission(store)
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        worker = store.get_worker(record["worker_id"])
        run = store.get_run(record["run_id"])
        bundle = json.loads(worker["bootstrap_bundle_json"])
        bundle.pop("glasshive_capability_authorization")
        bundle["execution_policy"] = "parallel-clean-room-v1"
        bundle["viventium_launch_authority"] = {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        }
        worker["execution_mode"] = "docker"
        worker["bootstrap_bundle_json"] = json.dumps(bundle)

        with pytest.raises(BrokerAdmissionError) as captured:
            service._run_local_admitted_worker(worker, run)

        assert captured.value.code == "capability_authorization_missing"
        assert captured.value.needs_input is True
        assert captured.value.retryable is False
        assert service.runtime.run_workers == []
    finally:
        service.shutdown()


def test_clean_room_deferred_admission_keeps_exact_run_grant_in_memory_only(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)

    def fake_post(url, *, content, headers, timeout):
        if url.endswith("/revoke"):
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        if url.endswith("/auth/preflight"):
            return httpx.Response(
                200,
                json={
                    "status": "authorized",
                    "provider": "openai",
                    "workerId": record["worker_id"],
                    "runId": record["run_id"],
                },
                headers={"Cache-Control": "no-store"},
            )
        request = json.loads(content)
        return httpx.Response(
            200,
            json=_success_body(
                **request,
            ),
            headers={"Cache-Control": "no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    record = _reserve_pending_admission(store)
    worker = store.get_worker(record["worker_id"])
    run = store.get_run(record["run_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["execution_policy"] = "parallel-clean-room-v1"
    bundle["viventium_launch_authority"] = {
        "version": 1,
        "kind": "conversation_orchestrator",
        "execution_mode": "docker",
    }
    worker["execution_mode"] = "docker"
    worker["bootstrap_bundle_json"] = json.dumps(bundle)

    try:
        admitted = service._run_local_admitted_worker(
            worker,
            run,
            authority_context={"container_generation_id": "a" * 64},
        )

        assert bootstrap_env_for(admitted) == {
            "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-local-grant"
        }
        persisted = store.get_worker(record["worker_id"])
        assert "synthetic-run-local-grant" not in str(persisted["bootstrap_bundle_json"])
    finally:
        service.shutdown()


def test_scheduled_fallback_prepares_and_admits_exact_generation_without_delegation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "default")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "0")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "0")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "1")
    monkeypatch.setenv("WPR_DOCKER_DISK_RESERVATION_MB", "1")
    origin_ref = "scheduled-origin-synthetic-0001"
    calls: dict[str, list[dict]] = {
        "prepare": [],
        "admit": [],
        "preflight": [],
        "revoke": [],
    }

    def fake_post(url, *, content, headers, timeout):
        request = json.loads(content) if content else {}
        if url.endswith("/capabilities/prepare-scheduled"):
            calls["prepare"].append(request)
            return httpx.Response(
                200,
                json={
                    "status": "prepared",
                    **request,
                    "authorizationRef": "gha_scheduled_runtime_0001",
                    "scopeFingerprint": "scope_scheduled_runtime_0001",
                    "brokerUrl": (
                        "http://127.0.0.1:3180/api/viventium/glasshive/"
                        "capabilities/mcp"
                    ),
                    "maxExpiresAt": "2099-01-01T00:00:00+00:00",
                },
                headers={"Cache-Control": "private, no-store"},
            )
        if url.endswith("/capabilities/admit"):
            calls["admit"].append(request)
            return httpx.Response(
                200,
                json={
                    "status": "authorized",
                    **request,
                    "scopeFingerprint": "scope_scheduled_runtime_0001",
                    "brokerUrl": (
                        "http://127.0.0.1:3180/api/viventium/glasshive/"
                        "capabilities/mcp"
                    ),
                    "grantToken": "synthetic-scheduled-run-local-grant",
                    "grant": {
                        "grantId": "grant_scheduled_runtime_0001",
                        "expiresAt": int(time.time()) + 60,
                        "allowedServers": [],
                        "allowedHostTools": [],
                        "scopes": {"provider_invoke": True},
                    },
                    "maxExpiresAt": "2099-01-01T00:00:00+00:00",
                },
                headers={"Cache-Control": "private, no-store"},
            )
        if url.endswith("/providers/anthropic/auth/preflight"):
            calls["preflight"].append(
                {"request": request, "authorization": headers.get("Authorization")}
            )
            run_id = calls["prepare"][0]["runId"]
            worker_id = calls["prepare"][0]["workerId"]
            return httpx.Response(
                200,
                json={
                    "status": "authorized",
                    "provider": "anthropic",
                    "workerId": worker_id,
                    "runId": run_id,
                },
                headers={"Cache-Control": "private, no-store"},
            )
        if url.endswith("/capabilities/revoke"):
            calls["revoke"].append(request)
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        raise AssertionError(f"Unexpected synthetic broker URL: {url}")

    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post", fake_post
    )

    class ScheduledFallbackRuntime(StubRuntime):
        requires_run_start_identity = False

        def __init__(self):
            super().__init__()
            self.prepared: list[tuple[str, str]] = []
            self.invocations: list[dict] = []

        def resolve_model(self, profile, execution_mode="docker"):
            return f"stub/{profile}"

        def prepare_run_authority_context(self, worker, run_id=None):
            self.prepared.append((str(worker["profile"]), str(run_id)))
            return {"container_generation_id": SCHEDULED_GENERATION}

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            super().run_task(
                worker, instruction, timeout_sec=timeout_sec, run_id=run_id
            )
            bundle = json.loads(worker["bootstrap_bundle_json"])
            self.invocations.append(
                {
                    "profile": worker["profile"],
                    "run_id": run_id,
                    "env": dict(bundle.get("env") or {}),
                    "binding": dict(
                        worker.get("_run_local_capability_binding") or {}
                    ),
                }
            )
            return "SCHEDULED_FALLBACK_COMPLETED"

    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = ScheduledFallbackRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    project = service.create_project(
        "synthetic-owner",
        "Scheduled provider authority",
        "Run the exact scheduled fallback.",
        "codex-cli",
    )
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="synthetic-owner",
        name="Scheduled worker",
        role="scheduled prompt worker",
        profile="codex-cli",
        backend="codex-cli",
        execution_mode="docker",
        bootstrap_profile="clean-room",
        bootstrap_bundle={
            "execution_policy": "parallel-clean-room-v1",
            "viventium_launch_authority": {
                "version": 1,
                "kind": "prompt_workbench_scheduled",
                "execution_mode": "docker",
                "fallback_worker_profile": "claude-code",
            },
            "callbacks": {
                "events_webhook_url": (
                    "http://127.0.0.1:7010/internal/scheduled-prompts/"
                    "glasshive-callback"
                ),
                "hmac_secret": "synthetic-callback-secret",
                "origin_ref": origin_ref,
            },
            "env": {
                "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
                "WPR_CLAUDE_CODE_EFFORT": "default",
            },
        },
        start_synchronously=False,
    )
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    primary_route = service._provider_route(worker)
    for index in range(6):
        store.record_provider_route_failure(
            **primary_route,
            failure_class="provider_quota_exhausted",
            failure_structured=True,
            retry_at=retry_at.isoformat(),
            default_cooldown_s=300,
            run_id=f"scheduled-prior-{index}",
        )

    try:
        run = service.assign_run(
            worker["worker_id"], "Execute the scheduled health context."
        )
        deadline = time.time() + 5
        completed = None
        while time.time() < deadline:
            completed = store.get_run(run["run_id"])
            if completed and completed["state"] in {
                "completed",
                "failed",
                "needs_input",
            }:
                break
            time.sleep(0.01)

        assert completed is not None
        assert completed["state"] == "completed"
        assert completed["output_text"] == "SCHEDULED_FALLBACK_COMPLETED"
        assert completed["runtime_invoked_at"]
        attempts = store.list_run_attempts(run["run_id"])
        assert [attempt["state"] for attempt in attempts] == [
            "retry_queued", "completed"
        ]
        assert attempts[0]["runtime_invoked_at"] is None
        assert attempts[1]["runtime_invoked_at"] == completed["runtime_invoked_at"]
        started_events = [
            event for event in store.list_events(worker["worker_id"])
            if event["event_type"] == "run.started"
        ]
        assert len(started_events) == 1
        assert completed["provider_route_decision"] == "fallback_selected"
        assert completed["provider_route_profile"] == "claude-code"
        assert runtime.prepared == [("claude-code", run["run_id"])]
        assert calls["prepare"] == [
            {
                "ownerId": "synthetic-owner",
                "originRef": origin_ref,
                "workRef": origin_ref,
                "workerId": worker["worker_id"],
                "runId": run["run_id"],
                "containerGenerationId": SCHEDULED_GENERATION,
            }
        ]
        assert calls["admit"] == [
            {
                "authorizationRef": "gha_scheduled_runtime_0001",
                "originRef": origin_ref,
                "workRef": origin_ref,
                "workerId": worker["worker_id"],
                "runId": run["run_id"],
                "containerGenerationId": SCHEDULED_GENERATION,
            }
        ]
        assert runtime.invocations == [
            {
                "profile": "claude-code",
                "run_id": run["run_id"],
                "env": {
                    "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
                    "WPR_CLAUDE_CODE_EFFORT": "default",
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": (
                        "synthetic-scheduled-run-local-grant"
                    ),
                },
                "binding": {
                    "authorizationRef": "gha_scheduled_runtime_0001",
                    "originRef": origin_ref,
                    "workRef": origin_ref,
                    "workerId": worker["worker_id"],
                    "runId": run["run_id"],
                    "containerGenerationId": SCHEDULED_GENERATION,
                    "grantId": "grant_scheduled_runtime_0001",
                },
            }
        ]
        wait_until = time.time() + 2
        while time.time() < wait_until and not calls["revoke"]:
            time.sleep(0.01)
        assert calls["preflight"] == [
            {
                "request": {"version": 1},
                "authorization": "Bearer synthetic-scheduled-run-local-grant",
            }
        ]
        assert calls["revoke"] == [runtime.invocations[0]["binding"]]
        assert store.get_delegation_for_worker(
            worker["worker_id"],
            tenant_id=worker["tenant_id"],
            owner_id=worker["owner_id"],
        ) is None
        persisted = store.get_worker(worker["worker_id"])
        assert "synthetic-scheduled-run-local-grant" not in str(
            persisted["bootstrap_bundle_json"]
        )
        assert "grant_scheduled_runtime_0001" not in str(
            persisted["bootstrap_bundle_json"]
        )
        assert "gha_scheduled_runtime_0001" not in str(
            persisted["bootstrap_bundle_json"]
        )
        assert "scope_scheduled_runtime_0001" not in str(
            persisted["bootstrap_bundle_json"]
        )
    finally:
        service.shutdown()


def test_clean_room_continuation_keeps_server_context_and_admitted_grant_ephemeral(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    record = _reserve_pending_admission(store)

    def fake_post(url, *, content, headers, timeout):
        request = json.loads(content)
        if url.endswith("/revoke"):
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        if url.endswith("/auth/preflight"):
            return httpx.Response(
                200,
                json={
                    "status": "authorized",
                    "provider": "openai",
                    "workerId": record["worker_id"],
                    "runId": record["run_id"],
                },
                headers={"Cache-Control": "no-store"},
            )
        return httpx.Response(
            200,
            json=_success_body(
                **request,
            ),
            headers={"Cache-Control": "no-store"},
        )

    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post", fake_post
    )
    worker = store.get_worker(record["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["execution_policy"] = "parallel-clean-room-v1"
    bundle["viventium_launch_authority"] = {
        "version": 1,
        "kind": "conversation_orchestrator",
        "execution_mode": "docker",
    }
    worker["execution_mode"] = "docker"
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    run = {
        **store.get_run(record["run_id"]),
        "continuation_contract_json": json.dumps(
            {"version": 1, "run_id": record["run_id"], "mode": "resume"}
        ),
        "continuation_context_json": json.dumps(
            {
                "version": 1,
                "base_instruction": "preserve workspace",
                "guidance": ["finish"],
            }
        ),
    }

    try:
        run_worker = service._run_local_worker(
            worker,
            run,
            authority_context={"container_generation_id": "a" * 64},
        )
        run_bundle = json.loads(run_worker["bootstrap_bundle_json"])

        assert bootstrap_env_for(run_worker) == {
            "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-local-grant"
        }
        assert run_bundle["viventium_continuation_contract"] == {
            "version": 1,
            "run_id": record["run_id"],
            "mode": "resume",
        }
        assert run_bundle["viventium_continuation_context"] == {
            "version": 1,
            "base_instruction": "preserve workspace",
            "guidance": ["finish"],
        }
        persisted = store.get_worker(record["worker_id"])
        persisted_bundle = str(persisted["bootstrap_bundle_json"])
        assert "synthetic-run-local-grant" not in persisted_bundle
        assert "viventium_continuation_contract" not in persisted_bundle
        assert "viventium_continuation_context" not in persisted_bundle
    finally:
        service.shutdown()


def test_expired_pending_authorization_uses_canonical_reauthorization_code(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    record = _reserve_pending_admission(store)
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        worker = store.get_worker(record["worker_id"])
        bundle = json.loads(worker["bootstrap_bundle_json"])
        bundle["glasshive_capability_authorization"]["max_expires_at"] = (
            "2000-01-01T00:00:00+00:00"
        )
        worker["bootstrap_bundle_json"] = json.dumps(bundle)

        with pytest.raises(BrokerAdmissionError) as captured:
            service._deferred_capability_authorization(worker)

        assert captured.value.code == "capability_authorization_horizon_expired"
        assert captured.value.needs_input is True
        assert captured.value.retryable is False
    finally:
        service.shutdown()


def test_deferred_admission_overlays_grant_only_for_exact_started_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    captured_requests: list[tuple[str, dict]] = []

    def fake_post(_url, *, content, headers, timeout):
        captured_request = json.loads(content)
        captured_requests.append((_url, captured_request))
        assert "synthetic-run-local-grant" not in content.decode()
        if _url.endswith("/revoke"):
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        assert "/providers/" not in _url
        return httpx.Response(
            200,
            json=_success_body(
                **captured_request,
            ),
            headers={"Cache-Control": "no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    leases = []
    acquire = service._acquire_host_run_lease
    def capture_lease(worker, run):
        lease = acquire(worker, run)
        leases.append(dict(lease))
        return lease
    service._acquire_host_run_lease = capture_lease
    record = _reserve_pending_admission(store)
    before = store.get_worker(record["worker_id"])
    assert "synthetic-run-local-grant" not in str(before["bootstrap_bundle_json"])

    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    lease_digest = hashlib.sha256(leases[0]["startup_token"].encode()).hexdigest()
    assert captured_requests[0][1] == {
        **{key: value for key, value in BODY.items() if key != "containerGenerationId"},
        "hostStartupLeaseId": lease_digest,
        "runId": record["initial_run_id"],
        "workRef": record["work_ref"],
        "workerId": record["worker_id"],
    }
    assert len(captured_requests) == 2
    assert captured_requests[1][0].endswith("/revoke")
    assert captured_requests[1][1] == {
        **captured_requests[0][1],
        "grantId": "grant_synthetic_0001",
    }
    assert len(runtime.run_workers) == 1
    assert runtime.run_workers[0]["_run_startup_token_digest"] == lease_digest
    assert runtime.run_workers[0]["_run_local_capability_binding"]["hostStartupLeaseId"] == lease_digest
    assert leases[0]["startup_token"] not in str(runtime.run_workers)
    assert leases[0]["startup_token"] not in str(captured_requests)
    run_bundle = json.loads(runtime.run_workers[0]["bootstrap_bundle_json"])
    assert run_bundle["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == (
        "synthetic-run-local-grant"
    )
    assert run_bundle["glasshive_capability_broker"]["scope_fingerprint"] == (
        "scope_synthetic_0001"
    )
    persisted = store.get_worker(record["worker_id"])
    assert "synthetic-run-local-grant" not in str(persisted["bootstrap_bundle_json"])
    assert store.get_run(record["initial_run_id"])["state"] == "completed"
    assert store.get_run(record["initial_run_id"])["runtime_invoked_at"]
    assert [
        attempt["state"]
        for attempt in store.list_run_attempts(record["initial_run_id"])
    ] == ["completed"]
    started_events = [
        event for event in store.list_events(record["worker_id"])
        if event["event_type"] == "run.started"
    ]
    assert len(started_events) == 1
    assert store.list_active_host_run_leases() == []


def test_missing_provider_preflight_blocks_before_runtime_and_closes_exact_attempt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    calls: list[str] = []

    def fake_post(url, *, content, headers, timeout):
        calls.append(url)
        request = json.loads(content)
        if url.endswith("/auth/preflight"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "provider_auth_missing",
                        "message": "Connect the configured model account, then resume this work.",
                        "needsInput": True,
                    }
                },
                headers={"Cache-Control": "no-store"},
            )
        if url.endswith("/revoke"):
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        return httpx.Response(
            200,
            json=_success_body(
                **request,
            ),
            headers={"Cache-Control": "no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    # Set the fixture's authority before background queue recovery can claim it.
    # Provider brokerage is a Docker-only route; native hosts own account auth.
    record = _reserve_pending_admission(store, execution_mode="docker")
    runtime.prepare_run_authority_context = lambda worker, run_id=None: {"container_generation_id": "a" * 64}
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None

    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    run = store.get_run(record["initial_run_id"])
    assert run is not None
    assert run["state"] == "needs_input"
    assert run["failure_class"] == "provider_auth_missing"
    assert run["runtime_invoked_at"] is None
    assert runtime.run_workers == []
    assert store.list_active_host_run_leases() == []
    attempts = store.list_run_attempts(record["initial_run_id"])
    assert len(attempts) == 1
    assert attempts[0]["state"] == "needs_input"
    assert attempts[0]["ended_at"]
    assert calls[0].endswith("/admit")
    assert calls[1].endswith("/providers/openai/auth/preflight")
    assert calls[2].endswith("/revoke")
    detail = store.work_trace_detail(
        run_id=run["run_id"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert detail is not None
    assert detail["traceability"]["contractVersion"] == 2
    assert detail["traceability"]["runtimeInvocations"] == []
    preflights = detail["traceability"]["providerAuthorizationPreflights"]
    assert len(preflights) == 1
    assert preflights[0]["attemptNumber"] == 1
    assert preflights[0]["provider"] == "openai"
    assert preflights[0]["status"] == "needs_input"
    assert preflights[0]["failureClass"] == "provider_auth_missing"
    assert preflights[0]["providerAuthorizationPreflightRef"].startswith(
        "provider_authorization_preflight_sha256:"
    )


def test_failed_exact_grant_revocation_is_durable_and_recovers_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    calls: list[str] = []
    fail_revoke = True

    def fake_post(url, *, content, headers, timeout):
        nonlocal fail_revoke
        calls.append(url)
        body = json.loads(content)
        if url.endswith("/revoke"):
            if fail_revoke:
                raise httpx.ConnectError("synthetic unavailable")
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        return httpx.Response(
            200,
            json=_success_body(
                **body,
            ),
            headers={"Cache-Control": "no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    service._capability_revocation_retry_delay_s = lambda _record: 0.0
    record = _reserve_pending_admission(store)
    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    pending = store.list_capability_grant_revocations()
    assert len(pending) == 1
    assert len(pending[0]["host_startup_lease_id"]) == 64
    assert {
        key: pending[0][key]
        for key in (
            "authorization_ref",
            "origin_ref",
            "work_ref",
            "worker_id",
            "run_id",
            "grant_id",
            "container_generation_id",
            "host_startup_lease_id",
            "status",
            "attempts",
            "last_error_code",
        )
    } == {
        "authorization_ref": BODY["authorizationRef"],
        "origin_ref": BODY["originRef"],
        "work_ref": record["work_ref"],
        "worker_id": record["worker_id"],
        "run_id": record["initial_run_id"],
        "grant_id": "grant_synthetic_0001",
        "container_generation_id": "",
        "host_startup_lease_id": pending[0]["host_startup_lease_id"],
        "status": "pending",
        "attempts": 1,
        "last_error_code": "broker_revocation_unavailable",
    }

    fail_revoke = False
    recovered = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        deadline = time.time() + 2
        while time.time() < deadline:
            current = store.list_capability_grant_revocations()[0]
            if current["status"] == "applied":
                break
            time.sleep(0.01)
        assert current["status"] == "applied"
        assert current["attempts"] == 2
    finally:
        recovered.shutdown()
    assert sum(url.endswith("/revoke") for url in calls) == 2


def test_exact_generation_revocation_uses_a_fresh_signed_request(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, *, content, headers, timeout):
        calls.append(
            {"url": url, "content": content, "headers": dict(headers), "timeout": timeout}
        )
        return httpx.Response(204, headers={"Cache-Control": "private, no-store"})

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    revoke_capability_grant(
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
        secret=SECRET,
        body={**BODY, "grantId": "grant_synthetic_0001"},
    )

    assert calls[0]["url"].endswith("/api/viventium/glasshive/capabilities/revoke")
    assert calls[0]["content"] == json.dumps(
        {**BODY, "grantId": "grant_synthetic_0001"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert calls[0]["headers"]["X-Viventium-GlassHive-Admission"].startswith("v1:")


def test_needs_input_admission_does_not_start_provider_and_releases_lease(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/internal/admission",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            410,
            json={
                "error": {
                    "code": "connected_account_reauthorization_required",
                    "message": "Reconnect the account to continue.",
                    "needsInput": True,
                }
            },
            headers={"Cache-Control": "no-store"},
        ),
    )
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    record = _reserve_pending_admission(store)
    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    assert runtime.run_workers == []
    run = store.get_run(record["initial_run_id"])
    worker = store.get_worker(record["worker_id"])
    assert run["state"] == "needs_input"
    assert run["failure_class"] == "connected_account_reauthorization_required"
    assert worker["state"] == "needs_input"
    assert store.list_active_host_run_leases() == []


def test_retryable_admission_failure_requeues_without_failed_event_or_callback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_RETRY_BASE_DELAY_S", "300")
    monkeypatch.setenv("GLASSHIVE_RETRY_MAX_DELAY_S", "300")
    store = Store(str(tmp_path / "runtime.sqlite3"))
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_URL", "http://127.0.0.1:3180/internal/admission")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("synthetic unavailable")
    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", unavailable)
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    callbacks: list[str] = []
    service._emit_callback = (
        lambda _worker, event_type, **_kwargs: callbacks.append(event_type)
    )
    record = _reserve_pending_admission(store)

    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    run = store.get_run(record["initial_run_id"])
    worker = store.get_worker(record["worker_id"])
    event_types = {
        event["event_type"] for event in store.list_events(record["worker_id"])
    }
    assert runtime.run_workers == []
    assert run["state"] == "queued"
    assert run["failure_class"] == "broker_admission_unavailable"
    assert run["failure_retryable"] == 1
    assert run["retry_after"]
    assert worker["state"] == "ready"
    assert store.list_active_host_run_leases() == []
    assert "run.waiting_on_capacity" in event_types
    assert "run.failed" not in event_types
    assert callbacks == ["run.waiting_on_capacity"]


def test_required_protected_capability_unavailable_needs_input_without_provider_start(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    record = _reserve_pending_admission(store)
    worker = store.get_worker(record["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle.pop("glasshive_capability_authorization", None)
    bundle["glasshive_capability_requirement"] = {
        "version": 1,
        "required": True,
        "status": "unavailable",
        "reason": "broker_config_unavailable",
    }
    store.update_worker(
        record["worker_id"], bootstrap_bundle_json=json.dumps(bundle, sort_keys=True)
    )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None

    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    assert runtime.run_workers == []
    run = store.get_run(record["initial_run_id"])
    worker = store.get_worker(record["worker_id"])
    assert run["state"] == "needs_input"
    assert run["failure_class"] == "required_capability_unavailable"
    assert worker["state"] == "needs_input"
    assert store.list_active_host_run_leases() == []


HOST_BODY = {
    **{key: value for key, value in BODY.items() if key != "containerGenerationId"},
    "hostStartupLeaseId": "d" * 64,
}


@pytest.mark.parametrize("mutation", ["both", "neither", "malformed", "unknown"])
def test_host_admission_rejects_ambiguous_or_invalid_generation_before_network(
    mutation, monkeypatch
):
    body = dict(HOST_BODY)
    if mutation == "both":
        body["containerGenerationId"] = "a" * 64
    elif mutation == "neither":
        body.pop("hostStartupLeaseId")
    elif mutation == "malformed":
        body["hostStartupLeaseId"] = "not-a-startup-lease-digest"
    else:
        body["executionMode"] = "host"
    calls = []
    monkeypatch.setattr(broker_admission_module.httpx, "post", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(BrokerAdmissionError, match="binding is invalid"):
        admit_capability_grant(
            "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
            secret=SECRET,
            body=body,
            expected_scope_fingerprint="scope_synthetic_0001",
        )
    with pytest.raises(BrokerAdmissionError, match="binding is invalid"):
        revoke_capability_grant(
            "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
            secret=SECRET,
            body={**body, "grantId": "grant_synthetic_0001"},
        )
    assert calls == []


@pytest.mark.parametrize("mutation", ["none", "other_lease", "container", "both"])
def test_host_admission_requires_exact_generation_response(mutation, monkeypatch):
    payload = _success_body(**HOST_BODY)
    if mutation == "other_lease":
        payload["hostStartupLeaseId"] = "e" * 64
    elif mutation == "container":
        payload.pop("hostStartupLeaseId")
        payload["containerGenerationId"] = "d" * 64
    elif mutation == "both":
        payload["containerGenerationId"] = "d" * 64
    calls = []
    def post(url, *, content, headers, timeout):
        calls.append((json.loads(content), headers))
        return httpx.Response(200, json=payload, headers={"Cache-Control": "no-store"})
    monkeypatch.setattr(broker_admission_module.httpx, "post", post)
    if mutation == "none":
        grant = admit_capability_grant(
            "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
            secret=SECRET, body=HOST_BODY,
            expected_scope_fingerprint="scope_synthetic_0001",
        )
        assert grant.host_startup_lease_id == "d" * 64
        assert grant.container_generation_id == ""
        assert calls[0][0] == HOST_BODY
        header = calls[0][1]["X-Viventium-GlassHive-Admission"]
        _, timestamp, nonce, digest = header.split(":")
        canonical = json.dumps(HOST_BODY, sort_keys=True, separators=(",", ":"))
        material = f"v1\n{timestamp}\n{nonce}\n{canonical}"
        expected = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), material.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        assert digest == expected
    else:
        with pytest.raises(BrokerAdmissionError, match="response is invalid"):
            admit_capability_grant(
                "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
                secret=SECRET, body=HOST_BODY,
                expected_scope_fingerprint="scope_synthetic_0001",
            )


@pytest.mark.parametrize("mutation", ["none", "stale_lease", "missing_lease", "other_run", "other_worker", "container", "both"])
def test_native_host_checks_exact_admitted_lease_before_secret_materialization(mutation):
    from workers_projects_runtime.profile_runtime import HostNativeCliMixin
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
    class ReachedMaterialization(Exception):
        pass
    class Runtime(HostNativeCliMixin):
        def _conversation_mode_from_worker(self, worker):
            return False
        def ensure_worker_ready(self, worker):
            raise ReachedMaterialization()
    worker = {
        "worker_id": BODY["workerId"],
        "_run_startup_token_digest": "d" * 64,
        "_run_local_capability_binding": dict(HOST_BODY),
    }
    binding = worker["_run_local_capability_binding"]
    if mutation == "stale_lease":
        worker["_run_startup_token_digest"] = "e" * 64
    elif mutation == "missing_lease":
        worker.pop("_run_startup_token_digest")
    elif mutation == "other_run":
        binding["runId"] = "run_other_synthetic"
    elif mutation == "other_worker":
        binding["workerId"] = "wrk_other_synthetic"
    elif mutation == "container":
        binding.pop("hostStartupLeaseId")
        binding["containerGenerationId"] = "d" * 64
    elif mutation == "both":
        binding["containerGenerationId"] = "d" * 64
    expected = ReachedMaterialization if mutation == "none" else RuntimeErrorBase
    with pytest.raises(expected):
        Runtime().run_task(worker, "Use approved capabilities.", run_id=BODY["runId"])


@pytest.mark.parametrize("mode,context", [
    ("host", {"container_generation_id": "a" * 64}),
    ("docker", {"host_startup_lease_id": "d" * 64}),
    ("host", {"container_generation_id": "a" * 64, "host_startup_lease_id": "d" * 64}),
])
def test_service_does_not_exchange_generation_kinds(tmp_path, monkeypatch, mode, context):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    record = _reserve_pending_admission(store)
    worker = {**store.get_worker(record["worker_id"]), "execution_mode": mode}
    service = WorkersProjectsService(store, _CapturingHostRuntime(), reconcile_on_startup=False)
    calls = []
    monkeypatch.setattr(broker_admission_module.httpx, "post", lambda *args, **kwargs: calls.append(args))
    try:
        with pytest.raises(BrokerAdmissionError) as error:
            service._run_local_admitted_worker(worker, store.get_run(record["run_id"]), authority_context=context)
        assert error.value.code == "broker_admission_generation_unavailable"
        assert calls == []
    finally:
        service.shutdown()


def test_existing_docker_revocation_schema_migrates_without_changing_binding(tmp_path):
    import sqlite3
    path = str(tmp_path / "runtime.sqlite3")
    store = Store(path)
    record = _reserve_pending_admission(store)
    binding = {**BODY, "runId": record["run_id"], "workerId": record["worker_id"], "grantId": "grant_synthetic_0001"}
    original = store.enqueue_capability_grant_revocation(binding)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE capability_grant_revocations DROP COLUMN host_startup_lease_id")
    migrated = Store(path)
    restored = migrated.list_capability_grant_revocations()[0]
    assert restored == {**{key: value for key, value in original.items() if key != "host_startup_lease_id"}, "host_startup_lease_id": ""}
    assert migrated.enqueue_capability_grant_revocation(binding)["revocation_id"] == original["revocation_id"]
