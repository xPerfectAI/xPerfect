from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.inference_broker import (
    ADAPTER_ID,
    GlassHiveInferenceBroker,
    InferenceBrokerConfig,
    InferenceBrokerError,
    InferenceBrokerOwnerBinding,
    _default_request,
    _sign_claims,
    _urlsafe_b64decode,
    _urlsafe_b64encode,
    inference_broker_config_from_environment,
    validated_codex_broker_projection,
)
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo
from workers_projects_runtime.profile_runtime import CodexCliRuntime, ProfiledWorkerRuntime


SECRET = "synthetic-broker-secret-with-at-least-32-characters"
NOW = 2_000_000_000


def test_default_broker_request_refuses_redirect_without_forwarding_authorization():
    destination_requests: list[str | None] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            destination_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            return

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(307)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{destination.server_address[1]}/capture",
            )
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            return

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        with pytest.raises(InferenceBrokerError) as exc_info:
            _default_request(
                "POST",
                f"http://127.0.0.1:{redirect.server_address[1]}/redirect",
                {"Authorization": "Bearer synthetic-secret"},
                b"{}",
                2,
            )
        assert exc_info.value.code == "broker_redirect_rejected"
        assert destination_requests == []
    finally:
        redirect.shutdown()
        redirect.server_close()
        destination.shutdown()
        destination.server_close()


def config() -> InferenceBrokerConfig:
    return InferenceBrokerConfig(
        issuer_url="https://librechat.example.test/api/viventium/glasshive/inference",
        proxy_base_url="https://librechat.example.test/api/viventium/glasshive/inference",
        secret=SECRET,
        broker_tenant_id="broker-tenant",
        owner_bindings=(
            InferenceBrokerOwnerBinding(
                glasshive_tenant_id="glass-tenant",
                glasshive_owner_id="owner-a",
                librechat_user_id="user-a",
                proof="operator_verified",
            ),
        ),
    )


class FakeBrokerService:
    def __init__(self, *, expires_at: int | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self.expires_at = expires_at

    def __call__(self, method, url, headers, body, timeout):
        assertion = json.loads(
            _urlsafe_b64decode(headers["Authorization"].removeprefix("Bearer ")).decode()
        )
        signature = assertion.pop("sig")
        assert signature == _sign_claims(assertion, SECRET, "issuer")
        self.requests.append(
            {
                "method": method,
                "url": url,
                "assertion": dict(assertion),
                "body": json.loads(body),
                "timeout": timeout,
            }
        )
        if url.endswith("/grants/revoke"):
            token = json.loads(body)["grantToken"]
            grant = json.loads(_urlsafe_b64decode(token).decode())
            return 200, {"cache-control": "no-store"}, {
                "revoked": True,
                "grantId": grant["grant_id"],
            }

        grant_id = "ghcb_infer_" + "a" * 64
        grant_unsigned = {
            "aud": "glasshive-inference-proxy",
            "grant_id": grant_id,
            "tenant_id": assertion["tenant_id"],
            "user_id": assertion["user_id"],
            "worker_id": assertion["worker_id"],
            "run_id": assertion["run_id"],
            "provider": assertion["provider"],
            "route": assertion["route"],
            "adapter": assertion["adapter"],
            "models": assertion["models"],
            "iat": assertion["iat"],
            "exp": self.expires_at if self.expires_at is not None else assertion["iat"] + 600,
            "nonce": assertion["nonce"],
        }
        grant = {**grant_unsigned, "sig": _sign_claims(grant_unsigned, SECRET, "grant")}
        token = _urlsafe_b64encode(json.dumps(grant, separators=(",", ":")).encode())
        return 201, {"cache-control": "no-store"}, {
            "grantToken": token,
            "grantId": grant_id,
            "provider": "openai",
            "route": assertion["route"],
            "expiresAt": datetime.fromtimestamp(
                grant_unsigned["exp"], timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "adapter": {
                "id": ADAPTER_ID,
                "baseUrl": "https://librechat.example.test/api/viventium/glasshive/inference/openai/v1",
                "auth": "bearer_grant",
                "paths": ["/responses"],
                "supportsStreaming": True,
            },
        }


def test_environment_requires_explicit_verified_owner_binding(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_URL", config().issuer_url)
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_SECRET", SECRET)
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_TENANT_ID", "broker-tenant")
    monkeypatch.setenv(
        "GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON",
        json.dumps(
            [
                {
                    "glasshive_tenant_id": "glass-tenant",
                    "glasshive_owner_id": "owner-a",
                    "librechat_user_id": "different-user",
                    "proof": "shared_oidc_subject",
                }
            ]
        ),
    )

    with pytest.raises(InferenceBrokerError, match="same canonical principal"):
        inference_broker_config_from_environment()


def test_provider_account_api_creates_only_a_broker_reference_for_verified_owner(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_URL", config().issuer_url)
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_SECRET", SECRET)
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_TENANT_ID", "broker-tenant")
    monkeypatch.setenv(
        "GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON",
        json.dumps(
            [
                {
                    "glasshive_tenant_id": "local",
                    "glasshive_owner_id": "demo-owner",
                    "librechat_user_id": "user-a",
                    "proof": "operator_verified",
                }
            ]
        ),
    )
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(str(database), runtime_backend="stub"))

    response = client.post(
        "/v1/provider-accounts",
        json={
            "provider": "codex",
            "label": "Personal OpenAI",
            "auth_method": "api_key",
            "platform_support": "client-cannot-enable-this",
            "secret_locator": "secret-store://attacker-controlled",
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "ready"
    assert "secret_locator" not in response.json()
    record = ControlPlaneStore(str(database)).get_provider_account_record(
        account_id=response.json()["account_id"],
        tenant_id="local",
        owner_id="demo-owner",
    )
    assert record["secret_locator"] == "broker://librechat-openai"


def test_issue_is_exactly_run_bound_and_revoke_happens_on_exit():
    service = FakeBrokerService()
    broker = GlassHiveInferenceBroker(config(), request=service, now=lambda: NOW)

    with broker.bind_run(
        tenant_id="glass-tenant",
        owner_id="owner-a",
        worker_id="worker-a",
        run_id="run-a",
        auth_method="api_key",
        models=["gpt-5.4"],
    ) as projection:
        assert projection["adapter"] == ADAPTER_ID
        assert projection["worker_id"] == "worker-a"
        assert projection["run_id"] == "run-a"

    assert [request["assertion"]["action"] for request in service.requests] == [
        "issue",
        "revoke",
    ]
    assert all(request["assertion"]["user_id"] == "user-a" for request in service.requests)
    assert all(request["assertion"]["worker_id"] == "worker-a" for request in service.requests)
    assert all(request["assertion"]["run_id"] == "run-a" for request in service.requests)


def test_cross_user_binding_and_expired_grant_fail_closed():
    broker = GlassHiveInferenceBroker(config(), request=FakeBrokerService(), now=lambda: NOW)
    with pytest.raises(InferenceBrokerError, match="no explicitly verified"):
        broker.issue(
            tenant_id="glass-tenant",
            owner_id="owner-b",
            worker_id="worker-a",
            run_id="run-a",
            auth_method="api_key",
            models=["gpt-5.4"],
        )

    expired = GlassHiveInferenceBroker(
        config(), request=FakeBrokerService(expires_at=NOW), now=lambda: NOW
    )
    with pytest.raises(InferenceBrokerError, match="scope does not match"):
        expired.issue(
            tenant_id="glass-tenant",
            owner_id="owner-a",
            worker_id="worker-a",
            run_id="run-a",
            auth_method="api_key",
            models=["gpt-5.4"],
        )


def test_projection_rejects_cross_run_or_expired_reuse():
    projection = {
        "adapter": ADAPTER_ID,
        "grant_token": "synthetic-short-lived-grant",
        "base_url": "https://librechat.example.test/api/viventium/glasshive/inference/openai/v1",
        "worker_id": "worker-a",
        "run_id": "run-a",
        "models": ["gpt-5.4"],
        "expires_at": int(__import__("time").time()) + 300,
    }
    with pytest.raises(RuntimeErrorBase, match="does not match"):
        validated_codex_broker_projection(
            {
                "worker_id": "worker-a",
                "model": "gpt-5.4",
                "_active_run_id": "run-b",
                "_glasshive_inference_broker": projection,
            }
        )


def test_codex_projects_only_reviewed_responses_adapter_and_run_headers(tmp_path, monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "worker-a",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "_active_run_id": "run-a",
        "_glasshive_inference_broker_bound": True,
        "_glasshive_inference_broker": {
            "adapter": ADAPTER_ID,
            "grant_token": "synthetic-short-lived-grant",
            "base_url": "https://librechat.example.test/api/viventium/glasshive/inference/openai/v1",
            "worker_id": "worker-a",
            "run_id": "run-a",
            "models": ["gpt-5.4"],
            "expires_at": int(__import__("time").time()) + 300,
        },
    }
    command, env = runtime._build_command(
        worker,
        "Synthetic task",
        RuntimeInfo(
            runtime="codex-cli",
            model="gpt-5.4",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            pid=None,
        ),
    )
    rendered = "\n".join(command)
    assert 'wire_api="responses"' in rendered
    assert "X-GlassHive-Worker-Id" in rendered
    assert "X-GlassHive-Run-Id" in rendered
    assert "openai_responses_v1" not in rendered  # adapter is a contract, not a fake Codex feature.
    assert env["OPENAI_API_KEY"] == "synthetic-short-lived-grant"
    assert "synthetic-short-lived-grant" not in rendered


class RecordingRuntime:
    runtime_name = "codex-cli"

    def __init__(self):
        self.calls = []
        self.usage = {"input_tokens": 11, "output_tokens": 3}
        self.failure = None

    def resolve_model(self, _profile):
        return "gpt-5.4"

    def ensure_worker_ready(self, worker):
        return RuntimeInfo(
            runtime=str(worker.get("profile") or "synthetic"),
            model="gpt-5.4",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir="",
            workspace_dir="",
            pid=1,
        )

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        self.calls.append((worker, instruction, timeout_sec, run_id))
        if self.failure is not None:
            raise self.failure
        return "completed"

    def run_usage(self, _worker, _run_id):
        return dict(self.usage)

    def interrupt_worker(self, worker, run_id=None):
        self.calls.append((worker, "interrupt", None, run_id))
        return "interrupted"


class RecordingBroker:
    def __init__(self):
        self.binds = []
        self.revokes = []

    @contextmanager
    def bind_run(self, **kwargs):
        self.binds.append(kwargs)
        yield {
            "adapter": ADAPTER_ID,
            "grant_token": "ephemeral-grant-never-persist",
            "base_url": "https://librechat.example.test/api/viventium/glasshive/inference/openai/v1",
            "worker_id": kwargs["worker_id"],
            "run_id": kwargs["run_id"],
            "models": kwargs["models"],
            "expires_at": int(__import__("time").time()) + 300,
        }

    def revoke_active(self, **kwargs):
        self.revokes.append(kwargs)


class RevokeFailureAfterDispatchBroker(RecordingBroker):
    @contextmanager
    def bind_run(self, **kwargs):
        with super().bind_run(**kwargs) as projection:
            yield projection
        raise InferenceBrokerError(
            "synthetic broker revocation failure",
            code="revoke_failed",
        )


def test_scheduled_run_issues_at_execution_and_never_persists_grant(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_INFERENCE_BROKER_URL", raising=False)
    db_path = tmp_path / "control-plane.db"
    store = ControlPlaneStore(str(db_path))
    account = store.create_provider_account(
        tenant_id="glass-tenant",
        owner_id="owner-a",
        provider="openai",
        label="Personal OpenAI",
        auth_method="api_key",
        platform_support="supported",
        secret_locator="broker://librechat",
        status="ready",
    )
    profiled = ProfiledWorkerRuntime(
        base_dir=str(tmp_path / "runtime"),
        provider_account_db_path=str(db_path),
    )
    runtime = RecordingRuntime()
    broker = RecordingBroker()
    monkeypatch.setattr(profiled, "_runtime_for_worker", lambda _worker: runtime)
    profiled.inference_broker = broker
    worker = {
        "worker_id": "worker-a",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": {
            "provider_account": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            }
        },
    }

    # Persisting or scheduling the workspace alone has no broker side effect.
    assert broker.binds == []
    assert profiled.run_task(worker, "Scheduled synthetic task", run_id="run-scheduled") == "completed"
    assert broker.binds[0]["run_id"] == "run-scheduled"
    assert "_glasshive_inference_broker" not in worker
    assert "ephemeral-grant-never-persist" not in db_path.read_bytes().decode(
        "utf-8", errors="ignore"
    )
    observed = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="glass-tenant",
        owner_id="owner-a",
    )
    assert observed["observed_runs"] == 1
    assert observed["observed_failures"] == 0
    assert observed["observed_duration_seconds"] >= 0
    assert observed["observed_input_tokens"] == 11
    assert observed["observed_output_tokens"] == 3


def test_brokered_provider_account_records_failed_run_without_inventing_tokens(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_INFERENCE_BROKER_URL", raising=False)
    db_path = tmp_path / "control-plane.db"
    store = ControlPlaneStore(str(db_path))
    account = store.create_provider_account(
        tenant_id="glass-tenant",
        owner_id="owner-a",
        provider="openai",
        label="Personal OpenAI",
        auth_method="api_key",
        platform_support="supported",
        secret_locator="broker://librechat",
        status="ready",
    )
    profiled = ProfiledWorkerRuntime(
        base_dir=str(tmp_path / "runtime"),
        provider_account_db_path=str(db_path),
    )
    runtime = RecordingRuntime()
    runtime.failure = RuntimeErrorBase("synthetic worker failure")
    runtime.usage = {}
    monkeypatch.setattr(profiled, "_runtime_for_worker", lambda _worker: runtime)
    profiled.inference_broker = RecordingBroker()
    worker = {
        "worker_id": "worker-a",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": {
            "provider_account": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            }
        },
    }

    with pytest.raises(RuntimeErrorBase, match="synthetic worker failure"):
        profiled.run_task(worker, "Scheduled synthetic task", run_id="run-failed")

    observed = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="glass-tenant",
        owner_id="owner-a",
    )
    assert observed["observed_runs"] == 1
    assert observed["observed_failures"] == 1
    assert observed["observed_input_tokens"] is None
    assert observed["observed_output_tokens"] is None


def test_preferred_broker_cleanup_failure_never_dispatches_the_mission_twice(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_INFERENCE_BROKER_URL", raising=False)
    db_path = tmp_path / "control-plane.db"
    store = ControlPlaneStore(str(db_path))
    account = store.create_provider_account(
        tenant_id="glass-tenant",
        owner_id="owner-a",
        provider="openai",
        label="Personal OpenAI",
        auth_method="api_key",
        platform_support="supported",
        secret_locator="broker://librechat",
        status="ready",
    )
    profiled = ProfiledWorkerRuntime(
        base_dir=str(tmp_path / "runtime"),
        provider_account_db_path=str(db_path),
    )
    runtime = RecordingRuntime()
    monkeypatch.setattr(profiled, "_runtime_for_worker", lambda _worker: runtime)
    profiled.inference_broker = RevokeFailureAfterDispatchBroker()
    worker = {
        "worker_id": "worker-a",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": {
            "provider_account": {
                "policy": "personal_preferred",
                "account_id": account["account_id"],
            }
        },
    }

    with pytest.raises(InferenceBrokerError, match="revocation failure"):
        profiled.run_task(worker, "Run exactly once", run_id="run-revoke-failed")

    assert len(runtime.calls) == 1
    observed = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="glass-tenant",
        owner_id="owner-a",
    )
    assert observed["status"] == "action_required"
    assert observed["observed_runs"] == 1


def test_native_provider_account_records_usage_only_after_bound_worker_dispatch(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "control-plane.db"
    store = ControlPlaneStore(str(db_path))
    account = store.create_provider_account(
        tenant_id="glass-tenant",
        owner_id="owner-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )
    profiled = ProfiledWorkerRuntime(
        base_dir=str(tmp_path / "runtime"),
        provider_account_db_path=str(db_path),
    )
    runtime = RecordingRuntime()
    monkeypatch.setattr(profiled, "_runtime_for_worker", lambda _worker: runtime)

    @contextmanager
    def bound_account(worker, **_kwargs):
        yield {**worker, "_glasshive_provider_account_bound": True}

    monkeypatch.setattr(profiled.provider_account_binder, "bind", bound_account)
    monkeypatch.setattr(
        profiled.provider_account_binder,
        "mark_active_route_ready",
        lambda *_args, **_kwargs: None,
    )
    worker = {
        "worker_id": "worker-a",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": {
            "provider_account": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            }
        },
    }

    assert profiled._run_task_with_provider_account(
        worker,
        "Native synthetic task",
        timeout_sec=30,
        run_id="run-native",
    ) == "completed"

    observed = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="glass-tenant",
        owner_id="owner-a",
    )
    assert observed["observed_runs"] == 1
    assert observed["observed_failures"] == 0
    assert observed["observed_input_tokens"] == 11
    assert observed["observed_output_tokens"] == 3


def test_legacy_provider_path_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_INFERENCE_BROKER_URL", raising=False)
    profiled = ProfiledWorkerRuntime(base_dir=str(tmp_path / "runtime"))
    runtime = RecordingRuntime()
    broker = RecordingBroker()
    monkeypatch.setattr(profiled, "_runtime_for_worker", lambda _worker: runtime)
    profiled.inference_broker = broker
    worker = {
        "worker_id": "legacy-worker",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "bootstrap_bundle_json": {},
    }

    assert profiled.run_task(worker, "Legacy synthetic task", run_id="run-legacy") == "completed"
    assert broker.binds == []
    assert runtime.calls[0][0] is worker


def test_direct_conversation_never_uses_mission_inference_account(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_INFERENCE_BROKER_URL", raising=False)
    profiled = ProfiledWorkerRuntime(base_dir=str(tmp_path / "runtime"))
    runtime = RecordingRuntime()
    broker = RecordingBroker()
    monkeypatch.setattr(profiled, "_runtime_for_worker", lambda _worker: runtime)
    profiled.inference_broker = broker
    worker = {
        "worker_id": "conversation-worker",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
        "bootstrap_bundle_json": {
            "run_mode": "conversation",
            "provider_account": {
                "policy": "personal_required",
                "account_id": "mission-account-must-be-ignored",
            },
        },
    }

    assert profiled.run_task(worker, "Direct synthetic turn", run_id="conversation-run") == "completed"
    assert broker.binds == []
    assert runtime.calls[0][0] is worker


def test_interrupt_revokes_any_active_run_grant(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_INFERENCE_BROKER_URL", raising=False)
    profiled = ProfiledWorkerRuntime(base_dir=str(tmp_path / "runtime"))
    runtime = RecordingRuntime()
    broker = RecordingBroker()
    monkeypatch.setattr(profiled, "_runtime_for_worker", lambda _worker: runtime)
    profiled.inference_broker = broker
    worker = {
        "worker_id": "worker-a",
        "owner_id": "owner-a",
        "tenant_id": "glass-tenant",
        "profile": "codex-cli",
    }

    assert profiled.interrupt_worker(worker, run_id="run-a") == "interrupted"
    assert broker.revokes == [
        {
            "tenant_id": "glass-tenant",
            "owner_id": "owner-a",
            "worker_id": "worker-a",
            "run_id": "run-a",
        }
    ]
