from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.conversation_provider import (
    GLASSHIVE_MODELS,
    ChatCompletionRequest,
    ChatMessage,
    ConversationProvider,
    StreamingRedactor,
    _harness_auth_configured,
    _developer_instruction_snapshot,
    _legacy_idempotency_keys,
    _history_instruction,
    _native_usage,
    _native_visible_text,
    _normalized_harness_activity,
    _system_snapshot,
    _versioned_idempotency_key,
)
from workers_projects_runtime.openclaw_runtime import (
    RuntimeErrorBase,
    RuntimeInfo,
    StubRuntime,
    notify_runtime_started,
    runtime_start_boundary,
)
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import RunRestorationState, Store

AUTH = {
    "Authorization": "Bearer provider-test-token",
    "X-Viventium-User-Id": "owner-a",
}


def test_conversation_catalog_uses_only_configured_exact_grok_model(monkeypatch):
    from fastapi import HTTPException
    from workers_projects_runtime.native_model_selection import ModelConfigurationRequired
    provider = ConversationProvider.__new__(ConversationProvider)
    monkeypatch.setenv("WPR_MODEL_GROK_BUILD", "grok-exact-synthetic")
    selected = provider._model("grok-build:grok-exact-synthetic")
    assert selected.harness_profile == "grok-build"
    assert selected.native_model == "grok-exact-synthetic"
    assert selected.recommended_effort == "default"
    with pytest.raises(HTTPException):
        provider._model("grok-build:other")
    assert any(item["id"] == selected.id for item in provider.models_payload()["data"])
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD")
    with pytest.raises(ModelConfigurationRequired):
        provider._model(selected.id)

BOOTSTRAP_SIGNATURE_SECRET = "synthetic-bootstrap-signature-secret"

_TEST_CLIENT_LIFESPANS: ExitStack | None = None


@pytest.fixture(autouse=True)
def lifecycle_owned_test_clients():
    global _TEST_CLIENT_LIFESPANS
    assert _TEST_CLIENT_LIFESPANS is None
    baseline_owned_threads = {
        thread.ident
        for thread in threading.enumerate()
        if thread.ident is not None
        and (
            thread.name.startswith("wpr-")
            or thread.name.startswith("glasshive-provider-")
        )
    }
    stack = ExitStack()
    _TEST_CLIENT_LIFESPANS = stack
    try:
        yield
    finally:
        try:
            stack.close()
        finally:
            _TEST_CLIENT_LIFESPANS = None
    leaked_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.ident is not None
        and thread.ident not in baseline_owned_threads
        and (
            thread.name.startswith("wpr-")
            or thread.name.startswith("glasshive-provider-")
        )
    ]
    assert leaked_threads == []


def _lifespan_owned_client(app) -> TestClient:
    if _TEST_CLIENT_LIFESPANS is None:
        raise RuntimeError("Conversation-provider test client has no lifecycle owner")
    return _TEST_CLIENT_LIFESPANS.enter_context(TestClient(app))


def _publish_in_process_test_start(worker: dict) -> None:
    with runtime_start_boundary(worker):
        notify_runtime_started(worker)


def test_system_snapshot_preserves_all_current_request_instruction_messages():
    messages = [
        ChatMessage(role="system", content="Current agent instructions."),
        ChatMessage(role="user", content="Hello."),
        ChatMessage(role="system", content="Current conversation policy."),
    ]

    snapshot = _system_snapshot(messages)

    assert snapshot.count("Current agent instructions.") == 1
    assert snapshot.count("Current conversation policy.") == 1
    assert snapshot.index("Current agent instructions.") < snapshot.index(
        "Current conversation policy."
    )


def test_system_snapshot_deduplicates_identical_instruction_messages():
    messages = [
        ChatMessage(role="system", content="Shared instruction."),
        ChatMessage(role="system", content="Shared instruction."),
    ]

    assert _system_snapshot(messages) == "Shared instruction."


def test_system_snapshot_preserves_openai_developer_instructions():
    messages = [
        ChatMessage(role="developer", content="Application-owned instruction."),
        ChatMessage(role="user", content="Hello."),
    ]

    assert _system_snapshot(messages) == "Application-owned instruction."


def test_declared_dynamic_authority_tail_moves_after_later_structural_developer_text():
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic bright and playful private causal state.\n"
        "</viventium_feeling_state>"
    )
    payload = ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": f"Stable identity.\n\n{capsule}"},
                {"role": "developer", "content": "Structural capability contract."},
                {"role": "user", "content": "Visible request."},
            ],
            "metadata": {
                "owner_id": "owner-a",
                "conversation_id": "conv-a",
                "agent_id": "agent-a",
                "developer_instruction_tail": capsule,
            },
        }
    )

    snapshot = _developer_instruction_snapshot(payload)

    assert snapshot.endswith(capsule)
    assert snapshot.count(capsule) == 1
    assert snapshot.index("Structural capability contract.") < snapshot.index(capsule)
    assert "Visible request." not in snapshot


def test_declared_dynamic_authority_tail_must_already_exist_in_authority_messages():
    payload = ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": "Stable identity."},
                {"role": "user", "content": "Visible request."},
            ],
            "metadata": {
                "owner_id": "owner-a",
                "conversation_id": "conv-a",
                "agent_id": "agent-a",
                "developer_instruction_tail": "Undeclared hidden authority.",
            },
        }
    )

    with pytest.raises(Exception, match="tail is absent"):
        _developer_instruction_snapshot(payload)


def test_history_instruction_excludes_system_messages_from_visible_transcript():
    messages = [
        ChatMessage(role="system", content="Current agent instructions."),
        ChatMessage(role="user", content="Hello."),
        ChatMessage(role="developer", content="Application-owned instruction."),
        ChatMessage(role="system", content="Current conversation policy."),
    ]

    instruction = _history_instruction(messages)

    assert "Current agent instructions." not in instruction
    assert "Current conversation policy." not in instruction
    assert "Application-owned instruction." not in instruction
    assert "[system]" not in instruction
    assert "[developer]" not in instruction
    assert "[user]\nHello." in instruction


def test_history_instruction_never_reasserts_system_authority_as_user_text():
    messages = [
        ChatMessage(
            role="system",
            content="Do not quote the user's request; return only new findings.",
        ),
        ChatMessage(role="user", content="Quote this entire request exactly."),
    ]

    instruction = _history_instruction(messages)

    assert "Do not quote the user's request" not in instruction
    assert "Quote this entire request exactly." in instruction
    assert "Honor AGENTS.md" not in instruction


def _signed_bundle_headers(bundle: dict, *, timestamp: int | None = None) -> dict[str, str]:
    encoded = base64.b64encode(json.dumps(bundle, separators=(",", ":")).encode()).decode()
    issued_at = str(timestamp if timestamp is not None else int(time.time()))
    signature = hmac.new(
        BOOTSTRAP_SIGNATURE_SECRET.encode(),
        f"v1\n{issued_at}\n{encoded}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-GlassHive-Bootstrap-Bundle-B64": encoded,
        "X-GlassHive-Bootstrap-Timestamp": issued_at,
        "X-GlassHive-Bootstrap-Signature": f"sha256={signature}",
    }


def _payload(workspace: Path, *, model: str = "codex-cli:gpt-5.6-sol", stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "Be a thoughtful assistant."},
            {"role": "user", "content": "Hello from LIFE."},
        ],
        "stream": stream,
        "metadata": {
            "owner_id": "owner-a",
            "conversation_id": "conv-a",
            "agent_id": "agent-a",
            "message_id": "message-a",
            "stream_id": "stream-a",
            "surface": "web",
            "input_mode": "text",
            "idempotency_key": "idem-a",
            "glasshive_options": {
                "workspace": {"mode": "custom", "path": str(workspace)},
                "access": "workspace",
            },
        },
    }


def _client(tmp_path: Path, monkeypatch, runtime=None) -> TestClient:
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-admin-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_API_KEY", "provider-test-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_PRINCIPAL_ID", "owner-a")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_TENANT_ID", "local")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_TRUST_IDENTITY_HEADERS", "1")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS", "1")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_ACCESS", "full")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET",
        BOOTSTRAP_SIGNATURE_SECRET,
    )
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS", str(tmp_path))
    return _lifespan_owned_client(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=runtime or StubRuntime(),
        )
    )


def _scoped_client(
    tmp_path: Path,
    monkeypatch,
    runtime=None,
    *,
    trust_identity_headers: bool = False,
    allow_full_access: bool = False,
) -> TestClient:
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-admin-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_API_KEY", "provider-test-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_PRINCIPAL_ID", "owner-a")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_TENANT_ID", "local")
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_TRUST_IDENTITY_HEADERS",
        "1" if trust_identity_headers else "0",
    )
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS",
        "1" if allow_full_access else "0",
    )
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET",
        BOOTSTRAP_SIGNATURE_SECRET,
    )
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    return _lifespan_owned_client(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=runtime or StubRuntime(),
        )
    )


class ActivityStubRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "command_execution", "status": "completed", "exit_code": 0},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "file_change", "status": "completed", "changes": [{}]},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "Activity answer."},
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                ]
            ),
        )


class InterruptCountingRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.interrupt_calls: list[tuple[str, str | None]] = []

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupt_calls.append((str(worker["worker_id"]), run_id))
        return super().interrupt_worker(worker)


class SplitSecretStreamingRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.stdout = ""

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = instruction, timeout_sec, run_id
        _publish_in_process_test_start(worker)
        first = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Before api_key=PUBLIC_FAKE_"}]
            },
        }
        second = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "SECRET_VALUE after\n"}]},
        }
        self.stdout = json.dumps(first)
        time.sleep(0.15)
        self.stdout += "\n" + json.dumps(second)
        time.sleep(0.15)
        self.stdout += "\n" + json.dumps(
            {
                "type": "result",
                "result": "Before api_key=PUBLIC_FAKE_SECRET_VALUE after\n",
            }
        )
        return "Before api_key=PUBLIC_FAKE_SECRET_VALUE after\n"

    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return "claude-code", self.stdout


class NativeUsageRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "Native answer."},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 17, "output_tokens": 4},
                        }
                    ),
                ]
            ),
        )


class NativeToolEvidenceRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "id": "command-1",
                                "command": "check limit",
                                "aggregated_output": (
                                    "Synthetic native-only tool condition: the limit is 731 units."
                                ),
                                "status": "completed",
                                "exit_code": 0,
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "The check is complete.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 17, "output_tokens": 4},
                        }
                    ),
                ]
            ),
        )


class RetainedNativeRunLogs(NativeUsageRuntime):
    """Keep the first run's log available when later native runs complete."""

    def __init__(self, older_log: str):
        super().__init__()
        self.older_log = older_log
        self.logs: dict[str, tuple[str, str]] = {}

    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        if run_id not in self.logs:
            self.logs[run_id] = (
                NativeToolEvidenceRuntime().provider_activity_log(worker, run_id)
                if not self.logs and self.older_log == "tool"
                else super().provider_activity_log(worker, run_id)
            )
        return self.logs[run_id]


class CompactedActivityRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "glasshive.log_compacted",
                            "excluded_prefix_bytes": 2048,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "Tail answer."},
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                ]
            ),
        )


class ProviderRateLimitedRuntime(StubRuntime):
    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = instruction, timeout_sec, run_id
        _publish_in_process_test_start(worker)
        raise RuntimeErrorBase("codex-cli exited after the model provider reached its usage limit")

    def collect_completed_run(
        self,
        worker: dict,
        run_id: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, object]:
        _ = worker, run_id, instruction
        return {
            "state": "failed",
            "output_text": "",
            "error_text": "The model provider reached its usage limit.",
            "failure_class": "provider_rate_limited",
            "failure_retryable": 1,
            "failure_user_message": (
                "The selected model provider rejected the worker turn because its "
                "usage quota or rate limit was reached."
            ),
            "failure_recommended_recovery": (
                "Retry after the provider-reported reset or restore provider quota."
            ),
            "failure_diagnostic_summary": "Provider usage limit reached.",
        }


class StructuredDeliveryRuntime(StubRuntime):
    def __init__(self, *, voice: str | None = "skip"):
        super().__init__()
        self.voice = voice

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = instruction, timeout_sec, run_id
        _publish_in_process_test_start(worker)
        bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
        if "messaging_delivery_control" not in bundle:
            return "Text-only answer."
        payload = {
            "type": "assistant_response",
            "content": "Text-only answer.",
            "tool_name": None,
        }
        if self.voice is not None:
            payload["voice"] = self.voice
        return json.dumps(payload)


def test_models_expose_exact_harness_registry(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/models", headers=AUTH)

    assert response.status_code == 200
    models = {item["id"]: item for item in response.json()["data"]}
    assert set(models) == {
        "codex-cli:gpt-6-astra", "codex-cli:gpt-6-sol", "codex-cli:gpt-6-luna",
        "codex-cli:gpt-5.6-sol", "codex-cli:gpt-5.6-luna", "codex-cli:gpt-5.6-terra",
        "codex-cli:gpt-5.4", "codex-cli:native-default",
        "claude-code:claude-opus-5-5", "claude-code:claude-opus-5", "claude-code:opus",
    }
    assert models["codex-cli:gpt-5.6-sol"]["display_name"] == "Codex / GPT-5.6 Sol"
    assert models["codex-cli:gpt-5.6-sol"]["recommended_effort"] == "medium"
    assert models["codex-cli:gpt-5.6-sol"]["context_window"] == 272000
    assert models["claude-code:opus"]["display_name"] == "Claude / Opus"
    assert models["claude-code:opus"]["recommended_effort"] == "max"
    assert models["claude-code:opus"]["effort_choices"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "default",
    ]
    assert all(model["capabilities"]["activity_stream"] for model in models.values())
    assert all(model["capabilities"]["conversation_session"] for model in models.values())
    assert all(model["capabilities"]["chat_completions"] for model in models.values())
    assert all(model["capabilities"]["responses_api"] for model in models.values())
    assert all(
        model["capabilities"]["messaging_delivery_disposition"]
        for model in models.values()
    )
    assert all(
        model["capabilities"]["messaging_delivery_disposition_version"] == 1
        for model in models.values()
    )
    assert all(model["capabilities"]["voice_pipeline_llm"] for model in models.values())
    assert all(not model["capabilities"]["native_realtime_voice"] for model in models.values())
    assert models["codex-cli:gpt-5.6-sol"]["capabilities"]["incremental_text"] is False
    assert models["claude-code:opus"]["capabilities"]["incremental_text"] is False
    assert all(model["readiness"]["status"] for model in models.values())
    assert all(isinstance(model["created"], int) and model["created"] > 0 for model in models.values())


def test_conversation_provider_durably_binds_trusted_lane_before_host_start(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_payload(workspace),
    )

    assert response.status_code == 200, response.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    worker = client.app.state.store.get_worker(sessions[0]["worker_id"])
    assert worker["trusted_run_lane"] == "conversation"


def test_provider_rate_limit_uses_standard_openai_429_error(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=ProviderRateLimitedRuntime())

    response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_payload(workspace),
    )

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "message": (
                "The selected model provider rejected the worker turn because its "
                "usage quota or rate limit was reached."
            ),
            "type": "rate_limit_error",
            "param": None,
            "code": "rate_limit_exceeded",
        }
    }


def test_audio_eligible_header_requires_and_projects_structured_voice_disposition(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    headers = {
        **AUTH,
        "X-Viventium-Surface": "telegram",
        "X-Viventium-Audio-Eligible": "true",
    }
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime())

    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_payload(workspace),
    )

    assert response.status_code == 200, response.text
    message = response.json()["choices"][0]["message"]
    assert message["content"] == "Text-only answer."
    assert message["provider_specific_fields"] == {
        "viventium": {
            "delivery_disposition": {
                "version": 1,
                "audio": "skip",
                "required": True,
                "valid": True,
                "source": "model",
            }
        }
    }
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    worker = client.app.state.store.get_worker(sessions[0]["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert bundle["messaging_delivery_control"] == {
        "version": 1,
        "audio_eligible": True,
    }


def test_audio_eligibility_header_rejects_ambiguous_values(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime())

    response = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Viventium-Audio-Eligible": "sometimes"},
        json=_payload(workspace),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_provider_session_mode_uses_trusted_header_and_exact_run_record(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)

    body_only = _payload(workspace)
    body_only["metadata"]["provider_session_mode"] = "stateless"
    persistent = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=body_only,
    )
    assert persistent.status_code == 200, persistent.text
    persistent_request = client.app.state.store.get_provider_request(
        persistent.json()["id"]
    )
    assert json.loads(persistent_request["replay_decision_json"])[
        "provider_session_mode"
    ] == "persistent"

    stateless_payload = _payload(workspace)
    stateless_payload["metadata"]["message_id"] = "message-stateless"
    stateless_payload["metadata"]["idempotency_key"] = "idem-stateless"
    stateless = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-GlassHive-Provider-Session-Mode": "stateless"},
        json=stateless_payload,
    )
    assert stateless.status_code == 200, stateless.text
    store = client.app.state.store
    request_record = store.get_provider_request(stateless.json()["id"])
    assert json.loads(request_record["replay_decision_json"])[
        "provider_session_mode"
    ] == "stateless"
    run = store.get_run(request_record["run_id"])
    session = store.get_provider_session_by_id(request_record["session_id"])
    worker = store.get_worker(session["worker_id"])
    store.update_worker(worker["worker_id"], session_key="prior-native-session")
    client.app.state.service._apply_runtime_info(
        worker["worker_id"],
        RuntimeInfo(
            runtime="codex-cli",
            model="gpt-5.6-sol",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir="",
            workspace_dir=str(workspace),
            pid=None,
        ),
        state="ready",
        last_error="",
    )
    assert store.get_worker(worker["worker_id"])["session_key"] == "prior-native-session"
    store.update_worker(
        worker["worker_id"],
        bootstrap_bundle_json=json.dumps(
            {**json.loads(worker["bootstrap_bundle_json"]), "env": {}}
        ),
    )
    projected = client.app.state.service._exact_provider_session_bundle_for_run(
        json.loads(store.get_worker(worker["worker_id"])["bootstrap_bundle_json"]),
        run["run_id"],
    )
    assert projected["env"]["GLASSHIVE_PROVIDER_SESSION_MODE"] == "stateless"


def test_audio_eligibility_is_part_of_idempotent_request_identity(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime())
    payload = _payload(workspace)

    standard = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=payload,
    )
    audio_eligible = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Viventium-Audio-Eligible": "true"},
        json=payload,
    )
    audio_retry = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Viventium-Audio-Eligible": "true"},
        json=payload,
    )

    assert standard.status_code == 200, standard.text
    assert audio_eligible.status_code == 200, audio_eligible.text
    assert audio_retry.status_code == 200, audio_retry.text
    assert standard.json()["id"] != audio_eligible.json()["id"]
    assert audio_retry.json()["id"] == audio_eligible.json()["id"]
    assert "provider_specific_fields" not in standard.json()["choices"][0]["message"]
    disposition = audio_eligible.json()["choices"][0]["message"][
        "provider_specific_fields"
    ]["viventium"]["delivery_disposition"]
    assert disposition["required"] is True
    assert disposition["valid"] is True


def test_versioned_idempotency_namespace_cannot_collide_with_raw_user_suffix(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime())
    standard_payload = _payload(workspace)
    standard_payload["metadata"][
        "idempotency_key"
    ] = "x:delivery:v1:audio-eligible"
    audio_payload = _payload(workspace)
    audio_payload["metadata"]["idempotency_key"] = "x"

    standard = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=standard_payload,
    )
    audio = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Viventium-Audio-Eligible": "true"},
        json=audio_payload,
    )

    assert standard.status_code == 200, standard.text
    assert audio.status_code == 200, audio.text
    assert standard.json()["id"] != audio.json()["id"]
    assert "provider_specific_fields" in audio.json()["choices"][0]["message"]


def test_serial_fallback_headers_arm_the_durable_request(tmp_path, monkeypatch):
    # LibreChat declares the agent's serial fallback through the trusted transport headers. The
    # runtime must validate it once at admission and arm the durable request so the
    # conversation-lane switch can continue the exact turn on the fallback model.
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    armed = client.post(
        "/v1/chat/completions",
        headers={
            **AUTH,
            "X-GlassHive-Fallback-Model": "claude-code:opus",
            "X-GlassHive-Fallback-Reasoning-Effort": "high",
        },
        json=payload,
    )
    assert armed.status_code == 200, armed.text
    record = client.app.state.store.get_provider_request(armed.json()["glasshive"]["request_id"])
    assert record["fallback_model_id"] == "claude-code:opus"
    assert record["fallback_reasoning_effort"] == "high"
    assert str(record["fallback_instruction"]).strip()
    assert record["fallback_state"] == ""

    plain_payload = _payload(workspace)
    plain_payload["metadata"]["idempotency_key"] = "plain-turn"
    plain = client.post("/v1/chat/completions", headers=AUTH, json=plain_payload)
    assert plain.status_code == 200, plain.text
    plain_record = client.app.state.store.get_provider_request(plain.json()["glasshive"]["request_id"])
    assert plain_record["fallback_model_id"] == ""
    assert plain_record["fallback_instruction"] == ""

    same_payload = _payload(workspace)
    same_payload["metadata"]["idempotency_key"] = "same-model-turn"
    rejected = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-GlassHive-Fallback-Model": same_payload["model"]},
        json=same_payload,
    )
    assert rejected.status_code == 400
    assert "must differ from the primary model" in rejected.text


@pytest.mark.parametrize("authoring_scope", [("external_user", "interactive"), ("system", "scheduler")])
def test_structured_quota_failure_continues_the_turn_on_the_serial_fallback(tmp_path, authoring_scope):
    # A primary run that fails with structured provider quota evidence must not fail the request:
    # the armed serial fallback is claimed exactly once and the exact turn continues on a new
    # worker running the fallback model.
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, InterruptCountingRuntime(), reconcile_on_startup=False)
    provider: ConversationProvider | None = None
    try:
        project = store.create_project("owner-a", "Synthetic conversation", "Serial fallback", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-fallback",
            agent_id="agent-fallback",
            actor_kind=authoring_scope[0],
            origin=authoring_scope[1],
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "answer the user", state="queued")
        store.update_run(
            run["run_id"],
            state="failed",
            failure_class="provider_quota_exhausted",
            failure_retryable=1,
            failure_structured=1,
            retry_attempts=0,
            output_text="",
        )
        request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="serial-fallback",
            message_id="message-fallback",
            stream_id="stream-fallback",
            requested_history_count=1,
            fallback_model_id="claude-code:opus",
            fallback_reasoning_effort="high",
            fallback_instruction="answer the user",
        )
        store.update_provider_request(request["request_id"], run_id=run["run_id"], state="running")
        provider = ConversationProvider(store, service)

        synced = provider._sync(store.get_provider_request(request["request_id"]))

        after = store.get_provider_request(request["request_id"])
        activities = [
            (str(item["event_type"]), item.get("payload"))
            for item in store.list_provider_activity(request["request_id"])
        ]
        assert after["fallback_from_run_id"] == run["run_id"], (after, activities)
        assert after["fallback_state"] != "", (after, activities)
        assert after["state"] != "failed", (synced, after, activities)
        assert after["run_id"] != run["run_id"], (after, activities)
        fallback_run = store.get_run(str(after["run_id"]))
        fallback_worker = store.get_worker(str(fallback_run["worker_id"]))
        assert fallback_worker["profile"] == "claude-code", fallback_worker
        assert fallback_worker["worker_id"] != worker["worker_id"]
        current_session = store.get_provider_session_by_id(session["session_id"])
        assert (current_session["actor_kind"], current_session["origin"]) == authoring_scope
        assert current_session["worker_id"] == fallback_worker["worker_id"]
        assert len(store.list_provider_sessions(owner_id="owner-a")) == 1
    finally:
        if provider is not None:
            provider.shutdown()
        service.shutdown()


def test_standard_retry_finds_pre_upgrade_raw_idempotency_record(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)

    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert first.status_code == 200, first.text
    request_id = first.json()["glasshive"]["request_id"]
    with client.app.state.store._connect() as conn:
        conn.execute(
            "UPDATE provider_requests SET idempotency_key = ? WHERE request_id = ?",
            ("idem-a", request_id),
        )

    retry = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == first.json()["id"]
    assert len(client.app.state.store.list_provider_sessions(owner_id="owner-a")) == 1


def test_legacy_graph_idempotency_lookup_preserves_pre_upgrade_digest():
    payload = ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Consult if useful."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lc_transfer_to_specialist",
                        "description": "Consult a specialist.",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "metadata": {
                "owner_id": "owner-a",
                "idempotency_key": "legacy-graph",
            },
        }
    )

    keys = _legacy_idempotency_keys(payload)

    assert len(keys) == 1
    assert keys[0].startswith("legacy-graph:graph:")
    assert not keys[0].startswith("viventium-request:v2:")


def test_audio_eligible_stream_preserves_text_and_fails_audio_closed_when_disposition_is_missing(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    payload = _payload(workspace, stream=True)
    client = _client(
        tmp_path,
        monkeypatch,
        runtime=StructuredDeliveryRuntime(voice=None),
    )

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={
            **AUTH,
            "X-Viventium-Surface": "telegram",
            "X-Viventium-Audio-Eligible": "true",
        },
        json=payload,
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: {")
    ]
    visible = "".join(
        choice.get("delta", {}).get("content", "")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )
    disposition = next(
        choice["delta"]["provider_specific_fields"]["viventium"][
            "delivery_disposition"
        ]
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if "delivery_disposition"
        in choice.get("delta", {}).get("provider_specific_fields", {}).get("viventium", {})
    )
    assert visible == "Text-only answer."
    assert disposition == {
        "version": 1,
        "audio": "skip",
        "required": True,
        "valid": False,
        "source": "required_missing",
    }


def test_audio_eligible_stream_emits_structured_disposition_outside_visible_text(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime())

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={
            **AUTH,
            "X-Viventium-Surface": "telegram",
            "X-Viventium-Audio-Eligible": "true",
        },
        json=_payload(workspace, stream=True),
    ) as response:
        lines = [line for line in response.iter_lines() if line and line != "data: [DONE]"]

    assert response.status_code == 200
    chunks = [json.loads(line.removeprefix("data: ")) for line in lines]
    visible = "".join(
        choice.get("delta", {}).get("content", "")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )
    disposition = next(
        choice["delta"]["provider_specific_fields"]["viventium"][
            "delivery_disposition"
        ]
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if "delivery_disposition"
        in choice.get("delta", {}).get("provider_specific_fields", {}).get("viventium", {})
    )
    assert visible == "Text-only answer."
    assert "{SKIP_VOICE}" not in visible
    assert disposition == {
        "version": 1,
        "audio": "skip",
        "required": True,
        "valid": True,
        "source": "model",
    }


def test_provider_rate_limit_stream_uses_standard_error_type_and_code(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=ProviderRateLimitedRuntime())
    payload = _payload(workspace, stream=True)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=AUTH,
        json=payload,
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type":"rate_limit_error"' in body
    assert '"code":"rate_limit_exceeded"' in body
    assert body.count('"error"') == 1


def test_responses_api_preserves_rate_limit_status_and_stream_code(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(workspace))
    client = _client(tmp_path, monkeypatch, runtime=ProviderRateLimitedRuntime())
    request = {
        "model": "codex-cli:gpt-5.6-sol",
        "input": "Hello from the Responses API.",
    }

    response = client.post("/v1/responses", headers=AUTH, json=request)

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert response.json()["error"]["code"] == "rate_limit_exceeded"

    stream_request = {**request, "stream": True}
    with client.stream(
        "POST",
        "/v1/responses",
        headers={**AUTH, "X-GlassHive-Idempotency-Key": "responses-rate-stream"},
        json=stream_request,
    ) as stream:
        body = "".join(stream.iter_text())

    assert stream.status_code == 200
    assert '"type":"error"' in body
    assert '"code":"rate_limit_exceeded"' in body
    assert '"type":"response.failed"' in body


def test_provider_credential_is_scoped_and_standard_request_needs_no_viventium_headers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "mcp-test-token")
    client = _scoped_client(tmp_path, monkeypatch)

    models = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer provider-test-token"},
    )
    admin_on_provider = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer runtime-admin-token"},
    )
    mcp_on_provider = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer mcp-test-token"},
    )
    provider_on_runtime = client.get(
        "/v1/projects",
        headers={"Authorization": "Bearer provider-test-token"},
    )
    completion = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Portable hello."}],
        },
    )

    assert models.status_code == 200
    assert admin_on_provider.status_code == 401
    assert mcp_on_provider.status_code == 401
    assert mcp_on_provider.json()["error"]["code"] == "invalid_api_key"
    assert provider_on_runtime.status_code == 401
    assert completion.status_code == 200, completion.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    assert sessions[0]["tenant_id"] == "local"
    assert sessions[0]["workspace_dir"] == str(tmp_path.resolve())
    assert sessions[0]["access_mode"] == "workspace"


def test_standard_responses_request_uses_the_same_provider_core(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "instructions": "Be concise.",
            "input": "Portable Responses hello.",
            "reasoning": {"effort": "medium"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"].startswith("resp_")
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == "codex-cli:gpt-5.6-sol"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["role"] == "assistant"
    assert body["output"][0]["content"][0]["type"] == "output_text"
    assert "Portable Responses hello." in body["output_text"]
    assert body["usage"]["total_tokens"] > 0
    assert body["glasshive"]["activity_url"].endswith("/activity")
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    request = client.app.state.store.get_provider_request(body["glasshive"]["request_id"])
    assert request["session_id"] == sessions[0]["session_id"]


@pytest.mark.parametrize("stream", [False, True])
def test_responses_rejects_chat_completions_only_audio_eligibility_extension(
    tmp_path, monkeypatch, stream
):
    client = _scoped_client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/responses",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-Audio-Eligible": "true",
        },
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Portable Responses hello.",
            "stream": stream,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "chat/completions" in response.json()["error"]["message"]


@pytest.mark.parametrize("header_value", ["false", "0"])
def test_responses_accepts_explicit_audio_ineligibility_opt_out(
    tmp_path, monkeypatch, header_value
):
    client = _scoped_client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/responses",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-Audio-Eligible": header_value,
        },
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Portable Responses opt-out.",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


def test_responses_previous_response_id_reuses_only_the_authenticated_session(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    first = client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": [
                {"role": "developer", "content": "Answer naturally."},
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "First turn."}],
                },
            ],
        },
    )
    assert first.status_code == 200, first.text

    follow_up = client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Second turn.",
            "previous_response_id": first.json()["id"],
        },
    )
    cross_owner = client.post(
        "/v1/responses",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-User-Id": "owner-b",
        },
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Attempt cross-owner resume.",
            "previous_response_id": first.json()["id"],
        },
    )

    assert follow_up.status_code == 200, follow_up.text
    first_request = client.app.state.store.get_provider_request(
        first.json()["glasshive"]["request_id"]
    )
    second_request = client.app.state.store.get_provider_request(
        follow_up.json()["glasshive"]["request_id"]
    )
    assert first_request["session_id"] == second_request["session_id"]
    assert follow_up.json()["previous_response_id"] == first.json()["id"]
    assert cross_owner.status_code == 403
    assert cross_owner.json()["error"]["code"] == "permission_denied"


def test_responses_stream_emits_typed_monotonic_events(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    with client.stream(
        "POST",
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Portable Responses stream.",
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]

    events = []
    current_event = ""
    for line in lines:
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
            assert payload["type"] == current_event
            events.append(payload)
    event_types = [event["type"] for event in events]
    assert event_types[0] == "response.created"
    assert "response.output_text.delta" in event_types
    assert event_types[-1] == "response.completed"
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert events[-1]["response"]["status"] == "completed"
    assert "Portable Responses stream." in events[-1]["response"]["output_text"]


def test_responses_unsupported_or_non_text_shapes_fail_visibly(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    unsupported_tools = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Hello.",
            "tools": [{"type": "function", "name": "wrapper_tool"}],
        },
    )
    unsupported_item = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": [{"type": "computer_call_output", "call_id": "call-1", "output": "x"}],
        },
    )

    assert unsupported_tools.status_code == 400
    assert unsupported_tools.json()["error"]["code"] == "unsupported_parameter"
    assert unsupported_tools.json()["error"]["param"] == "tools"
    assert unsupported_item.status_code == 400
    assert unsupported_item.json()["error"]["code"] == "invalid_request"


def test_standard_stream_options_and_user_are_openai_compatible(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Portable stream."}],
            "stream": True,
            "stream_options": {"include_usage": True},
            "user": "portable-client-user",
        },
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in lines
        if line != "data: [DONE]"
    ]
    assert chunks[-1]["usage"]["total_tokens"] > 0
    assert chunks[-1]["choices"] == []
    assert lines[-1] == "data: [DONE]"


def test_identity_delegation_and_full_access_require_server_side_grants(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload["metadata"]["owner_id"] = "owner-b"

    impersonation = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-User-Id": "owner-b",
        },
        json=payload,
    )

    payload = _payload(tmp_path)
    payload["metadata"]["glasshive_options"]["access"] = "full"
    full_access = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json=payload,
    )

    assert impersonation.status_code == 403
    assert full_access.status_code == 403
    assert impersonation.json()["error"]["code"] == "permission_denied"
    assert full_access.json()["error"]["code"] == "permission_denied"


def test_trusted_service_credential_can_delegate_owner_and_full_access(tmp_path, monkeypatch):
    client = _scoped_client(
        tmp_path,
        monkeypatch,
        trust_identity_headers=True,
        allow_full_access=True,
    )
    payload = _payload(tmp_path)
    payload["metadata"]["owner_id"] = "owner-b"
    payload["metadata"]["glasshive_options"]["access"] = "full"

    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-User-Id": "owner-b",
        },
        json=payload,
    )

    assert response.status_code == 200, response.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-b")
    assert len(sessions) == 1
    assert sessions[0]["access_mode"] == "full"


def test_standard_ignored_parameters_and_unsupported_shapes_use_openai_error_envelope(
    tmp_path, monkeypatch
):
    client = _scoped_client(tmp_path, monkeypatch)

    tolerated = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Hello."}],
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 100,
            "presence_penalty": 0,
            "frequency_penalty": 0,
            "seed": 7,
            "store": False,
        },
    )
    unsupported = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Hello."}],
            "tools": [{"type": "function", "function": {"name": "unsafe_shape"}}],
        },
    )
    invalid = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={"model": "codex-cli:gpt-5.6-sol", "messages": []},
    )

    assert tolerated.status_code == 200, tolerated.text
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["type"] == "invalid_request_error"
    assert unsupported.json()["error"]["code"] == "unsupported_parameter"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["type"] == "invalid_request_error"
    assert invalid.json()["error"]["code"] == "invalid_request"


def test_identical_requests_without_explicit_idempotency_start_distinct_runs(
    tmp_path, monkeypatch
):
    client = _scoped_client(tmp_path, monkeypatch)
    request = {
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Run this twice."}],
    }
    headers = {"Authorization": "Bearer provider-test-token"}

    first = client.post("/v1/chat/completions", headers=headers, json=request)
    second = client.post("/v1/chat/completions", headers=headers, json=request)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] != second.json()["id"]


def test_readiness_does_not_treat_placeholder_provider_tokens_as_authentication(
    tmp_path, monkeypatch
):
    fake_binary = tmp_path / "harness"
    fake_binary.write_text("#!/bin/sh\nexit 1\n")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("WPR_CODEX_BIN", str(fake_binary))
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_binary))
    monkeypatch.setenv("OPENAI_API_KEY", "user_provided")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "${CLAUDE_CODE_OAUTH_TOKEN}")
    monkeypatch.setattr(
        "workers_projects_runtime.conversation_provider.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )

    assert _harness_auth_configured("codex-cli") is False
    assert _harness_auth_configured("claude-code") is False


def test_non_streaming_completion_reuses_one_native_session_and_idempotency(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)

    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    duplicate = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == first.json()["id"]
    content = first.json()["choices"][0]["message"]["content"]
    assert "Hello from LIFE." in content
    assert "FINAL REPORT" not in content
    store = client.app.state.store
    sessions = store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    assert sessions[0]["model_id"] == "codex-cli:gpt-5.6-sol"
    worker = store.get_worker(sessions[0]["worker_id"])
    assert worker is not None
    assert worker["workspace_dir"] == str(workspace.resolve())
    assert json.loads(worker["bootstrap_bundle_json"])["run_mode"] == "conversation"
    assert len(store.list_runs_for_worker(worker["worker_id"])) == 1


def test_provider_header_pins_dynamic_authority_after_bootstrap_instructions(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic bright and playful private causal state.\n"
        "</viventium_feeling_state>"
    )
    payload = _payload(workspace)
    payload["messages"] = [
        {"role": "system", "content": f"Stable identity.\n\n{capsule}"},
        {"role": "user", "content": "Visible request."},
    ]
    headers = {
        **AUTH,
        **_signed_bundle_headers(
            {"codex_md": "Structural capability broker contract."}
        ),
        "X-GlassHive-Developer-Instruction-Tail-B64": base64.b64encode(
            capsule.encode("utf-8")
        ).decode("ascii"),
    }

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    developer_instructions = json.loads(worker["bootstrap_bundle_json"])[
        "developer_instructions"
    ]
    assert developer_instructions.endswith(capsule)
    assert developer_instructions.count(capsule) == 1
    assert developer_instructions.index(
        "Structural capability broker contract."
    ) < developer_instructions.index(capsule)


def test_model_change_supersedes_native_session_and_seeds_visible_history(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    second_payload = _payload(workspace, model="claude-code:opus")
    second_payload["metadata"]["message_id"] = "message-b"
    second_payload["metadata"]["idempotency_key"] = "idem-b"
    second_payload["messages"].extend(
        [
            {"role": "assistant", "content": "Earlier answer."},
            {"role": "user", "content": "Please correct it."},
        ]
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["model_id"] == "claude-code:opus"
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"
    assert "Earlier answer." in second.json()["choices"][0]["message"]["content"]
    assert "Please correct it." in second.json()["choices"][0]["message"]["content"]


def test_system_state_change_supersedes_session_and_uses_native_developer_authority(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first_payload["messages"][0]["content"] = "Quiet Feeling capsule."
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    first_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert first_worker is not None
    assert json.loads(first_worker["bootstrap_bundle_json"])["developer_instructions"] == (
        "Quiet Feeling capsule."
    )

    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-feeling-change"
    second_payload["metadata"]["idempotency_key"] = "idem-feeling-change"
    second_payload["messages"] = [
        {"role": "system", "content": "Joyful Feeling capsule."},
        {"role": "user", "content": "Hello from LIFE."},
        {"role": "assistant", "content": "Earlier visible answer."},
        {"role": "user", "content": "Continue with the current state."},
    ]
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"
    current_worker = client.app.state.store.get_worker(current["worker_id"])
    assert current_worker is not None
    current_bundle = json.loads(current_worker["bootstrap_bundle_json"])
    assert current_bundle["developer_instructions"] == "Joyful Feeling capsule."
    content = second.json()["choices"][0]["message"]["content"]
    assert "Earlier visible answer." in content
    assert "Continue with the current state." in content
    assert "Quiet Feeling capsule." not in content
    assert "Joyful Feeling capsule." not in content


def test_native_policy_change_supersedes_contaminated_session(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", raising=False)
    monkeypatch.delenv("WPR_HOST_PLUGIN_DENYLIST", raising=False)
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "inherit")
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first = client.post(
        "/v1/chat/completions", headers=AUTH, json=_payload(workspace)
    )
    assert first.status_code == 200, first.text
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    monkeypatch.setenv(
        "GLASSHIVE_HOST_PLUGIN_DENYLIST",
        "synthetic-policy@project-viventium",
    )
    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-policy-change"
    second_payload["metadata"]["idempotency_key"] = "idem-policy-change"
    second = client.post(
        "/v1/chat/completions", headers=AUTH, json=second_payload
    )

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"


def test_phase_b_style_short_prompt_reuses_session_without_losing_visible_history_count(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first_payload["messages"].extend(
        [
            {"role": "assistant", "content": "Initial answer."},
            {"role": "user", "content": "One correction."},
        ]
    )
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    initial_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert initial_session["history_count"] == 5

    follow_up = _payload(workspace)
    follow_up["metadata"]["message_id"] = "message-phase-b"
    follow_up["metadata"]["idempotency_key"] = "idem-phase-b"
    follow_up["messages"] = [{"role": "user", "content": "Synthesize the cortex insight now."}]
    response = client.post("/v1/chat/completions", headers=AUTH, json=follow_up)

    assert response.status_code == 200, response.text
    assert "Synthesize the cortex insight now." in response.json()["choices"][0]["message"]["content"]
    current_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current_session["session_id"] == initial_session["session_id"]
    assert current_session["worker_id"] == initial_session["worker_id"]
    assert current_session["history_count"] == 5
    worker = client.app.state.store.get_worker(current_session["worker_id"])
    assert worker is not None
    assert json.loads(worker["bootstrap_bundle_json"])["developer_instructions"] == (
        "Be a thoughtful assistant."
    )


def test_normal_resumed_turn_sends_only_new_visible_messages_to_native_session(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-normal-follow-up"
    second_payload["metadata"]["idempotency_key"] = "idem-normal-follow-up"
    second_payload["messages"] = [
        {"role": "system", "content": "Be a thoughtful assistant."},
        first_payload["messages"][1],
        {"role": "assistant", "content": "Prior assistant answer."},
        {"role": "user", "content": "Only this correction is new."},
    ]

    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200
    content = second.json()["choices"][0]["message"]["content"]
    assert "Be a thoughtful assistant." not in content
    assert "Only this correction is new." in content
    assert "Prior assistant answer." not in content
    assert "Hello from LIFE." not in content
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] == first_session["worker_id"]


def test_effort_change_updates_the_existing_native_session_without_replacing_it(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    second_payload = _payload(workspace)
    second_payload["reasoning_effort"] = "high"
    second_payload["metadata"]["message_id"] = "message-effort-change"
    second_payload["metadata"]["idempotency_key"] = "idem-effort-change"
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] == first_session["worker_id"]
    manifest = json.loads(current["context_manifest_json"])
    assert manifest["effort"] == "high"


def test_all_declared_codex_efforts_validate_without_replacing_the_session(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    worker_ids = []

    for index, effort in enumerate(("low", "medium", "high", "xhigh", "max", "ultra")):
        payload = _payload(workspace)
        payload["reasoning_effort"] = effort
        payload["metadata"]["message_id"] = f"message-effort-{index}"
        payload["metadata"]["idempotency_key"] = f"idem-effort-{index}"
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200, response.text
        worker_ids.append(client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]["worker_id"])

    assert len(set(worker_ids)) == 1


def test_failed_native_worker_is_replaced_before_the_next_turn(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    assert first.status_code == 200
    store = client.app.state.store
    first_session = store.list_provider_sessions(owner_id="owner-a")[0]
    store.update_worker_state(first_session["worker_id"], "failed", last_error="synthetic crash")

    follow_up = _payload(workspace)
    follow_up["metadata"]["message_id"] = "message-after-crash"
    follow_up["metadata"]["idempotency_key"] = "idem-after-crash"
    second = client.post("/v1/chat/completions", headers=AUTH, json=follow_up)

    assert second.status_code == 200, second.text
    current = store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]


def test_authenticated_broker_bundle_is_forwarded_and_conversation_policy_is_forced(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    broker_bundle = {
        "codex_config_append": "[mcp_servers.synthetic]",
        "env": {"SYNTHETIC_BROKER_TOKEN": "test-only"},
        "run_mode": "mission",
        "provider_capabilities": {"self_delegation": True},
    }
    headers = {
        **AUTH,
        **_signed_bundle_headers(broker_bundle),
    }

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    persisted = json.loads(worker["bootstrap_bundle_json"])
    assert persisted["codex_config_append"] == "[mcp_servers.synthetic]"
    assert persisted["env"]["SYNTHETIC_BROKER_TOKEN"] == "test-only"
    assert persisted["run_mode"] == "conversation"
    assert persisted["provider_capabilities"] == {
        "self_delegation": False,
        "native_tools": True,
    }


@pytest.mark.parametrize("foreign", [False, True])
def test_native_current_attachment_uses_owner_file_bytes(tmp_path, monkeypatch, foreign):
    from workers_projects_runtime.bootstrap import _write_project_files
    import shutil

    workspace = tmp_path / "Life"
    workspace.mkdir()
    uploads = tmp_path / "uploads"
    owner_dir = uploads / ("owner-b" if foreign else "owner-a")
    owner_dir.mkdir(parents=True)
    original = owner_dir / "audio-current__request.m4a"
    original.write_bytes(b"synthetic-original-audio-bytes")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads))
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["messages"][-1]["content"] = ""
    ledger = {"viventium_upload_context": {"selected_uploads": [
        {"file_id": "audio-current", "filename": "request.m4a", "type": "audio/mp4",
         "source": "local", "bytes": original.stat().st_size},
    ]}}
    response = client.post("/v1/chat/completions", headers={**AUTH, **_signed_bundle_headers(ledger)}, json=payload)
    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert len(bundle.get("files", [])) == 1
    record = client.app.state.store.get_provider_request(response.json()["id"])
    current = record["admitted_instruction"].split("<viventium_current_accepted_turn_v1>")[1]
    assert bundle["files"][0]["path"] in current.split("</viventium_current_accepted_turn_v1>")[0]
    home = tmp_path / "worker-home"
    home.mkdir()
    _write_project_files(home, workspace, bundle, worker, shutil.copyfile, shutil.copytree)
    materialized = workspace / bundle["files"][0]["path"]
    if foreign:
        assert "source_path" not in bundle["files"][0]
        assert b"synthetic-original-audio-bytes" not in materialized.read_bytes()
        assert "original_bytes_unavailable" in materialized.read_text()
    else:
        assert materialized.read_bytes() == original.read_bytes()


@pytest.mark.parametrize(
    ("model", "profile_instruction_key"),
    [
        ("codex-cli:gpt-5.6-sol", "codex_md"),
        ("claude-code:opus", "claude_md"),
    ],
)
def test_authenticated_broker_instructions_reach_native_developer_authority(
    tmp_path, monkeypatch, model, profile_instruction_key
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace, model=model)
    broker_instruction = (
        "Use the signed host capability broker for connected-account facts. "
        "If it reports unavailable authentication, preserve its supported recovery action."
    )
    broker_bundle = {
        "agents_md": "Generic broker instruction.",
        profile_instruction_key: broker_instruction,
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1.invalid/mcp"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-test-grant"},
    }

    response = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **_signed_bundle_headers(broker_bundle)},
        json=payload,
    )

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    persisted = json.loads(worker["bootstrap_bundle_json"])
    assert persisted["developer_instructions"] == (
        "Be a thoughtful assistant.\n\n" + broker_instruction
    )
    assert (workspace / "AGENTS.md").exists() is False
    assert (workspace / "CLAUDE.md").exists() is False
    assert (workspace / "CODEX.md").exists() is False


def test_existing_conversation_session_refreshes_and_retracts_broker_bundle(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first_bundle = {
        "codex_config_append": "[mcp_servers.first]\nurl = 'http://first.invalid/mcp'",
        "env": {"FIRST_GRANT": "synthetic-first"},
        "codex_md": "Use the first signed broker contract.",
    }
    first = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **_signed_bundle_headers(first_bundle)},
        json=first_payload,
    )
    assert first.status_code == 200, first.text
    store = client.app.state.store
    session = store.list_provider_sessions(owner_id="owner-a")[0]
    worker_id = session["worker_id"]

    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-refresh-broker"
    second_payload["metadata"]["idempotency_key"] = "idem-refresh-broker"
    second_bundle = {
        "codex_config_append": "[mcp_servers.second]\nurl = 'http://second.invalid/mcp'",
        "env": {"SECOND_GRANT": "synthetic-second"},
        "codex_md": "Use the refreshed signed broker contract.",
    }
    second = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **_signed_bundle_headers(second_bundle)},
        json=second_payload,
    )
    assert second.status_code == 200, second.text

    refreshed_session = store.list_provider_sessions(owner_id="owner-a")[0]
    refreshed_worker = store.get_worker(refreshed_session["worker_id"])
    persisted = json.loads(refreshed_worker["bootstrap_bundle_json"])
    assert refreshed_session["worker_id"] == worker_id
    assert "mcp_servers.second" in persisted["codex_config_append"]
    assert "mcp_servers.first" not in persisted["codex_config_append"]
    assert persisted["env"]["SECOND_GRANT"] == "synthetic-second"
    assert "FIRST_GRANT" not in persisted["env"]
    assert "Use the refreshed signed broker contract." in persisted["developer_instructions"]
    assert "Use the first signed broker contract." not in persisted["developer_instructions"]


def test_bootstrap_bundle_requires_a_fresh_valid_service_signature(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    bundle = {"env": {"SYNTHETIC_BROKER_TOKEN": "test-only"}}
    encoded = base64.b64encode(json.dumps(bundle).encode()).decode()

    unsigned = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-GlassHive-Bootstrap-Bundle-B64": encoded},
        json=payload,
    )
    invalid = client.post(
        "/v1/chat/completions",
        headers={
            **AUTH,
            **_signed_bundle_headers(bundle),
            "X-GlassHive-Bootstrap-Signature": "sha256=invalid",
        },
        json=payload,
    )
    stale = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **_signed_bundle_headers(bundle, timestamp=int(time.time()) - 601)},
        json=payload,
    )

    assert unsigned.status_code == 403
    assert invalid.status_code == 403
    assert stale.status_code == 403
    assert unsigned.json()["error"]["code"] == "permission_denied"


def test_bootstrap_bundle_fails_closed_when_signature_verification_is_unconfigured(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    headers = {**AUTH, **_signed_bundle_headers({"env": {"SYNTHETIC": "value"}})}
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET")

    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_payload(workspace),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_streaming_completion_and_activity_recovery(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace, stream=True)

    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json=payload) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]

    chunks = [json.loads(line.removeprefix("data: ")) for line in lines if line != "data: [DONE]"]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    reasoning = [
        chunk["choices"][0]["delta"]["reasoning_content"]
        for chunk in chunks
        if "reasoning_content" in chunk["choices"][0]["delta"]
    ]
    assert reasoning[0] == "The harness started working.\n"
    assert all("queued" not in item.lower() and "waiting" not in item.lower() for item in reasoning)
    assert all("content" not in chunk["choices"][0]["delta"] for chunk in chunks if "reasoning_content" in chunk["choices"][0]["delta"])
    request_id = chunks[0]["id"]
    assert any("Hello from LIFE." in chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    assert lines[-1] == "data: [DONE]"

    activity = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH)
    assert activity.status_code == 200
    events = activity.json()["data"]
    assert [event["event"] for event in events][:2] == ["queued", "started"]
    assert events[-1]["event"] == "completed"
    after_first = client.get(
        f"/v1/requests/{request_id}/activity",
        headers={**AUTH, "Last-Event-ID": str(events[0]["id"])},
    )
    assert all(event["id"] > events[0]["id"] for event in after_first.json()["data"])


def test_non_streaming_completion_uses_native_usage_when_the_harness_reports_it(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())

    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "Native answer."
    assert response.json()["usage"] == {
        "prompt_tokens": 17,
        "completion_tokens": 4,
        "total_tokens": 21,
    }
    assert response.json()["glasshive"]["usage_source"] == "native"
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    manifest = json.loads(session["context_manifest_json"])
    assert manifest["latest_native_prompt_tokens"] == 17
    assert manifest["usage_calibration_request_ids"] == [response.json()["id"]]
    assert manifest["usage_calibration_scope"] == "admitted_instruction_bytes/native_prompt_tokens"
    assert client.app.state.conversation_provider._native_tool_evidence_coverage_proven(
        session, manifest
    ) is True
    def unavailable_history(*args, **kwargs):
        raise OSError("synthetic history unavailable")
    monkeypatch.setattr(
        client.app.state.store,
        "list_provider_session_requests_for_native_coverage",
        unavailable_history,
    )
    assert client.app.state.conversation_provider._native_tool_evidence_coverage_proven(
        session, {**manifest, "accepted_advancement_keys": ["earlier-turn:1"]}
    ) is False


@pytest.mark.parametrize("replay", ["complete", "missing", "changed"])
def test_ordinary_provider_pressure_uses_durable_visible_content_proof(
    tmp_path, monkeypatch, replay
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    first_record = store.get_provider_request(first.json()["id"])
    session = store.get_provider_session_by_id(first_record["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    assert manifest["accepted_visible_message_keys"]
    store.update_provider_session_history(
        session["session_id"],
        history_count=session["history_count"],
        context_manifest={
            **manifest,
            "latest_native_prompt_tokens": 180000,
            "latest_native_total_tokens": 180020,
            "observed_chars_per_token": 4.0,
            "usage_calibration_scope": "admitted_instruction_bytes/native_prompt_tokens",
        },
    )
    second_payload = _payload(workspace)
    old_user = first_payload["messages"][1]
    if replay == "changed":
        old_user = {"role": "user", "content": "Changed prior user request."}
    second_payload["messages"] = [
        second_payload["messages"][0],
        *([old_user] if replay != "missing" else []),
        {"role": "assistant", "content": first.json()["choices"][0]["message"]["content"]},
        {"role": "user", "content": "Continue with the measured context boundary."},
    ]
    second_payload["metadata"].update({
        "message_id": "message-b", "stream_id": "stream-b", "idempotency_key": "idem-b",
    })
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)
    assert second.status_code == 200, second.text
    decision = json.loads(store.get_provider_request(second.json()["id"])["replay_decision_json"])
    assert decision["native_occupancy"]["state"] == "pressure"
    assert decision["native_tool_evidence_coverage"] == "complete"
    if replay == "complete":
        assert decision["native_source_coverage"] == "complete"
        assert decision["native_context_transition"]["reason"] == "native_occupancy_pressure"
    else:
        assert decision["native_source_coverage"] == "unproven"
        assert decision["native_context_pressure_deferred"] is True
        assert decision["native_context_transition"] == {}


@pytest.mark.parametrize("older_log", ["plain", "tool", "missing"])
def test_ordinary_pressure_checks_every_retained_native_run(
    tmp_path, monkeypatch, older_log
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = RetainedNativeRunLogs(older_log)
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    first_record = store.get_provider_request(first.json()["id"])
    payload["messages"].extend([
        first.json()["choices"][0]["message"],
        {"role": "user", "content": "Acknowledge and continue."},
    ])
    payload["metadata"].update(
        message_id="second", stream_id="second", idempotency_key="second"
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert second.status_code == 200, second.text
    second_record = store.get_provider_request(second.json()["id"])
    assert first_record["session_id"] == second_record["session_id"]
    session = store.get_provider_session_by_id(first_record["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    assert not manifest.get("accepted_advancement_keys")
    store.update_provider_session_history(
        session["session_id"],
        history_count=session["history_count"],
        context_manifest={
            **manifest,
            "latest_native_prompt_tokens": 180000,
            "latest_native_total_tokens": 180020,
            "observed_chars_per_token": 4.0,
        },
    )
    if older_log == "missing":
        runtime.logs[first_record["run_id"]] = ("codex-cli", "")
    payload["messages"].extend([
        second.json()["choices"][0]["message"],
        {"role": "user", "content": "Apply the original condition."},
    ])
    payload["metadata"].update(
        message_id="third", stream_id="third", idempotency_key="third"
    )
    third = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert third.status_code == 200, third.text
    decision = json.loads(
        store.get_provider_request(third.json()["id"])["replay_decision_json"]
    )
    assert decision["native_occupancy"]["state"] == "pressure"
    if older_log == "plain":
        assert decision["native_tool_evidence_coverage"] == "complete"
        assert decision["native_context_transition"]["reason"] == "native_occupancy_pressure"
    else:
        assert decision["native_tool_evidence_coverage"] == "unproven"
        assert decision["native_context_pressure_deferred"] is True
        assert decision["native_context_transition"] == {}


def test_ordinary_visible_key_binds_structured_tool_call_content():
    from workers_projects_runtime.conversation_provider import _visible_message_keys

    original = ChatMessage(role="assistant", content="Checked.", tool_calls=[{
        "id": "call-one", "type": "function",
        "function": {"name": "measure", "arguments": '{"limit":731}'},
    }])
    changed = original.model_copy(update={"tool_calls": [{
        "id": "call-one", "type": "function",
        "function": {"name": "measure", "arguments": '{"limit":999}'},
    }]})
    before = _visible_message_keys([original], [])
    after = _visible_message_keys([changed], [])
    assert not ConversationProvider._native_source_coverage_proven(
        {"accepted_visible_message_keys": list(before.values())}, {}, after,
        new_native_session=False,
    )


@pytest.mark.parametrize("changed_field,replacement", [
    ("call_id", "call-two"),
    ("name", "unrelated-source"),
])
def test_ordinary_visible_key_binds_tool_result_selector(changed_field, replacement):
    from workers_projects_runtime.conversation_provider import _visible_message_keys

    original = ChatMessage(
        role="tool", content="Limit is 731.", call_id="call-one", name="measure"
    )
    changed = original.model_copy(update={changed_field: replacement})
    before = _visible_message_keys([original], [])
    after = _visible_message_keys([changed], [])
    assert not ConversationProvider._native_source_coverage_proven(
        {"accepted_visible_message_keys": list(before.values())}, {}, after,
        new_native_session=False,
    )


def test_foreground_bundle_resolves_selected_account_for_authenticated_owner(
    tmp_path, monkeypatch
):
    from fastapi import HTTPException

    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    control_plane = client.app.state.control_plane
    owned = control_plane.create_provider_account(
        tenant_id="local", owner_id="owner-a", provider="codex",
        label="Synthetic owned account", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://synthetic-owned",
        status="ready",
    )
    foreign = control_plane.create_provider_account(
        tenant_id="local", owner_id="owner-b", provider="codex",
        label="Synthetic foreign account", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://synthetic-foreign",
        status="ready",
    )
    provider = client.app.state.conversation_provider
    payload = _payload(workspace)
    payload["metadata"]["bootstrap_bundle"] = {"connection_id": owned["account_id"]}
    parsed = ChatCompletionRequest.model_validate(payload)
    bundle = provider._native_bundle(parsed, GLASSHIVE_MODELS[parsed.model], "high")
    assert bundle["provider_account"] == {
        "policy": "personal_required", "account_id": owned["account_id"]
    }
    assert bundle["run_mode"] == "conversation"
    payload["metadata"]["bootstrap_bundle"] = {"connection_id": foreign["account_id"]}
    with pytest.raises(HTTPException) as error:
        provider._native_bundle(
            ChatCompletionRequest.model_validate(payload),
            GLASSHIVE_MODELS[payload["model"]], "high",
        )
    assert error.value.status_code == 409


def test_foreground_selected_account_keeps_isolated_origin_placement(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    from fastapi import HTTPException
    from types import SimpleNamespace
    from workers_projects_runtime.profile_runtime import CodexCliRuntime
    from workers_projects_runtime.workspace_runtime import SharedWorkspaceRuntimes

    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    service = client.app.state.service
    owner_root = tmp_path / "owner-root"
    owner_root.mkdir()
    original_info = service.runtime._runtime_info
    monkeypatch.setattr(
        service.runtime, "_runtime_info",
        lambda worker, *, pid: replace(
            original_info(worker, pid=pid),
            workspace_dir=str(owner_root / "members" / worker["worker_id"]),
        ),
    )
    origin = service.create_project(
        "owner-a", "Synthetic origin", "Placement check", "codex-cli"
    )
    account = client.app.state.control_plane.create_provider_account(
        tenant_id="local", owner_id="owner-a", provider="codex",
        label="Synthetic selected account", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://synthetic-selected",
        status="ready",
    )
    payload = _payload(workspace)
    payload["metadata"]["glasshive_options"]["workspace"] = {"mode": "default"}
    payload["metadata"]["allowed_ai_origin_scope"] = {
        "version": 1, "tenant_id": "local", "owner_id": "owner-a",
        "project_id": origin["project_id"], "workspace_id": "",
        "connection_id": account["account_id"], "execution_mode": "docker",
    }
    provider = client.app.state.conversation_provider
    service.shared_workspace_readiness = lambda _workspace: {"available": True, "code": "ready"}
    parsed = ChatCompletionRequest.model_validate(payload)
    model = GLASSHIVE_MODELS[parsed.model]
    session = provider._create_native_session(
        parsed, model, workspace, "high", tenant_id="local"
    )
    worker = client.app.state.store.get_worker(session["worker_id"])
    assert worker["execution_mode"] == "docker"
    assert worker["workspace_id"]
    assert worker["bootstrap_profile"] == "none"
    assert worker["workspace_root"] != str(workspace)
    assert worker["workspace_dir"] != str(workspace)
    assert session["workspace_dir"] == worker["workspace_dir"]
    assert json.loads(worker["bootstrap_bundle_json"])["provider_account"]["account_id"] == account["account_id"]
    continued, changed = provider._session(parsed, model, workspace, "high", tenant_id="local")
    assert not changed
    assert continued["worker_id"] == worker["worker_id"]
    assert client.app.state.store.get_worker(worker["worker_id"])["workspace_dir"] == worker["workspace_dir"]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    payload["metadata"]["allowed_ai_origin_scope"]["execution_mode"] = "host"
    with pytest.raises(HTTPException) as error:
        provider._create_native_session(
            ChatCompletionRequest.model_validate(payload), model,
            workspace, "high", tenant_id="local",
        )
    assert error.value.status_code == 409
    payload["metadata"]["allowed_ai_origin_scope"]["execution_mode"] = "docker"
    payload["metadata"]["glasshive_options"]["workspace"] = {
        "mode": "custom", "path": str(workspace),
    }
    with pytest.raises(HTTPException) as error:
        provider._create_native_session(
            ChatCompletionRequest.model_validate(payload), model,
            workspace, "high", tenant_id="local",
        )
    assert error.value.status_code == 409

    for key, value in {
        "XPERFECT_EXECUTION_PROFILE": "hosted-xfs",
        "XPERFECT_SHARED_VOLUME_ROOT": str(owner_root),
        "XPERFECT_SHARED_VOLUME_NAME": "synthetic-data",
        "XPERFECT_SHARED_IMAGE": "synthetic-image",
        "XPERFECT_SHARED_MEMORY_BYTES": "1073741824",
        "XPERFECT_SHARED_PIDS_LIMIT": "64",
        "XPERFECT_SHARED_NETWORK": "synthetic-network",
        "XPERFECT_CONTROL_ROOT": str(tmp_path / "control"),
    }.items():
        monkeypatch.setenv(key, value)
    storage = SimpleNamespace(
        root=tmp_path, control_root=tmp_path / "control",
        owner_snapshot=lambda tenant, owner: SimpleNamespace(root=owner_root),
    )
    adapter = SharedWorkspaceRuntimes(client.app.state.store, storage=storage)
    native = adapter.for_worker(
        worker, CodexCliRuntime(base_dir=str(tmp_path / "runtime"), create_directories=False),
    )
    native.sandbox.assert_native_launch = lambda: (_ for _ in ()).throw(RuntimeError("reached-launch"))
    with pytest.raises(RuntimeError, match="reached-launch"):
        native.sandbox.ensure_ready(worker, "codex-cli")

    next_account = client.app.state.control_plane.create_provider_account(
        tenant_id="local", owner_id="owner-a", provider="codex",
        label="Synthetic next account", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://synthetic-next",
        status="ready",
    )
    payload["metadata"]["glasshive_options"]["workspace"] = {"mode": "default"}
    payload["metadata"]["allowed_ai_origin_scope"]["connection_id"] = next_account["account_id"]
    next_session, changed = provider._session(
        ChatCompletionRequest.model_validate(payload), model, workspace, "high", tenant_id="local"
    )
    assert changed
    assert next_session["worker_id"] != worker["worker_id"]
    next_worker = client.app.state.store.get_worker(next_session["worker_id"])
    assert next_worker["execution_mode"] == "docker"
    assert next_worker["workspace_id"] != worker["workspace_id"]
    assert json.loads(next_worker["bootstrap_bundle_json"])["provider_account"]["account_id"] == next_account["account_id"]

    failed_run = client.app.state.store.create_run(
        next_worker["worker_id"], next_worker["project_id"],
        "Exact retained instruction", state="queued",
    )
    client.app.state.store.update_run(
        failed_run["run_id"], state="failed",
        failure_class="provider_context_limit_exceeded", failure_structured=1,
    )
    request, _ = client.app.state.store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=next_session["session_id"],
        idempotency_key="member-recovery", message_id="member-recovery-message",
        stream_id="member-recovery-stream", requested_history_count=1,
        admitted_instruction="Exact retained instruction",
    )
    request = client.app.state.store.update_provider_request(
        request["request_id"], run_id=failed_run["run_id"], state="running",
    )
    recovered = provider._start_context_recovery(
        request, client.app.state.store.get_run(failed_run["run_id"])
    )
    recovered_session = client.app.state.store.get_provider_session_by_id(next_session["session_id"])
    replacement = client.app.state.store.get_worker(recovered_session["worker_id"])
    assert recovered["run_id"] != failed_run["run_id"]
    assert recovered["state"] != "failed"
    assert recovered["fallback_state"] != "context_recovery_failed"
    assert client.app.state.store.get_run(recovered["run_id"])["state"] != "cancelled"
    assert replacement["state"] not in {"failed", "terminated"}
    assert replacement["workspace_id"] != next_worker["workspace_id"]
    assert json.loads(replacement["bootstrap_bundle_json"])["provider_account"]["account_id"] == next_account["account_id"]
    assert any(
        item["event_type"] == "context-recovery"
        for item in client.app.state.store.list_provider_activity(request["request_id"])
    )
    continued, changed = provider._session(
        ChatCompletionRequest.model_validate(payload), model, workspace, "high", tenant_id="local"
    )
    assert not changed
    assert continued["worker_id"] == replacement["worker_id"]


def test_native_occupancy_pressure_advances_private_epoch_and_replays_full_admitted_source(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    first_payload = _payload(workspace)
    first_payload["metadata"].update({
        "message_id": "message-a",
        "stream_id": "stream-a",
        "idempotency_key": "idem-a",
        "main_context_protocol": "main_context_v1",
        "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64,
        "main_context_snapshot_sha256": "b" * 64,
        "main_context_epoch": "c" * 64,
        "continuity_domain_id": "d" * 64,
        "continuity_agent_id": "agent-main",
        "logical_turn_id": "turn-a",
        "visible_message_chain": [{
            "id": "source-a",
            "role": "user",
            "sha256": hashlib.sha256(b"Hello from LIFE.").hexdigest(),
            "accepted_source": True,
            "current_input": True,
        }],
    })
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    first_record = store.get_provider_request(first.json()["id"])
    first_session = store.get_provider_session_by_id(first_record["session_id"])
    first_manifest = json.loads(first_session["context_manifest_json"])
    store.update_provider_session_history(
        first_session["session_id"],
        history_count=first_session["history_count"],
        context_manifest={
            **first_manifest,
            "latest_native_prompt_tokens": 180000,
            "latest_native_total_tokens": 180010,
            "observed_chars_per_token": 4.0,
            "usage_calibration_scope": "admitted_instruction_bytes/native_prompt_tokens",
        },
    )
    second_payload = _payload(workspace)
    second_payload["messages"] = [
        *first_payload["messages"],
        {"role": "assistant", "content": first.json()["choices"][0]["message"]["content"]},
        {"role": "user", "content": "Continue with the measured context boundary."},
    ]
    second_payload["metadata"].update(
        message_id="message-b",
        stream_id="stream-b",
        idempotency_key="idem-b",
        main_context_protocol="main_context_v1",
        main_context_owner="core",
        stable_authority_sha256="a" * 64,
        main_context_snapshot_sha256="b" * 64,
        main_context_epoch="c" * 64,
        continuity_domain_id="d" * 64,
        continuity_agent_id="agent-main",
        logical_turn_id="turn-b",
        visible_message_chain=[
            {
                "id": "source-a",
                "role": "user",
                "sha256": hashlib.sha256(b"Hello from LIFE.").hexdigest(),
                "accepted_source": True,
            },
            {
                "id": "message-a",
                "role": "assistant",
                "sha256": hashlib.sha256(
                    first.json()["choices"][0]["message"]["content"].encode()
                ).hexdigest(),
            },
            {
                "id": "source-b",
                "role": "user",
                "sha256": hashlib.sha256(
                    b"Continue with the measured context boundary."
                ).hexdigest(),
                "accepted_source": True,
                "current_input": True,
            },
        ],
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)
    assert second.status_code == 200, second.text
    second_record = store.get_provider_request(second.json()["id"])
    decision = json.loads(second_record["replay_decision_json"])
    assert decision["native_occupancy"]["state"] == "pressure"
    assert decision["native_context_transition"]["reason"] == "native_occupancy_pressure"
    assert decision["native_context_transition"]["from_epoch"] == first_manifest.get("native_context_epoch", "")
    assert decision["native_context_epoch"] == decision["native_context_transition"]["to_epoch"]
    assert decision["mode"] == "bootstrap"
    assert decision["native_source_coverage"] == "complete"
    assert "Continue with the measured context boundary." in second_record["admitted_instruction"]
    second_session = store.get_provider_session_by_id(second_record["session_id"])
    assert second_session["worker_id"] == first_session["worker_id"]
    second_manifest = json.loads(second_session["context_manifest_json"])
    assert second_manifest["native_context_epoch_state"] == "active"


def test_native_occupancy_projection_keeps_boundary_delta_and_reserve_separate():
    model = GLASSHIVE_MODELS["codex-cli:gpt-5.6-sol"]
    decision = {
        "instruction_bytes": 3000,
        "projected_input_tokens": 750,
        "output_reserve_tokens": 27200,
    }
    below = ConversationProvider._native_occupancy_projection(
        model,
        {
            "latest_native_prompt_tokens": 160000,
            "observed_chars_per_token": 4.0,
            "usage_calibration_scope": "admitted_instruction_bytes/native_prompt_tokens",
        },
        decision,
    )
    at_boundary = ConversationProvider._native_occupancy_projection(
        model,
        {
            "latest_native_prompt_tokens": 165000,
            "observed_chars_per_token": 4.0,
            "usage_calibration_scope": "admitted_instruction_bytes/native_prompt_tokens",
        },
        decision,
    )
    assert below["state"] == "healthy"
    assert at_boundary["state"] == "pressure"
    assert below["admitted_delta_tokens"] == 750
    assert below["output_reserve_tokens"] == 27200
    assert at_boundary["projected_occupancy_tokens"] >= at_boundary["trigger_tokens"]


def test_native_source_coverage_requires_the_authorized_content_key():
    prior_key = "msg:source-a:" + ("a" * 64)
    assert not ConversationProvider._native_source_coverage_proven(
        {"accepted_visible_message_keys": [prior_key]},
        {},
        {0: "msg:source-a:" + ("b" * 64)},
        new_native_session=False,
    )
    assert ConversationProvider._native_source_coverage_proven(
        {"accepted_visible_message_keys": [prior_key]},
        {},
        {0: prior_key},
        new_native_session=False,
    )
    assert ConversationProvider._native_source_coverage_proven(
        {
            "accepted_visible_message_keys": ["msg:source-a", prior_key],
        },
        {},
        {0: prior_key},
        new_native_session=False,
    )
    assert not ConversationProvider._native_source_coverage_proven(
        {"accepted_visible_message_keys": ["msg:source-a"]},
        {},
        {0: prior_key},
        new_native_session=False,
    )


def test_native_delta_records_current_occupancy_without_recalibrating_ratio(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/v1/chat/completions", headers=AUTH, json=_payload(workspace)
    )
    assert response.status_code == 200, response.text
    store = client.app.state.store
    request = store.get_provider_request(response.json()["id"])
    session = store.get_provider_session_by_id(request["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    epoch = str(manifest.get("native_context_epoch") or "")
    store.update_provider_session_history(
        session["session_id"],
        history_count=session["history_count"],
        context_manifest={
            **manifest,
            "native_context_epoch": epoch,
            "latest_native_prompt_tokens": 17,
            "observed_chars_per_token": 4.0,
            "usage_calibration_scope": "admitted_instruction_bytes/native_prompt_tokens",
        },
    )
    decision = json.loads(request["replay_decision_json"])
    decision.update({
        "mode": "delta",
        "instruction_bytes": 40,
        "native_context_epoch": epoch,
        "native_source_coverage": "unproven",
    })
    store.update_provider_request(
        request["request_id"], replay_decision_json=json.dumps(decision)
    )
    client.app.state.conversation_provider._record_native_usage_calibration(
        store.get_provider_request(request["request_id"]),
        {"prompt_tokens": 180000, "completion_tokens": 20, "total_tokens": 180020},
    )
    store.record_provider_session_native_occupancy(
        request["session_id"],
        request_id="older-occupancy",
        prompt_tokens=50,
        total_tokens=60,
        measurement_epoch=epoch,
        measurement_created_at="2000-01-01T00:00:00+00:00",
    )
    store.record_provider_session_native_occupancy(
        request["session_id"],
        request_id="wrong-epoch-occupancy",
        prompt_tokens=40,
        total_tokens=50,
        measurement_epoch="wrong-epoch",
        measurement_created_at="2999-01-01T00:00:00+00:00",
    )
    updated = store.get_provider_session_by_id(request["session_id"])
    updated_manifest = json.loads(updated["context_manifest_json"])
    assert updated_manifest["latest_native_prompt_tokens"] == 180000
    assert updated_manifest["observed_chars_per_token"] == 4.0
    assert updated_manifest["native_occupancy_scope"] == "native_prompt_tokens"
    assert updated_manifest["native_occupancy_request_ids"] == [request["request_id"]]


def test_late_bootstrap_calibration_cannot_replace_newer_native_occupancy(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/v1/chat/completions", headers=AUTH, json=_payload(workspace)
    )
    assert response.status_code == 200, response.text
    store = client.app.state.store
    request = store.get_provider_request(response.json()["id"])
    session = store.get_provider_session_by_id(request["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    epoch = str(manifest.get("native_context_epoch") or "")
    newer_occupancy = "2026-09-22T12:00:03+00:00"
    older_calibration = "2026-09-22T12:00:02+00:00"

    legacy_seed = store.record_provider_session_usage_calibration(
        request["session_id"],
        request_id="legacy-calibration-seed",
        prompt_tokens=7,
        total_tokens=8,
        observed_chars_per_token=4.0,
        measurement_epoch=epoch,
    )
    assert json.loads(legacy_seed["context_manifest_json"])[
        "latest_native_prompt_tokens"
    ] == 7
    store.record_provider_session_native_occupancy(
        request["session_id"],
        request_id="newer-delta-occupancy",
        prompt_tokens=180000,
        total_tokens=180020,
        measurement_epoch=epoch,
        measurement_created_at=newer_occupancy,
    )
    calibration_request = {
        **request,
        "created_at": older_calibration,
        "replay_decision_json": json.dumps(
            {
                "mode": "bootstrap",
                "instruction_bytes": 400,
                "native_context_epoch": epoch,
                "native_source_coverage": "complete",
            }
        ),
    }
    client.app.state.conversation_provider._record_native_usage_calibration(
        calibration_request,
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )

    updated = store.get_provider_session_by_id(request["session_id"])
    updated_manifest = json.loads(updated["context_manifest_json"])
    assert updated_manifest["latest_native_prompt_tokens"] == 180000
    assert updated_manifest["latest_native_total_tokens"] == 180020
    assert updated_manifest["native_occupancy_latest_created_at"] == newer_occupancy
    assert updated_manifest["usage_calibration_latest_created_at"] == older_calibration
    assert updated_manifest["observed_chars_per_token"] == 4.0


def test_native_pressure_defers_when_prior_authorized_source_is_missing(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    retained_source = "The limit is 731 units."
    first_payload = _payload(workspace)
    first_payload["messages"][1]["content"] = retained_source
    first_payload["metadata"].update({
        "message_id": "message-a",
        "stream_id": "stream-a",
        "idempotency_key": "idem-a",
        "main_context_protocol": "main_context_v1",
        "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64,
        "main_context_snapshot_sha256": "b" * 64,
        "main_context_epoch": "c" * 64,
        "continuity_domain_id": "d" * 64,
        "continuity_agent_id": "agent-main",
        "logical_turn_id": "turn-a",
        "visible_message_chain": [{
            "id": "source-a",
            "role": "user",
            "sha256": hashlib.sha256(retained_source.encode()).hexdigest(),
            "accepted_source": True,
            "current_input": True,
        }],
    })
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    first_request = store.get_provider_request(first.json()["id"])
    first_session = store.get_provider_session_by_id(first_request["session_id"])
    first_manifest = json.loads(first_session["context_manifest_json"])
    store.update_provider_session_history(
        first_session["session_id"],
        history_count=first_session["history_count"],
        context_manifest={
            **first_manifest,
            "latest_native_prompt_tokens": 180000,
            "latest_native_total_tokens": 180010,
            "observed_chars_per_token": 4.0,
            "usage_calibration_scope": "admitted_instruction_bytes/native_prompt_tokens",
        },
    )

    # Keep the supplied prior assistant turn free of the accepted condition. The
    # current payload therefore contains other turns plus a new input, while the
    # durable source key for the condition is absent.
    prior_answer = "Earlier answer without the retained condition."
    current_input = "Continue with a new condition."
    second_payload = _payload(workspace)
    second_payload["messages"] = [
        {"role": "system", "content": "Be a thoughtful assistant."},
        {"role": "assistant", "content": prior_answer},
        {"role": "user", "content": "An unrelated prior turn."},
        {"role": "user", "content": current_input},
    ]
    second_payload["metadata"].update({
        "message_id": "message-b",
        "stream_id": "stream-b",
        "idempotency_key": "idem-b",
        "main_context_protocol": "main_context_v1",
        "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64,
        "main_context_snapshot_sha256": "b" * 64,
        "main_context_epoch": "c" * 64,
        "continuity_domain_id": "d" * 64,
        "continuity_agent_id": "agent-main",
        "logical_turn_id": "turn-b",
        "visible_message_chain": [
            {
                "id": "message-a",
                "role": "assistant",
                "sha256": hashlib.sha256(prior_answer.encode()).hexdigest(),
            },
            {
                "id": "unrelated-user",
                "role": "user",
                "sha256": hashlib.sha256(b"An unrelated prior turn.").hexdigest(),
            },
            {
                "id": "source-b",
                "role": "user",
                "sha256": hashlib.sha256(current_input.encode()).hexdigest(),
                "accepted_source": True,
                "current_input": True,
            },
        ],
    })
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)
    assert second.status_code == 200, second.text
    second_request = store.get_provider_request(second.json()["id"])
    decision = json.loads(second_request["replay_decision_json"])
    assert decision["mode"] == "delta"
    assert decision["native_occupancy"]["state"] == "pressure"
    assert decision["native_source_coverage"] == "unproven"
    assert decision["native_context_pressure_deferred"] is True
    assert decision["native_context_transition"] == {}
    assert decision["native_context_epoch"] == first_manifest.get("native_context_epoch", "")
    assert retained_source not in second_request["admitted_instruction"]


@pytest.mark.parametrize("prior_log", ["present", "empty", "unsupported", "collector_missing"])
def test_native_pressure_defers_when_prior_native_tool_evidence_is_not_replayed(
    tmp_path, monkeypatch, prior_log
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = NativeToolEvidenceRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    retained_source = "The limit is 731 units."
    first_payload = _payload(workspace)
    first_payload["messages"][1]["content"] = retained_source
    first_payload["metadata"].update(
        {
            "message_id": "message-a",
            "stream_id": "stream-a",
            "idempotency_key": "idem-a",
            "main_context_protocol": "main_context_v1",
            "main_context_owner": "core",
            "stable_authority_sha256": "a" * 64,
            "main_context_snapshot_sha256": "b" * 64,
            "main_context_epoch": "c" * 64,
            "continuity_domain_id": "d" * 64,
            "continuity_agent_id": "agent-main",
            "logical_turn_id": "turn-a",
            "visible_message_chain": [
                {
                    "id": "source-a",
                    "role": "user",
                    "sha256": hashlib.sha256(retained_source.encode()).hexdigest(),
                    "accepted_source": True,
                    "current_input": True,
                }
            ],
        }
    )
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    assert first.json()["choices"][0]["message"]["content"] == "The check is complete."

    store = client.app.state.store
    first_request = store.get_provider_request(first.json()["id"])
    first_session = store.get_provider_session_by_id(first_request["session_id"])
    first_manifest = json.loads(first_session["context_manifest_json"])
    first_epoch = str(first_manifest.get("native_context_epoch") or "")
    store.update_provider_session_history(
        first_session["session_id"],
        history_count=first_session["history_count"],
        context_manifest={
            **first_manifest,
            "latest_native_prompt_tokens": 180000,
            "latest_native_total_tokens": 180020,
            "observed_chars_per_token": 4.0,
            "usage_calibration_scope": "admitted_instruction_bytes/native_prompt_tokens",
        },
    )
    original_activity_log = runtime.provider_activity_log
    if prior_log == "empty":
        runtime.provider_activity_log = lambda worker, run_id: (
            ("codex-cli", "") if run_id == first_request["run_id"]
            else original_activity_log(worker, run_id)
        )
    elif prior_log == "unsupported":
        runtime.provider_activity_log = lambda worker, run_id: (
            ("unsupported-native-adapter", "opaque native tool records")
            if run_id == first_request["run_id"]
            else original_activity_log(worker, run_id)
        )
    elif prior_log == "collector_missing":
        runtime.provider_activity_log = None

    second_payload = _payload(workspace)
    second_payload["messages"] = [
        *first_payload["messages"],
        {"role": "assistant", "content": first.json()["choices"][0]["message"]["content"]},
        {"role": "user", "content": "Apply the checked limit."},
    ]
    second_payload["metadata"].update(
        {
            "message_id": "message-b",
            "stream_id": "stream-b",
            "idempotency_key": "idem-b",
            "main_context_protocol": "main_context_v1",
            "main_context_owner": "core",
            "stable_authority_sha256": "a" * 64,
            "main_context_snapshot_sha256": "b" * 64,
            "main_context_epoch": "c" * 64,
            "continuity_domain_id": "d" * 64,
            "continuity_agent_id": "agent-main",
            "logical_turn_id": "turn-b",
            "visible_message_chain": [
                {
                    "id": "source-a",
                    "role": "user",
                    "sha256": hashlib.sha256(retained_source.encode()).hexdigest(),
                    "accepted_source": True,
                },
                {
                    "id": "message-a",
                    "role": "assistant",
                    "sha256": hashlib.sha256(
                        first.json()["choices"][0]["message"]["content"].encode()
                    ).hexdigest(),
                },
                {
                    "id": "source-b",
                    "role": "user",
                    "sha256": hashlib.sha256(b"Apply the checked limit.").hexdigest(),
                    "accepted_source": True,
                    "current_input": True,
                },
            ],
        }
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)
    assert second.status_code == 200, second.text
    second_request = store.get_provider_request(second.json()["id"])
    decision = json.loads(second_request["replay_decision_json"])
    assert decision["native_occupancy"]["state"] == "pressure"
    assert decision["native_source_coverage"] == "unproven"
    assert decision["native_tool_evidence_coverage"] == "unproven"
    assert decision["native_context_pressure_deferred"] is True
    assert decision["native_context_transition"] == {}
    assert decision["native_context_epoch"] == first_epoch
    assert "Synthetic native-only tool condition" not in second_request["admitted_instruction"]


def test_completed_graph_target_is_removed_only_for_the_same_authenticated_turn_family(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    base_payload = _payload(workspace)
    base = client.post("/v1/chat/completions", headers=AUTH, json=base_payload)
    assert base.status_code == 200, base.text
    store = client.app.state.store
    base_record = store.get_provider_request(base.json()["id"])
    store.update_run(
        base_record["run_id"],
        state="completed",
        output_text=json.dumps(
            {"type": "tool_call", "tool_name": "lc_transfer_to_specialist"}
        ),
    )
    provider = client.app.state.conversation_provider
    graph_payload = dict(base_payload)
    graph_payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Completed specialist.",
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_other",
                "description": "Available other target.",
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            },
        },
    ]
    parsed = ChatCompletionRequest.model_validate(graph_payload)
    effective_tools = provider._graph_transfer_tools(parsed)
    names = {
        tool["function"]["name"] for tool in effective_tools or []
    }
    assert names == {"lc_transfer_to_other"}
    bundle = provider._native_bundle(parsed, provider._model(parsed.model), "medium")
    assert [item["name"] for item in bundle["agent_builder_control"]["tools"]] == [
        "lc_transfer_to_other"
    ]
    stable_decision = {
        "main_context_protocol": "main_context_v1",
        "main_context_owner": "core",
        "main_context_snapshot_sha256": "b" * 64,
        "context_epoch": "c" * 64,
        "logical_turn_id": "turn-a",
        "logical_turn_revision": 1,
    }
    store.update_provider_request(
        base_record["request_id"],
        replay_decision_json=json.dumps(stable_decision),
    )
    parsed.metadata.main_context_protocol = "main_context_v1"
    parsed.metadata.main_context_owner = "core"
    parsed.metadata.main_context_snapshot_sha256 = "b" * 64
    parsed.metadata.main_context_epoch = "c" * 64
    parsed.metadata.logical_turn_id = "turn-a"
    parsed.metadata.logical_turn_revision = 1
    same_turn_names = {
        tool["function"]["name"] for tool in provider._graph_transfer_tools(parsed) or []
    }
    assert same_turn_names == {"lc_transfer_to_other"}
    parsed.metadata.logical_turn_id = "turn-b"
    new_turn_names = {
        tool["function"]["name"] for tool in provider._graph_transfer_tools(parsed) or []
    }
    assert new_turn_names == {"lc_transfer_to_specialist", "lc_transfer_to_other"}
    forced = parsed.model_copy(
        update={
            "metadata": parsed.metadata.model_copy(update={"logical_turn_id": "turn-a"}),
            "tool_choice": {"type": "function", "function": {"name": "lc_transfer_to_specialist"}},
        }
    )
    with pytest.raises(ValueError, match="not available"):
        provider._graph_transfer_control(forced)


def test_native_log_window_compaction_is_recorded_in_the_private_session_manifest(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=CompactedActivityRuntime())

    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    manifest = json.loads(session["context_manifest_json"])
    assert manifest["compactions"] == [
        {"kind": "native_log_window", "excluded_prefix_bytes": 2048}
    ]
    assert manifest["last_request_id"] == response.json()["id"]


def test_dedicated_activity_sse_recovers_after_last_event_id(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=ActivityStubRuntime())
    completion = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = completion.json()["id"]
    events = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH).json()["data"]

    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/activity",
        headers={
            **AUTH,
            "Accept": "text/event-stream",
            "Last-Event-ID": str(events[0]["id"]),
        },
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

    recovered_ids = [int(line.removeprefix("id: ")) for line in lines if line.startswith("id: ")]
    assert recovered_ids == [event["id"] for event in events[1:]]
    assert any(line == "event: completed" for line in lines)


def test_completion_persists_native_activity_once_and_hides_internal_source_ids(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=ActivityStubRuntime())

    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = response.json()["id"]
    first = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH).json()["data"]
    second = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH).json()["data"]

    assert [event["event"] for event in first] == ["queued", "started", "tool", "file", "completed"]
    assert len(second) == len(first)
    assert "source_event_id" not in json.dumps(first)


def test_invalid_model_fails_loudly_and_missing_optional_agent_metadata_is_defaulted(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["model"] = "gpt-made-up"

    invalid_model = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert invalid_model.status_code == 400
    assert "Unsupported GlassHive model" in invalid_model.text

    payload = _payload(workspace)
    del payload["metadata"]["agent_id"]
    missing = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert missing.status_code == 200, missing.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert any(session["agent_id"] == "glasshive-direct" for session in sessions)


def test_relative_custom_workspace_fails_loudly_before_native_execution(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload["metadata"]["glasshive_options"]["workspace"]["path"] = "relative/Life"

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 400
    assert "absolute server-side path" in response.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_provider_routes_fail_closed_when_service_authentication_is_not_configured(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)
    monkeypatch.delenv("GLASSHIVE_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=StubRuntime(),
        )
    )

    response = client.get("/v1/models", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["message"] == "GlassHive provider authentication is not configured"


@pytest.mark.parametrize(
    ("first_name", "second_name", "message"),
    [
        (
            "GLASSHIVE_PROVIDER_API_KEY",
            "GLASSHIVE_MCP_API_KEY",
            "GLASSHIVE_MCP_API_KEY must be distinct from GLASSHIVE_PROVIDER_API_KEY",
        ),
        (
            "WPR_API_TOKEN",
            "GLASSHIVE_MCP_API_KEY",
            "GLASSHIVE_MCP_API_KEY must be distinct from WPR_API_TOKEN",
        ),
    ],
)
def test_runtime_rejects_shared_provider_mcp_or_admin_credentials(
    tmp_path, monkeypatch, first_name, second_name, message
):
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-admin-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_API_KEY", "provider-test-token")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "mcp-test-token")
    monkeypatch.setenv(first_name, "shared-token")
    monkeypatch.setenv(second_name, "shared-token")

    with pytest.raises(RuntimeError, match=message):
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=StubRuntime(),
        )


def test_openai_compatible_request_hydrates_structured_metadata_from_headers(tmp_path, monkeypatch):
    workspace = tmp_path / "Life folder"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = {
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Use the configured workspace."}],
        "stream": False,
        "reasoning_effort": "medium",
    }
    headers = {
        **AUTH,
        "X-Viventium-Conversation-Id": "conv-header",
        "X-GlassHive-Agent-Id": "agent-header",
        "X-Viventium-Message-Id": "message-header",
        "X-Viventium-Stream-Id": "stream-header",
        "X-Viventium-Surface": "telegram",
        "X-Viventium-Input-Mode": "voice_note",
        "X-GlassHive-Idempotency-Key": "idem-header",
        "X-GlassHive-Workspace-Mode": "custom",
        "X-GlassHive-Workspace-Path-B64": base64.b64encode(str(workspace).encode()).decode(),
        "X-GlassHive-Access": "full",
    }

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert session["conversation_id"] == "conv-header"
    assert session["agent_id"] == "agent-header"
    assert session["workspace_dir"] == str(workspace.resolve())
    assert session["access_mode"] == "full"


def test_activity_and_cancel_are_owner_scoped(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = response.json()["id"]

    denied = client.get(
        f"/v1/requests/{request_id}/activity",
        headers={**AUTH, "X-Viventium-User-Id": "owner-b"},
    )

    assert denied.status_code == 403


def test_cancel_by_idempotency_uses_authenticated_owner_scope(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    assert response.status_code == 200

    cancelled = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
    denied = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers={**AUTH, "X-Viventium-User-Id": "owner-b"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["id"] == response.json()["id"]
    assert cancelled.json()["state"] in {"completed", "cancelled"}
    assert denied.status_code == 200
    assert denied.json() == {"id": "", "object": "glasshive.request", "state": "cancelled"}
    assert client.app.state.store.get_provider_request(response.json()["id"])["owner_id"] == "owner-a"


def test_cancel_by_idempotency_before_request_prevents_a_late_native_start(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)

    cancelled = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
    late_start = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert late_start.status_code == 409
    assert "cancelled before native execution" in late_start.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_raw_idempotency_cancel_covers_audio_eligibility_identity(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime())
    audio_headers = {**AUTH, "X-Viventium-Audio-Eligible": "true"}

    response = client.post(
        "/v1/chat/completions",
        headers=audio_headers,
        json=_payload(workspace),
    )
    cancelled = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)

    assert response.status_code == 200, response.text
    assert cancelled.status_code == 200
    assert cancelled.json()["id"] == response.json()["id"]


def test_prestart_raw_idempotency_cancel_blocks_audio_eligible_start(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime())

    cancelled = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
    late_start = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Viventium-Audio-Eligible": "true"},
        json=_payload(workspace),
    )

    assert cancelled.status_code == 200
    assert late_start.status_code == 409
    assert "cancelled before native execution" in late_start.text


def test_raw_idempotency_cancel_prefers_and_cancels_active_variant():
    standard = {
        "request_id": "request-standard",
        "idempotency_key": _versioned_idempotency_key(
            "idem-a", audio_eligible=False
        ),
        "state": "completed",
    }
    audio = {
        "request_id": "request-audio",
        "idempotency_key": _versioned_idempotency_key(
            "idem-a", audio_eligible=True
        ),
        "state": "running",
    }

    class VariantStore:
        @staticmethod
        def upsert_provider_stop_tombstone(**kwargs):
            pass

        @staticmethod
        def list_provider_requests_by_idempotency_family(**kwargs):
            key = kwargs.get("base_idempotency_key")
            return [audio if key == audio["idempotency_key"] else standard]

    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = VariantStore()
    provider._start_lock = threading.RLock()
    cancelled_ids = []

    def cancel(request_id):
        cancelled_ids.append(request_id)
        return {**audio, "state": "cancelled"}

    provider.cancel = cancel

    result = provider.cancel_by_idempotency("idem-a", "owner-a")

    assert cancelled_ids == ["request-audio"]
    assert result["request_id"] == "request-audio"
    assert result["state"] == "cancelled"


def test_run_scoped_interrupt_cannot_cancel_a_newer_active_turn(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    project = store.create_project(
        "owner-a",
        "Synthetic conversation",
        "Cancellation scope regression",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Synthetic worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="gpt-5.6-sol",
    )
    # Restoring a running snapshot passes through a queued row; seed it before a
    # live scheduler exists, or its due pass can claim that row first.
    active = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "newer turn",
        state=RunRestorationState.RUNNING,
    )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        service.interrupt_worker(worker["worker_id"], run_id="older-request-run")

        assert runtime.interrupt_calls == []
        assert store.get_run(active["run_id"])["state"] == "running"
    finally:
        service.shutdown()


@pytest.mark.parametrize("by_family", [False, True])
@pytest.mark.parametrize(
    ("run_state", "already_cancelled", "newer_state", "rebound"),
    [
        ("queued", False, None, False),
        ("needs_input", False, None, False),
        ("needs_input", True, None, False),
        ("needs_input", True, "queued", False),
        ("needs_input", True, "running", False),
        ("queued", False, None, True),
        ("needs_input", False, None, True),
        ("needs_input", True, None, True),
    ],
)
def test_provider_cancel_settles_exact_pending_run(
    tmp_path, run_state, already_cancelled, newer_state, rebound, by_family
):
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    # Cancellation is exercised directly. A live scheduler would race the running
    # snapshots restored below and add its own wake-ups to `awakened`.
    service = WorkersProjectsService(
        store, runtime, reconcile_on_startup=False, start_background_consumers=False
    )
    provider: ConversationProvider | None = None
    try:
        project = store.create_project(
            "owner-a",
            "Synthetic conversation",
            "Queued cancellation regression",
            "codex-cli",
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-cancel",
            agent_id="agent-cancel",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        newer = None
        if newer_state == "running":
            newer = store.create_run(
                worker["worker_id"], project["project_id"], "preserve the active turn",
                state=RunRestorationState.RUNNING,
            )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "never execute this",
            state="queued",
        )
        if run_state == "needs_input":
            store.update_run(run["run_id"], state="needs_input")
            store.update_worker_state(worker["worker_id"], "needs_input")
        awakened = []
        service._ensure_worker_processor = awakened.append
        if newer_state == "queued":
            newer = store.create_run(
                worker["worker_id"], project["project_id"], "preserve the next turn",
                state="queued",
            )
        if newer_state == "running":
            store.update_worker_state(worker["worker_id"], "running")
        request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="queued-cancel",
            message_id="message-cancel",
            stream_id="stream-cancel",
            requested_history_count=0,
        )
        store.update_provider_request(request["request_id"], run_id=run["run_id"])
        if already_cancelled:
            store.update_provider_request(request["request_id"], state="cancelled")
        replacement_run = None
        if rebound:
            replacement_worker = store.create_worker(
                project_id=project["project_id"], owner_id="owner-a",
                name="Replacement worker", role="conversation-agent",
                profile="codex-cli", backend="", runtime="codex-cli",
                model="gpt-5.6-sol",
            )
            replacement_session = store.upsert_provider_session(
                tenant_id="local", owner_id="owner-a", conversation_id="conv-cancel",
                agent_id="agent-cancel", model_id="codex-cli:gpt-5.6-sol",
                project_id=project["project_id"], worker_id=replacement_worker["worker_id"],
                workspace_dir=str(tmp_path), access_mode="workspace",
            )
            assert replacement_session["session_id"] == session["session_id"]
            replacement_run = store.create_run(
                replacement_worker["worker_id"], project["project_id"],
                "preserve replacement worker turn", state=RunRestorationState.RUNNING,
            )
        provider = ConversationProvider(store, service)

        def cancel_target():
            return (provider.cancel_by_idempotency("queued-cancel", "owner-a")
                    if by_family else provider.cancel(request["request_id"]))

        if by_family and not already_cancelled:
            original_cancel_run = service.cancel_run
            def interrupted_cancel(*_args, **_kwargs):
                raise RuntimeError("simulated interrupted cancellation handoff")
            service.cancel_run = interrupted_cancel
            with pytest.raises(RuntimeError, match="interrupted cancellation handoff"):
                cancel_target()
            assert store.get_provider_request(request["request_id"])["state"] == "cancelled"
            service.cancel_run = original_cancel_run
        cancelled = cancel_target()

        assert cancelled["state"] == "cancelled"
        assert store.get_run(run["run_id"])["state"] == "cancelled"
        assert runtime.interrupt_calls == []
        if replacement_run:
            assert store.get_run(replacement_run["run_id"])["state"] == "running"
        if newer:
            assert store.get_run(newer["run_id"])["state"] == newer_state
        if newer_state == "queued":
            assert worker["worker_id"] in awakened
        if run_state == "needs_input":
            assert store.get_worker(worker["worker_id"])["state"] != "needs_input"
        assert provider._sync(store.get_provider_request(request["request_id"]))["state"] == "cancelled"
    finally:
        if provider is not None:
            provider.shutdown()
        service.shutdown()


def test_sync_never_resurrects_a_cancelled_provider_request(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = None
    provider: ConversationProvider | None = None
    try:
        project = store.create_project(
            "owner-a", "Synthetic conversation", "Cancellation race", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-race",
            agent_id="agent-race",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "late completion",
            state=RunRestorationState.RUNNING,
        )
        request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="cancel-race",
            message_id="message-race",
            stream_id="stream-race",
            requested_history_count=0,
        )
        store.update_provider_request(
            request["request_id"], run_id=run["run_id"], state="cancelled"
        )
        store.finalize_run(run["run_id"], state="completed", output_text="late answer")
        service = WorkersProjectsService(
            store, InterruptCountingRuntime(), reconcile_on_startup=False,
        )
        provider = ConversationProvider(store, service)

        synced = provider._sync(store.get_provider_request(request["request_id"]))

        assert synced["state"] == "cancelled"
        assert all(
            event["event_type"] != "completed"
            for event in store.list_provider_activity(request["request_id"])
        )
    finally:
        if provider is not None:
            provider.shutdown()
        if service is not None:
            service.shutdown()


def test_stream_disconnect_reconciles_a_later_completed_run(tmp_path):
    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    store = Store(str(tmp_path / "runtime.db"))
    # This test restores a running row synchronously. Keep the service queue
    # consumer out of the create_run -> restore claim window; provider
    # reconciliation is the lifecycle under test below.
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    provider: ConversationProvider | None = None
    try:
        project = store.create_project(
            "owner-a", "Synthetic conversation", "Disconnect reconciliation", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-disconnect",
            agent_id="agent-disconnect",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "finish after disconnect",
            state=RunRestorationState.RUNNING,
        )
        request_record, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="disconnect-reconciliation",
            message_id="message-disconnect",
            stream_id="stream-disconnect",
            requested_history_count=2,
        )
        request_record = store.update_provider_request(
            request_record["request_id"], run_id=run["run_id"], state="running"
        )
        provider = ConversationProvider(store, service)
        payload = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))

        async def consume_until_disconnect() -> list[str]:
            return [
                chunk
                async for chunk in provider.stream(
                    request_record,
                    payload,
                    DisconnectedRequest(),
                )
            ]

        chunks = asyncio.run(consume_until_disconnect())
        assert len(chunks) == 1
        assert store.get_provider_request(request_record["request_id"])["state"] == "running"

        store.finalize_run(run["run_id"], state="completed", output_text="durable answer")
        deadline = time.time() + 2
        while time.time() < deadline:
            if store.get_provider_request(request_record["request_id"])["state"] == "completed":
                break
            time.sleep(0.01)

        completed_request = store.get_provider_request(request_record["request_id"])
        assert completed_request["state"] == "completed"
        assert [
            event["event_type"]
            for event in store.list_provider_activity(request_record["request_id"])
        ].count("completed") == 1
        assert store.get_provider_session_by_id(session["session_id"])["history_count"] == 3
    finally:
        if provider is not None:
            provider.shutdown()
        service.shutdown()


def test_provider_startup_reconciles_a_request_left_running_by_process_restart(tmp_path):
    class RecoveredCompletionRuntime(StubRuntime):
        def collect_completed_run(self, worker, run_id=None, instruction=None):
            _ = worker, run_id, instruction
            return {
                "state": "completed",
                "output_text": "Recovered after provider restart.",
                "error_text": "",
            }

    store = Store(str(tmp_path / "runtime.db"))
    runtime = RecoveredCompletionRuntime()
    # The provider restart test owns the restore claim. A service processor
    # racing the synchronous RUNNING restoration makes the fixture order
    # dependent before provider recovery starts.
    service = WorkersProjectsService(
        store,
        runtime,
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    provider: ConversationProvider | None = None
    try:
        project = store.create_project(
            "owner-a", "Synthetic conversation", "Provider restart recovery", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-restart",
            agent_id="agent-restart",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "finish across provider restart",
            state=RunRestorationState.RUNNING,
        )
        request_record, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="provider-restart-reconciliation",
            message_id="message-restart",
            stream_id="stream-restart",
            requested_history_count=1,
        )
        store.update_provider_request(
            request_record["request_id"],
            run_id=run["run_id"],
            state="running",
        )

        provider = ConversationProvider(store, service)
        deadline = time.time() + 2
        while time.time() < deadline:
            if store.get_provider_request(request_record["request_id"])["state"] == "completed":
                break
            time.sleep(0.01)

        assert store.get_run(run["run_id"])["state"] == "completed"
        assert store.get_provider_request(request_record["request_id"])["state"] == "completed"
        assert [
            event["event_type"]
            for event in store.list_provider_activity(request_record["request_id"])
        ].count("completed") == 1
        deadline = time.time() + 5
        while time.time() < deadline and request_record["request_id"] in provider._detached_reconciliations:
            time.sleep(0.01)
        assert request_record["request_id"] not in provider._detached_reconciliations
    finally:
        if provider is not None:
            provider.shutdown()
        service.shutdown()


def test_provider_startup_fails_loudly_for_prestart_request_without_native_run(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    runtime = StubRuntime()
    service = WorkersProjectsService(
        store,
        runtime,
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    provider: ConversationProvider | None = None
    try:
        project = store.create_project(
            "owner-a", "Synthetic conversation", "Prestart recovery", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-prestart",
            agent_id="agent-prestart",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        request_record, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="provider-prestart-interrupted",
            message_id="message-prestart",
            stream_id="stream-prestart",
            requested_history_count=1,
        )

        provider = ConversationProvider(store, service)
        deadline = time.time() + 2
        while time.time() < deadline:
            if store.get_provider_request(request_record["request_id"])["state"] == "failed":
                break
            time.sleep(0.01)

        assert store.get_provider_request(request_record["request_id"])["state"] == "failed"
        failed_events = [
            event
            for event in store.list_provider_activity(request_record["request_id"])
            if event["event_type"] == "failed"
        ]
        assert len(failed_events) == 1
        assert json.loads(failed_events[0]["payload_json"])["failure_class"] == "prestart_interrupted"
        assert request_record["request_id"] not in provider._detached_reconciliations
    finally:
        if provider is not None:
            provider.shutdown()
        service.shutdown()


def test_service_startup_monitor_recovers_a_non_provider_host_run(tmp_path):
    class RecoveredMissionRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.collect_calls = 0

        def collect_completed_run(self, worker, run_id=None, instruction=None):
            _ = worker, run_id, instruction
            self.collect_calls += 1
            if self.collect_calls < 2:
                return None
            return {
                "state": "completed",
                "output_text": "Recovered mission result.",
                "error_text": "",
            }

    store = Store(str(tmp_path / "runtime.db"))
    runtime = RecoveredMissionRuntime()
    # Create the restored run before any queue consumer can claim it. The
    # explicit reconcile below starts the surviving-run monitor being tested.
    service = WorkersProjectsService(
        store,
        runtime,
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    try:
        project = store.create_project(
            "owner-a", "Synthetic mission", "Mission restart recovery", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic mission worker",
            role="general",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "finish the mission across restart",
            state=RunRestorationState.RUNNING,
        )

        service.reconcile_all_workers()
        deadline = time.time() + 2
        while time.time() < deadline:
            if store.get_run(run["run_id"])["state"] == "completed":
                break
            time.sleep(0.01)

        assert store.get_run(run["run_id"])["state"] == "completed"
        assert store.get_run(run["run_id"])["output_text"] == "Recovered mission result."
        deadline = time.time() + 5
        while time.time() < deadline:
            if [event["event_type"] for event in store.list_events(worker["worker_id"])].count(
                "run.completed"
            ) == 1:
                break
            time.sleep(0.01)
        assert [event["event_type"] for event in store.list_events(worker["worker_id"])].count(
            "run.completed"
        ) == 1
    finally:
        service.shutdown()


def test_activity_payload_recursively_redacts_private_strings(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = response.json()["id"]
    client.app.state.store.add_provider_activity(
        request_id,
        "file",
        "A file changed.",
        {
            "path": "/Users/private-person/Documents/secret-project/file.txt",
            "nested": ["token=super-secret-value"],
        },
    )

    activity = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH)
    serialized = json.dumps(activity.json())

    assert activity.status_code == 200
    assert "private-person" not in serialized
    assert "super-secret-value" not in serialized
    assert "[REDACTED" in serialized


def test_streaming_redactor_handles_secrets_split_across_chunks():
    redactor = StreamingRedactor(overlap=32)

    visible = redactor.feed("Before api_key=PUBLIC_FAKE_")
    visible += redactor.feed("SECRET_VALUE after")
    visible += redactor.flush()

    assert "PUBLIC_FAKE_SECRET_VALUE" not in visible
    assert "[REDACTED]" in visible
    assert GLASSHIVE_MODELS["codex-cli:gpt-5.6-sol"].recommended_effort == "medium"


def test_streaming_redactor_emits_safe_text_before_newline_or_terminal_flush():
    redactor = StreamingRedactor(overlap=16)

    first = redactor.feed("A safe response can arrive word by word without waiting for a newline")
    final = redactor.flush()

    assert first
    assert first + final == "A safe response can arrive word by word without waiting for a newline"


def test_streaming_redactor_default_emits_an_ordinary_short_answer_incrementally():
    redactor = StreamingRedactor()

    first = redactor.feed(
        "This is an ordinary safe conversational answer that should appear before completion. "
        "It is intentionally far shorter than one kilobyte."
    )

    assert first


def test_streaming_redactor_redacts_newline_split_home_path_and_bounds_long_lines():
    redactor = StreamingRedactor(overlap=16, max_buffer=64)

    visible = redactor.feed("Path /Users/synthetic/\nDocuments/private.txt\n")
    visible += redactor.feed("x" * 65)
    visible += redactor.flush()

    assert "/Users/synthetic" not in visible
    assert "[REDACTED_LOCAL_PATH]" in visible
    assert "[REDACTED_OVERSIZED_STREAM_SEGMENT]" in visible


def test_streaming_redactor_holds_and_redacts_split_common_credentials():
    redactor = StreamingRedactor(overlap=16, max_buffer=512)

    visible = redactor.feed("token ghp_synthetic")
    visible += redactor.feed("githubcredential then xoxb-synthetic-")
    visible += redactor.feed("slack-credential done\n")
    visible += redactor.flush()

    assert "syntheticgithubcredential" not in visible
    assert "synthetic-slack-credential" not in visible
    assert "[REDACTED]" in visible


def test_streaming_redactor_holds_a_multiline_private_key_until_it_can_be_redacted():
    redactor = StreamingRedactor(overlap=32, max_buffer=512)
    private_key_body = "A" * 120

    visible = redactor.feed(
        "Safe preamble. "
        + ("word " * 24)
        + "-----BEGIN "
        + "PRIVATE KEY-----\n"
    )
    visible += redactor.feed(f"{private_key_body}\n")
    visible += redactor.feed("-----END PRIVATE KEY-----\nSafe tail.")
    visible += redactor.flush()

    assert "BEGIN PRIVATE KEY" not in visible
    assert private_key_body not in visible
    assert "[REDACTED_PRIVATE_KEY]" in visible
    assert "Safe preamble." in visible
    assert "Safe tail." in visible


def test_streaming_redactor_fails_closed_for_an_oversized_unterminated_private_key():
    redactor = StreamingRedactor(overlap=16, max_buffer=96)

    visible = redactor.feed("Safe prefix. -----BEGIN PRIVATE KEY-----\n")
    visible += redactor.feed(("A" * 64) + "\n")
    visible += redactor.feed(("B" * 64) + "\nunsafe trailing text")
    visible += redactor.flush()

    assert "BEGIN PRIVATE KEY" not in visible
    assert "A" * 32 not in visible
    assert "B" * 32 not in visible
    assert "unsafe trailing text" not in visible
    assert "[REDACTED_OVERSIZED_STREAM_SEGMENT]" in visible
    assert "Safe prefix." in visible


def test_streaming_redactor_holds_a_bearer_credential_split_at_whitespace():
    redactor = StreamingRedactor(overlap=16, max_buffer=256)

    visible = redactor.feed(("safe " * 20) + "Bearer")
    visible += redactor.feed(" ")
    visible += redactor.feed("synthetic-bearer-credential\nSafe tail.")
    visible += redactor.flush()

    assert "synthetic-bearer-credential" not in visible
    assert "Bearer [REDACTED]" in visible
    assert "Safe tail." in visible


def test_production_sse_redacts_a_secret_split_across_native_stream_snapshots(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=SplitSecretStreamingRuntime())
    native_snapshots = [
        "Before api_key=PUBLIC_FAKE_",
        "Before api_key=PUBLIC_FAKE_SECRET_VALUE after\n",
    ]
    observed_snapshots: list[str] = []

    def split_native_snapshot(_request_record, _run):
        index = min(len(observed_snapshots), len(native_snapshots) - 1)
        snapshot = native_snapshots[index]
        observed_snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(
        client.app.state.conversation_provider,
        "_native_output_snapshot",
        split_native_snapshot,
    )
    payload = _payload(workspace, model="claude-code:opus", stream=True)

    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json=payload) as response:
        assert response.status_code == 200, response.text
        serialized = "\n".join(line for line in response.iter_lines() if line)

    assert len(observed_snapshots) >= 2
    assert observed_snapshots[0] == "Before api_key=PUBLIC_FAKE_"
    assert observed_snapshots[1] == "Before api_key=PUBLIC_FAKE_SECRET_VALUE after\n"
    assert "PUBLIC_FAKE_SECRET_VALUE" not in serialized
    assert "PUBLIC_FAKE_" not in serialized
    assert "[REDACTED]" in serialized
    assert serialized.endswith("data: [DONE]")


def test_native_visible_text_never_falls_back_to_raw_ndjson_or_thinking():
    raw = "\n".join(
        [
            "not-json token=synthetic-private-value",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "hidden private reasoning"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "secret"}},
                            {"type": "text", "text": "Safe visible answer."},
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "result": "Safe visible answer."}),
        ]
    )

    assert _native_visible_text("claude-code", raw) == "Safe visible answer."
    assert _native_visible_text("unknown-profile", raw) == ""


def test_native_visible_text_waits_for_claude_result_and_excludes_working_preamble():
    raw = "\n".join(
        [
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "thinking_delta", "thinking": "hidden"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "I will inspect the file."},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Exact final answer."},
                    },
                }
            ),
            json.dumps({"type": "result", "result": "Exact final answer."}),
        ]
    )

    assert _native_visible_text("claude-code", raw) == "Exact final answer."
    assert _native_visible_text("claude-code", "\n".join(raw.splitlines()[:-1])) == ""


def test_native_visible_text_waits_for_codex_turn_and_returns_only_latest_agent_message():
    lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "I will inspect the file."},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Exact final answer."},
            }
        ),
    ]

    assert _native_visible_text("codex-cli", "\n".join(lines)) == ""
    assert _native_visible_text(
        "codex-cli",
        "\n".join([*lines, json.dumps({"type": "turn.completed"})]),
    ) == "Exact final answer."


def test_native_visible_text_waits_for_matching_grok_terminal_result():
    started = json.dumps({"type": "grok.session.started", "session_id": "session-a"})
    update = json.dumps({"type": "grok.session.update", "session_id": "session-a",
                         "update": {"sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "Unfinished private draft"}}})
    completed = json.dumps({"type": "grok.result", "session_id": "session-a",
                            "stop_reason": "end_turn", "output": "Exact final answer."})
    assert _native_visible_text("grok-build", "\n".join((started, update))) == ""
    assert _native_visible_text("grok-build", "\n".join((started, update, completed))) == "Exact final answer."
    assert _native_visible_text("grok-build", "\n".join((started, json.dumps({
        "type": "grok.result", "session_id": "other", "stop_reason": "end_turn",
        "output": "Wrong session."})))) == ""
    assert _native_visible_text("grok-build", "\n".join((started, completed, completed))) == ""


def test_native_visible_text_shows_only_grok_final_report_when_present():
    started = json.dumps({"type": "grok.session.started", "session_id": "session-a"})
    completed = json.dumps({"type": "grok.result", "session_id": "session-a",
                            "stop_reason": "end_turn",
                            "output": "Working draft.\n\nFINAL REPORT: The requested answer."})
    assert _native_visible_text("grok-build", "\n".join((started, completed))) == "The requested answer."


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code"])
def test_completed_native_harness_without_terminal_answer_fails_loudly(
    tmp_path, monkeypatch, profile
):
    class MissingTerminalRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            _publish_in_process_test_start(worker)
            return "I am still working on it."

        def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
            _ = worker, run_id
            if profile == "codex-cli":
                return profile, json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "I am still working on it.",
                        },
                    }
                )
            return profile, json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I am still working on it."}
                        ]
                    },
                }
            )

    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=MissingTerminalRuntime())
    model = "codex-cli:gpt-5.6-sol" if profile == "codex-cli" else "claude-code:opus"

    response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_payload(workspace, model=model),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "missing_terminal_response"
    assert "terminal authored response" in response.text
    assert "I am still working on it" not in response.text


def test_native_usage_ignores_non_usage_events_and_normalizes_counts():
    stdout = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Hi"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}}),
        ]
    )

    assert _native_usage("codex-cli", stdout) == {
        "prompt_tokens": 11,
        "completion_tokens": 3,
        "total_tokens": 14,
    }


def test_provider_startup_prunes_only_old_terminal_requests_and_idle_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_REQUEST_RETENTION_DAYS", "30")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_SESSION_RETENTION_DAYS", "90")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    provider: ConversationProvider | None = None
    try:
        project = store.create_project("owner-a", "Old conversation", "retention", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Old worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-old",
            agent_id="agent-a",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="full",
            history_count=2,
            context_manifest={"messages": 2},
        )
        request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="old-terminal",
            message_id="message-old",
            stream_id="stream-old",
            requested_history_count=1,
        )
        store.update_provider_request(request["request_id"], state="completed")
        store.add_provider_activity(request["request_id"], "completed", "Completed")
        old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
        with store._connect() as conn:
            conn.execute(
                "UPDATE provider_requests SET created_at = ?, updated_at = ? WHERE request_id = ?",
                (old, old, request["request_id"]),
            )
            conn.execute(
                "UPDATE provider_sessions SET created_at = ?, updated_at = ? WHERE session_id = ?",
                (old, old, session["session_id"]),
            )

        provider = ConversationProvider(store, service)

        assert store.get_provider_request(request["request_id"]) is None
        assert store.list_provider_sessions(owner_id="owner-a") == []
        assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    finally:
        if provider is not None:
            provider.shutdown()
        service.shutdown()


def test_provider_reapplies_retention_during_a_long_lived_process(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    provider: ConversationProvider | None = None
    try:
        provider = ConversationProvider(store, service)
        calls = 0
        original = provider._apply_retention_policy

        def counted_retention():
            nonlocal calls
            calls += 1
            original()

        provider._apply_retention_policy = counted_retention
        provider._last_retention_monotonic = time.monotonic() - 7200
        provider._maybe_apply_retention_policy()

        assert calls == 1
        assert time.monotonic() - provider._last_retention_monotonic < 2
    finally:
        if provider is not None:
            provider.shutdown()
        service.shutdown()


def test_codex_native_events_become_safe_tool_file_plan_and_reasoning_activity():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "private hidden reasoning"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "todo_list",
                        "items": [{"text": "Inspect private records", "completed": True}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": (
                            "curl -H 'Authorization: "
                            + "Bearer "
                            + "synthetic-secret-value' example.invalid"
                        ),
                        "aggregated_output": "token=synthetic-secret-value",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "file_change",
                        "changes": [{"path": "/Users/private-person/Documents/private.txt"}],
                        "status": "completed",
                    },
                }
            ),
        ]
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert [event["event_type"] for event in events] == [
        "reasoning-summary",
        "plan",
        "tool",
        "file",
    ]
    serialized = json.dumps(events)
    assert "private hidden reasoning" not in serialized
    assert "synthetic-secret-value" not in serialized
    assert "private-person" not in serialized
    assert events[2]["payload"]["source_event_id"].startswith("codex-cli:")
    assert events[2]["payload"]["source_event_id"].endswith(":0")
    assert {key: value for key, value in events[2]["payload"].items() if key != "source_event_id"} == {
        "tool": "shell",
        "status": "completed",
        "exit_code": 0,
    }
    assert events[3]["payload"]["change_count"] == 1


def test_codex_broker_tool_completion_exposes_only_safe_task_and_status():
    stdout = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "private-call-id",
                "duration": 1.25,
                "invocation": {
                    "server": "glasshive-user-capabilities",
                    "tool": "gh_scheduling_cortex__schedule_create",
                    "arguments": {
                        "title": "private reminder title",
                        "token": "synthetic-secret-value",
                    },
                },
                "result": {
                    "Ok": {
                        "content": [{"type": "text", "text": "private result"}],
                        "isError": False,
                    }
                },
            },
        }
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert len(events) == 1
    assert events[0]["event_type"] == "tool"
    assert events[0]["summary"] == "Connected tool completed: schedule create."
    assert {
        key: value
        for key, value in events[0]["payload"].items()
        if key != "source_event_id"
    } == {
        "tool": "connected_tool",
        "task": "schedule create",
        "status": "completed",
    }
    serialized = json.dumps(events)
    for forbidden in [
        "glasshive-user-capabilities",
        "gh_scheduling_cortex",
        "private-call-id",
        "private reminder title",
        "synthetic-secret-value",
    ]:
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "result",
    [
        {
            "isError": False,
            "content": [{"type": "text", "text": "Private successful retrieval"}],
            "structured_content": {"error": "domain evidence, not an execution error"},
        },
        {
            "success": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Retrieved document: 1 validation error for call\n"
                        "field\nField required [type=missing]\n"
                        "https://errors.pydantic.dev/2.12/v/missing"
                    ),
                }
            ],
        },
    ],
)
def test_codex_explicit_success_is_not_overridden_by_domain_result_data(result):
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "private-call-id",
                "type": "mcp_tool_call",
                "tool": "gh_retrieval__read",
                "status": "completed",
                "result": result,
            },
        }
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert len(events) == 1
    assert events[0]["summary"] == "Connected tool completed: read."
    assert events[0]["payload"]["status"] == "completed"
    serialized = json.dumps(events)
    assert "domain evidence" not in serialized
    assert "validation error" not in serialized


def test_codex_completed_item_preserves_structured_mcp_failure_truth():
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "private-call-id",
                "type": "mcp_tool_call",
                "tool": "gh_scheduling_cortex__schedule_create",
                "status": "completed",
                "arguments": {"private": "synthetic-secret-value"},
                "result": {
                    "content": [{"type": "text", "text": "Private blocked result"}],
                    "structured_content": {
                        "status": "blocked",
                        "reason": "private broker reason",
                    },
                },
            },
        }
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert len(events) == 1
    assert events[0]["summary"] == "Connected tool failed: schedule create."
    assert events[0]["payload"]["status"] == "failed"
    serialized = json.dumps(events)
    assert "synthetic-secret-value" not in serialized
    assert "private broker reason" not in serialized


def test_codex_duplicate_native_views_emit_one_terminal_tool_receipt():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "mcp_tool_call_end",
                        "call_id": "shared-private-call-id",
                        "invocation": {
                            "server": "glasshive-user-capabilities",
                            "tool": "gh_scheduling_cortex__schedule_create",
                            "arguments": {},
                        },
                        "result": {"ok": True},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "shared-private-call-id",
                        "type": "mcp_tool_call",
                        "tool": "gh_scheduling_cortex__schedule_create",
                        "status": "completed",
                    },
                }
            ),
        ]
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    terminal = [event for event in events if event["payload"].get("status") == "completed"]
    assert len(terminal) == 1
    assert terminal[0]["summary"] == "Connected tool completed: schedule create."


def test_identical_native_tool_events_keep_distinct_stable_source_ids():
    event = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "status": "completed", "exit_code": 0},
        }
    )

    events = _normalized_harness_activity("codex-cli", f"{event}\n{event}\n")

    assert len(events) == 2
    assert events[0]["payload"]["source_event_id"] != events[1]["payload"]["source_event_id"]


def test_claude_stream_events_expose_tool_categories_without_tool_inputs_or_thinking():
    stdout = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hidden internal reasoning"},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/Users/private-person/Documents/private.txt"},
                    },
                    {
                        "type": "tool_use",
                        "name": "WebSearch",
                        "input": {"query": "private query"},
                    },
                ]
            },
        }
    )

    events = _normalized_harness_activity("claude-code", stdout)

    assert [event["event_type"] for event in events] == ["file", "tool"]
    assert events[0]["payload"]["tool"] == "file"
    assert events[1]["payload"]["tool"] == "web_search"
    serialized = json.dumps(events)
    assert "hidden internal reasoning" not in serialized
    assert "private-person" not in serialized
    assert "private query" not in serialized


def test_public_activity_delta_fields_carry_only_bounded_public_identity():
    from workers_projects_runtime.conversation_provider import _public_activity_delta_fields

    fields = _public_activity_delta_fields(
        {
            "event_type": "tool",
            "payload_json": json.dumps(
                {
                    "tool": "connected_tool",
                    "task": "worker delegate once",
                    "status": "completed",
                    "source_event_id": "codex-cli:12:0",
                    "call_id": "call_secret",
                    "invocation": {"tool": "glasshive__worker_delegate_once", "arguments": {"x": 1}},
                }
            ),
        }
    )
    assert fields == {
        "provider_specific_fields": {
            "viventium": {
                "activity": {
                    "event": "tool",
                    "tool": "connected_tool",
                    "task": "worker delegate once",
                    "status": "completed",
                }
            }
        }
    }
    assert _public_activity_delta_fields({"event_type": "", "payload_json": "{}"}) == {}
    assert _public_activity_delta_fields({"event_type": "reasoning-summary", "payload_json": "{}"}) == {
        "provider_specific_fields": {"viventium": {"activity": {"event": "reasoning-summary"}}}
    }


def test_request_creation_persists_bounded_admission_atomically(tmp_path, monkeypatch):
    """The admitted instruction and its ReplayDecisionV1 are created with the request row, and a
    rapid follow-up turn on the same session admits only the delta after the prior history."""
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    first_request = store.get_provider_request(first.json()["id"])
    first_decision = json.loads(first_request["replay_decision_json"])
    assert first_decision["version"] == 1
    assert first_decision["provider_session_mode"] == "persistent"
    assert first_decision["admitted_message_indices"] == [
        index
        for index, message in enumerate(first_payload["messages"])
        if str(message.get("role") or "").lower() not in {"system", "developer"}
    ]
    first_user_text = next(
        str(message["content"])
        for message in reversed(first_payload["messages"])
        if message.get("role") == "user"
    )
    assert first_user_text in str(first_request["admitted_instruction"])

    second_payload = _payload(workspace)
    second_payload["messages"] = [
        *first_payload["messages"],
        {"role": "assistant", "content": first.json()["choices"][0]["message"]["content"]},
        {"role": "user", "content": "Rapid follow-up: make it shorter."},
    ]
    second_payload["metadata"]["message_id"] = "message-rapid-2"
    second_payload["metadata"]["idempotency_key"] = "idem-rapid-2"
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)
    assert second.status_code == 200, second.text
    second_request = store.get_provider_request(second.json()["id"])
    assert second_request["session_id"] == first_request["session_id"]
    second_decision = json.loads(second_request["replay_decision_json"])
    assert second_decision["version"] == 1
    previous_history_count = len(first_payload["messages"])
    assert second_decision["admitted_message_indices"]
    assert min(second_decision["admitted_message_indices"]) >= previous_history_count
    assert "Rapid follow-up: make it shorter." in str(second_request["admitted_instruction"])
    assert first_user_text not in str(second_request["admitted_instruction"])


def test_connected_tool_activity_carries_a_typed_deferred_callback_anchor_only_for_dispatch_tools():
    """Only GlassHive run-dispatching tools may arm long host polling; ordinary tools never do."""
    from workers_projects_runtime.mcp_tool_registry import DEFERRED_CALLBACK_TOOLS

    def codex_end(tool: str, call_id: str) -> str:
        return json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": call_id,
                    "invocation": {"server": "glasshive", "tool": tool, "arguments": {}},
                    "result": {"Ok": {"content": [{"type": "text", "text": "ok"}], "isError": False}},
                },
            }
        )

    stdout = "\n".join(
        [
            codex_end("worker_delegate_once", "call-1"),
            codex_end("feelings_get_state", "call-2"),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "id": "call-3",
                        "tool": "glasshive__workspace_launch",
                        "status": "completed",
                    },
                }
            ),
        ]
    ) + "\n"
    events = _normalized_harness_activity("codex-cli", stdout)
    payloads = [event["payload"] for event in events]
    assert [p["task"] for p in payloads] == ["worker delegate once", "feelings get state", "workspace launch"]
    assert payloads[0].get("expects_deferred_callback") is True
    assert "expects_deferred_callback" not in payloads[1]
    assert payloads[2].get("expects_deferred_callback") is True

    claude_stdout = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__glasshive-workers-projects__worker_run", "input": {}},
                    {"type": "tool_use", "name": "mcp__feelings__feelings_get_state", "input": {}},
                ]
            },
        }
    )
    claude_events = _normalized_harness_activity("claude-code", claude_stdout + "\n")
    assert claude_events[0]["payload"].get("expects_deferred_callback") is True
    assert "expects_deferred_callback" not in claude_events[1]["payload"]
    assert "worker_run" in DEFERRED_CALLBACK_TOOLS and "feelings_get_state" not in DEFERRED_CALLBACK_TOOLS


def test_public_activity_delta_carries_the_deferred_callback_anchor_as_a_typed_boolean():
    from workers_projects_runtime.conversation_provider import _public_activity_delta_fields

    fields = _public_activity_delta_fields(
        {
            "event_type": "tool",
            "payload_json": json.dumps(
                {
                    "tool": "connected_tool",
                    "task": "worker delegate once",
                    "status": "completed",
                    "expects_deferred_callback": True,
                    "source_event_id": "codex-cli:12:0",
                }
            ),
        }
    )
    activity = fields["provider_specific_fields"]["viventium"]["activity"]
    assert activity["expects_deferred_callback"] is True
    plain = _public_activity_delta_fields(
        {
            "event_type": "tool",
            "payload_json": json.dumps(
                {"tool": "connected_tool", "task": "feelings get state", "status": "completed", "expects_deferred_callback": "yes"}
            ),
        }
    )
    assert "expects_deferred_callback" not in plain["provider_specific_fields"]["viventium"]["activity"]


def test_deferred_callback_anchor_requires_glasshive_provenance():
    """A foreign MCP server exposing the same tool name must never arm host-side listening."""
    from workers_projects_runtime.mcp_tool_registry import (
        DEFERRED_CALLBACK_TOOLS,
        connected_tool_expects_deferred_callback,
    )

    # The exact identifier the installed claude harness emitted for a live delegation:
    # the host brokers GlassHive's tool and appends the origin server as an `_mcp_` suffix.
    assert connected_tool_expects_deferred_callback(
        "mcp__glasshive-user-capabilities__worker_delegate_once_mcp_glasshive-workers-projects"
    )
    assert connected_tool_expects_deferred_callback(
        "mcp__glasshive-user-capabilities__gh_glasshive__worker_delegate_once"
    )
    # A brokered request/response tool stays out even with full GlassHive provenance.
    assert not connected_tool_expects_deferred_callback(
        "mcp__glasshive-user-capabilities__workspace_status_mcp_glasshive-workers-projects"
    )
    # The origin-server suffix alone is enough provenance, and a foreign origin is refused.
    assert connected_tool_expects_deferred_callback("worker_run_mcp_glasshive-workers-projects")
    assert not connected_tool_expects_deferred_callback("worker_run_mcp_acme-tools")
    assert connected_tool_expects_deferred_callback(
        "mcp__glasshive-workers-projects__worker_run"
    )
    assert connected_tool_expects_deferred_callback(
        "worker_delegate_once", server="glasshive-user-capabilities"
    )
    # Foreign server, same bare tool name.
    assert not connected_tool_expects_deferred_callback("mcp__acme-tools__worker_run")
    assert not connected_tool_expects_deferred_callback("worker_run", server="acme-tools")
    # No provenance at all fails closed.
    assert not connected_tool_expects_deferred_callback("worker_run")
    # Synchronous operator-URL lookup delivers nothing later.
    assert "worker_takeover" not in DEFERRED_CALLBACK_TOOLS
    assert not connected_tool_expects_deferred_callback(
        "mcp__glasshive-workers-projects__worker_takeover"
    )


def test_codex_activity_uses_the_invocation_server_for_provenance():
    def codex_end(server: str, tool: str, call_id: str) -> str:
        return json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": call_id,
                    "invocation": {"server": server, "tool": tool, "arguments": {}},
                    "result": {"Ok": {"content": [], "isError": False}},
                },
            }
        )

    stdout = "\n".join(
        [
            codex_end("glasshive-workers-projects", "worker_delegate_once", "c1"),
            codex_end("acme-tools", "worker_run", "c2"),
        ]
    ) + "\n"
    payloads = [event["payload"] for event in _normalized_harness_activity("codex-cli", stdout)]
    assert payloads[0].get("expects_deferred_callback") is True
    assert "expects_deferred_callback" not in payloads[1]


def test_visible_message_identity_changes_when_same_source_id_is_corrected():
    from workers_projects_runtime.conversation_provider import _visible_message_keys
    def keys(text):
        return _visible_message_keys(
            [ChatMessage(role="user", content=text)],
            [{"id": "source-user", "role": "user", "sha256": hashlib.sha256(text.encode()).hexdigest()}],
        )
    assert keys("Wait for approval") != keys("Approval has arrived")
    assert keys("Wait for approval") == keys("Wait for approval")


@pytest.mark.parametrize("current_tail", [[], [ChatMessage(role="assistant", content="Checking the source."), ChatMessage(role="tool", content="Source result.")]])
def test_mixed_history_delta_retains_reused_current_turn(current_tail):
    from workers_projects_runtime.conversation_provider import _admit_conversation_history
    messages = [
        ChatMessage(role="user", content="Earlier request."),
        ChatMessage(role="assistant", content="Newly observed historical answer."),
        ChatMessage(role="user", content="Recheck the attached source."),
        *current_tail,
    ]
    unseen = {1}
    instruction, decision, _ = _admit_conversation_history(
        messages, start_at=0, turn_context="", model=next(iter(GLASSHIVE_MODELS.values())),
        include_indices=unseen,
    )
    historical, current = instruction.split("<viventium_current_accepted_turn_v1>", 1)
    assert "Newly observed historical answer." in historical
    assert "Recheck the attached source." in current
    assert "Newly observed historical answer." not in current
    assert set(range(1, len(messages))).issubset(decision["admitted_message_indices"])
    assert unseen == {1}


def test_core_source_history_is_whole_even_outside_recent_turn_window():
    from workers_projects_runtime.conversation_provider import _admit_conversation_history
    source = "Original background. " * 1400 + " Written clearance is still required."
    messages = [ChatMessage(role="user", content=source), ChatMessage(role="assistant", content="I will wait.")]
    for index in range(3):
        messages.extend([ChatMessage(role="user", content=f"Later {index}"), ChatMessage(role="assistant", content="Recorded.")])
    messages.append(ChatMessage(role="user", content="What remains required?"))
    instruction, decision, _ = _admit_conversation_history(
        messages, start_at=0, turn_context="", model=next(iter(GLASSHIVE_MODELS.values())), protected_indices={0, 1},
    )
    assert source in instruction
    assert {0, 1}.issubset(decision["admitted_message_indices"])
    assert instruction.index(source) < instruction.index("<viventium_current_accepted_turn_v1>")


def test_core_source_larger_than_native_budget_fails_without_partial_admission():
    from fastapi import HTTPException
    from workers_projects_runtime.conversation_provider import _admit_conversation_history
    with pytest.raises(HTTPException) as error:
        _admit_conversation_history(
            [ChatMessage(role="user", content="Protected " * 300000), ChatMessage(role="assistant", content="Accepted."), ChatMessage(role="user", content="Continue.")],
            start_at=0, turn_context="", model=next(iter(GLASSHIVE_MODELS.values())), protected_indices={0, 1},
        )
    assert error.value.status_code == 413


def test_core_message_carrier_preserves_sources_across_selected_sibling_branches(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    source = "Original long context. " * 1600 + " Wait for written clearance from Rowan."
    prior = [{"role": "user", "content": source}, {"role": "assistant", "content": "I will wait."}]
    def send(number, history, ids):
        payload = _payload(workspace)
        payload["messages"] = [{"role": "system", "content": f"Current dynamic context {number}"}, *history]
        chain = [{"id": identity, "role": message["role"], "sha256": hashlib.sha256(message["content"].encode()).hexdigest(), "accepted_source": identity.startswith("prior-")}
                 for identity, message in zip(ids, history)]
        payload["metadata"].update({
            "message_id": f"response-{number}", "idempotency_key": f"request-{number}",
            "main_context_protocol": "main_context_v1", "main_context_owner": "core",
            "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": str(number) * 64,
            "main_context_epoch": "a" * 64, "continuity_domain_id": "b" * 64,
            "continuity_agent_id": "agent-main", "logical_turn_id": f"turn-{number}",
            "visible_message_chain": chain,
        })
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200, response.text
        return client.app.state.store.get_provider_request(response.json()["id"])
    first = send(1, [*prior, {"role": "user", "content": "What remains required?"}], ["prior-user", "prior-answer", "current-1"])
    assert source in first["admitted_instruction"]
    first_decision = json.loads(first["replay_decision_json"])
    assert first_decision["admission_state"] == "accepted"
    second = send(2, [*prior, {"role": "user", "content": "Continue reviewing."}], ["prior-user", "prior-answer", "current-2"])
    assert second["session_id"] == first["session_id"]
    # Each selected chain omits the previous native answer, so this is a sibling branch.
    assert source in second["admitted_instruction"]
    assert json.loads(second["replay_decision_json"])["mode"] == "bootstrap"
    assert "Continue reviewing." in second["admitted_instruction"]
    corrected = source + " Correction: only review the document, do not contact Rowan."
    third = send(3, [{"role": "user", "content": corrected}, prior[1], {"role": "user", "content": "What changed?"}], ["prior-user", "prior-answer", "current-3"])
    assert third["session_id"] == first["session_id"]
    assert corrected in third["admitted_instruction"]
    keys = json.loads(third["replay_decision_json"])["admitted_visible_message_keys"]
    assert any(key.startswith("msg:prior-user:") for key in keys)
    regenerated = send(4, [{"role": "user", "content": corrected}, prior[1],
                           {"role": "user", "content": "What changed?"}],
                       ["prior-user", "prior-answer", "current-3"])
    assert regenerated["session_id"] == third["session_id"]
    assert "What changed?" in regenerated["admitted_instruction"]
    assert "<viventium_current_accepted_turn_v1>" in regenerated["admitted_instruction"]
    assert corrected in regenerated["admitted_instruction"]
    assert json.loads(regenerated["replay_decision_json"])["mode"] == "bootstrap"
    assert json.loads(regenerated["replay_decision_json"])["admitted_visible_message_keys"] == []


def test_blocking_response_waits_for_the_run_processor_to_accept_the_turn(tmp_path, monkeypatch):
    """A blocking completion is released only after its accepted-turn commit.

    The run processor commits the terminal request and then advances the session inside the
    provider sync lock. The blocking waiter also arbitrates the response deadline outside
    that lock. Park each thread where the scheduler can already park it: the waiter right
    after a sync that saw the run still active, and the processor between its terminal
    commit and its acceptance transaction.
    """
    waiter_synced = threading.Event()
    terminal_committed = threading.Event()
    release_processor = threading.Event()
    waiter = threading.local()

    class RunEndsDuringWaiterPoll(StubRuntime):
        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            output = super().run_task(worker, instruction, timeout_sec, run_id)
            waiter_synced.wait(timeout=10)
            return output

    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, RunEndsDuringWaiterPoll())
    provider = client.app.state.conversation_provider
    store = client.app.state.store
    assert provider.store is store
    sync_lock = provider._sync_lock
    original_wait = provider.wait
    original_sync = provider._sync
    original_advance = store.advance_provider_session_history

    class ContendedSyncLock:
        def acquire(self, blocking=True, timeout=-1):
            if sync_lock.acquire(blocking=False):
                return True
            # Another thread needs the lock the parked processor holds.
            release_processor.set()
            return sync_lock.acquire(blocking, timeout)

        def release(self):
            sync_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc_info):
            self.release()

    def marked_wait(request_id, **kwargs):
        waiter.active = True
        try:
            return original_wait(request_id, **kwargs)
        finally:
            waiter.active = False

    def parked_waiter_sync(request_record):
        result = original_sync(request_record)
        if (getattr(waiter, "active", False) and not waiter_synced.is_set()
                and result["state"] in {"queued", "running"}):
            waiter_synced.set()
            terminal_committed.wait(timeout=10)
        return result

    def parked_acceptance(session_id, **kwargs):
        if not getattr(waiter, "active", False) and not terminal_committed.is_set():
            terminal_committed.set()
            release_processor.wait(timeout=10)
        return original_advance(session_id, **kwargs)

    monkeypatch.setattr(provider, "_sync_lock", ContendedSyncLock())
    monkeypatch.setattr(provider, "wait", marked_wait)
    monkeypatch.setattr(provider, "_sync", parked_waiter_sync)
    monkeypatch.setattr(store, "advance_provider_session_history", parked_acceptance)
    source = "Wait for written clearance from Rowan."
    payload = _payload(workspace)
    payload["messages"] = [{"role": "system", "content": "Current dynamic context"},
                           {"role": "user", "content": source}]
    payload["metadata"].update({
        "message_id": "response-1", "idempotency_key": "request-1",
        "main_context_protocol": "main_context_v1", "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "c" * 64,
        "main_context_epoch": "a" * 64, "continuity_domain_id": "b" * 64,
        "continuity_agent_id": "agent-main", "logical_turn_id": "turn-1",
        "visible_message_chain": [{"id": "current-1", "role": "user", "accepted_source": True,
                                   "sha256": hashlib.sha256(source.encode()).hexdigest()}],
    })
    try:
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200, response.text
        row = store.get_provider_request(response.json()["id"])
    finally:
        release_processor.set()
    assert waiter_synced.is_set() and terminal_committed.is_set()
    assert row["state"] == "completed"
    assert json.loads(row["replay_decision_json"])["admission_state"] == "accepted"


class RunUsageRuntime(StubRuntime):
    """Native log per run: its answer and prompt-token usage (high for one chosen run)."""

    def __init__(self, high_usage_run=None):
        super().__init__()
        self.high_usage_run = high_usage_run
        self.ordinals = {}

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        self.ordinals.setdefault(str(run_id), len(self.ordinals) + 1)
        return super().run_task(worker, instruction, timeout_sec, run_id)

    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        ordinal = self.ordinals.get(str(run_id))
        if ordinal is None:
            return "codex-cli", ""
        tokens = 250_000 if ordinal == self.high_usage_run else 17
        return "codex-cli", "\n".join([
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": f"Native answer {ordinal}."}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": 4}}),
        ])


def _continue_conversation(client, workspace, history, number, user):
    history.append({"role": "user", "content": user})
    payload = _payload(workspace)
    payload["messages"] = [payload["messages"][0], *history]
    payload["metadata"].update({"message_id": f"response-{number}", "idempotency_key": f"request-{number}",
                                "stream_id": f"stream-{number}"})
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    history.append({"role": "assistant", "content": response.json()["choices"][0]["message"]["content"]})
    return client.app.state.store.get_provider_request(response.json()["id"])


def test_late_settlement_pass_keeps_the_next_turns_native_context_rotation(tmp_path, monkeypatch):
    """A turn's session write belongs to the pass that settles it, never to a later pass.

    The run processor reads turn 2 as pending, then the blocking waiter settles turn 2, so the
    processor's pass runs late. Park that pass where the scheduler or a slow native-log read can
    already park it, after it read the session and before it would write it, until turn 3 has
    been admitted. Turn 3 is admitted under measured native pressure and rotates the epoch.
    """
    processor_read_pending = threading.Event()
    waiter_returned = threading.Event()
    next_turn_admitted = threading.Event()
    parked = threading.Event()
    local = threading.local()
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, RunUsageRuntime(high_usage_run=2))
    provider = client.app.state.conversation_provider
    store = client.app.state.store
    original_wait, original_sync, original_start = provider.wait, provider._sync, provider.start
    original_output = provider._native_output_snapshot

    def marked_wait(request_id, **kwargs):
        local.waiter = True
        try:
            result = original_wait(request_id, **kwargs)
        finally:
            local.waiter = False
        if result[0]["message_id"] == "response-2":
            waiter_returned.set()
        return result

    def ordered_sync(request_record):
        if request_record["message_id"] != "response-2":
            return original_sync(request_record)
        if getattr(local, "waiter", False):
            processor_read_pending.wait(timeout=10)
            return original_sync(request_record)
        if threading.current_thread().name.startswith("wpr-") and not processor_read_pending.is_set():
            processor_read_pending.set()
            waiter_returned.wait(timeout=10)
            local.late = True
            try:
                return original_sync(request_record)
            finally:
                local.late = False
        return original_sync(request_record)

    def slow_output_read(request_record, run):
        if getattr(local, "late", False) and not parked.is_set():
            parked.set()
            next_turn_admitted.wait(timeout=10)
        return original_output(request_record, run)

    def marked_start(payload, **kwargs):
        result = original_start(payload, **kwargs)
        if payload.metadata.message_id == "response-3":
            next_turn_admitted.set()
        return result

    monkeypatch.setattr(provider, "wait", marked_wait)
    monkeypatch.setattr(provider, "_sync", ordered_sync)
    monkeypatch.setattr(provider, "_native_output_snapshot", slow_output_read)
    monkeypatch.setattr(provider, "start", marked_start)
    history = []
    try:
        _continue_conversation(client, workspace, history, 1, "Plan the launch checklist.")
        _continue_conversation(client, workspace, history, 2, "Add the security review.")
        third = _continue_conversation(client, workspace, history, 3, "Summarize the open items.")
    finally:
        processor_read_pending.set()
        waiter_returned.set()
        next_turn_admitted.set()
    assert parked.is_set()
    rotation = json.loads(third["replay_decision_json"])
    assert rotation["native_context_transition"]["reason"] == "native_occupancy_pressure"
    manifest = json.loads(store.get_provider_session_by_id(third["session_id"])["context_manifest_json"])
    assert manifest["native_context_epoch"] == rotation["native_context_epoch"] != ""
    assert manifest["latest_native_prompt_tokens"] == 17
    fourth = json.loads(_continue_conversation(
        client, workspace, history, 4, "What is still blocking?")["replay_decision_json"])
    assert fourth["native_context_epoch"] == rotation["native_context_epoch"]
    assert fourth["native_occupancy"]["state"] == "healthy"
    assert fourth["mode"] == "delta"


def test_reading_an_earlier_turns_activity_keeps_the_session_on_its_latest_turn(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, RunUsageRuntime(high_usage_run=2))
    store = client.app.state.store
    history = []
    _continue_conversation(client, workspace, history, 1, "Plan the launch checklist.")
    second = _continue_conversation(client, workspace, history, 2, "Add the security review.")
    third = _continue_conversation(client, workspace, history, 3, "Summarize the open items.")
    assert json.loads(third["replay_decision_json"])["native_context_transition"]["reason"] == (
        "native_occupancy_pressure")
    activity = client.get(f"/v1/requests/{second['request_id']}/activity", headers=AUTH)
    assert activity.status_code == 200, activity.text
    manifest = json.loads(store.get_provider_session_by_id(third["session_id"])["context_manifest_json"])
    assert manifest["last_request_id"] == third["request_id"]
    fourth = json.loads(_continue_conversation(
        client, workspace, history, 4, "What is still blocking?")["replay_decision_json"])
    assert fourth["native_tool_evidence_coverage"] == "complete"


def test_reserved_source_stays_in_next_instruction_until_delivery_is_accepted(tmp_path, monkeypatch):
    from workers_projects_runtime.conversation_provider import _visible_message_keys
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    initial_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=initial_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    first_request = store.get_provider_request(first.json()["id"])
    source = "Keep the draft internal until written approval arrives."
    chain = [{"id": "source-user", "role": "user", "sha256": hashlib.sha256(source.encode()).hexdigest(), "accepted_source": True}]
    key = _visible_message_keys([ChatMessage(role="user", content=source)], chain)[0]
    pending, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=first_request["session_id"],
        idempotency_key="still-pending", message_id="pending-answer", stream_id="", requested_history_count=2,
        replay_decision={"main_context_protocol": "main_context_v1", "logical_turn_id": "pending-turn",
                         "advancement_key": "pending-turn:1", "logical_turn_revision": 1,
                         "admitted_visible_message_keys": [key]},
        admitted_instruction=source,
    )
    payload = _payload(workspace)
    payload["messages"] = [*initial_payload["messages"], {"role": "user", "content": source},
                           {"role": "assistant", "content": "Recorded."}, {"role": "user", "content": "What remains required?"}]
    payload["metadata"].update({
        "message_id": "next-answer", "idempotency_key": "next-request",
        "main_context_protocol": "main_context_v1", "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "c" * 64,
        "main_context_epoch": "a" * 64, "continuity_domain_id": "b" * 64,
        "continuity_agent_id": "agent-main", "logical_turn_id": "next-turn", "visible_message_chain": chain,
    })
    # Retain the existing physical worker so this exercises delta admission.
    session = store.get_provider_session_by_id(first_request["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    store.update_provider_session_history(session["session_id"], history_count=session["history_count"],
                                          context_manifest={**manifest, "stable_authority_sha256": "a" * 64})
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    request = store.get_provider_request(response.json()["id"])
    assert source in request["admitted_instruction"]
    assert key not in json.loads(request["replay_decision_json"])["admitted_visible_message_keys"]
    store.update_provider_request(pending["request_id"], state="failed")
    # Failure releases ownership; it cannot retroactively remove the context already delivered above.
    assert key not in store.get_provider_session_admission_state(session["session_id"])["reserved_visible_message_keys"]


def test_core_body_manifest_keeps_long_history_identity_with_bounded_entry_validation(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    history = [{"role": "user", "content": f"Synthetic turn {index}"} for index in range(260)]
    chain = [{"id": f"source-{index}", "role": "user", "sha256": hashlib.sha256(item["content"].encode()).hexdigest(), "accepted_source": index == 259}
             for index, item in enumerate(history)]
    payload["messages"] = history
    payload["metadata"].update({
        "main_context_protocol": "main_context_v1", "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "c" * 64,
        "main_context_epoch": "a" * 64, "continuity_domain_id": "b" * 64,
        "continuity_agent_id": "agent-main", "logical_turn_id": "long-history-turn",
        "visible_message_chain": chain,
    })
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    row = client.app.state.store.get_provider_request(response.json()["id"])
    assert "Synthetic turn 259" in row["admitted_instruction"]
    keys = json.loads(row["replay_decision_json"])["admitted_visible_message_keys"]
    assert any(key.startswith("msg:source-259:") for key in keys)
    for invalid_chain in [chain + [chain[-1]], [{**chain[0], "sha256": "invalid"}], [{**chain[0], "id": "x" * 161}]]:
        payload["metadata"]["visible_message_chain"] = invalid_chain
        rejected = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert rejected.status_code == 400, rejected.text


class BlockingFamilyStopRuntime(StubRuntime):
    """Hold one real service run until Stop reaches the native boundary."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.interrupt_calls = []

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        _publish_in_process_test_start(worker)
        self.started.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("Synthetic native run was not released")
        return json.dumps({
            "type": "assistant_response", "content": "The requested detailed answer.",
            "tool_name": None,
        })

    def interrupt_worker(self, worker, run_id=None):
        self.interrupt_calls.append((worker["worker_id"], run_id))
        self.release.set()
        return super().interrupt_worker(worker)


@pytest.mark.parametrize("first_scope", [("external_user", "interactive"), ("system", "scheduler"), ("worker", "callback")])
def test_trusted_authoring_scopes_overlap_without_replacing_native_sessions(tmp_path, monkeypatch, first_scope):
    runtime = BlockingFamilyStopRuntime()
    entered = []
    both_entered = threading.Event()
    original_run = runtime.run_task

    def record_run(worker, instruction, timeout_sec=None, run_id=None):
        entered.append((worker["worker_id"], instruction))
        if len(entered) == 2:
            both_entered.set()
        return original_run(worker, instruction, timeout_sec, run_id)

    monkeypatch.setattr(runtime, "run_task", record_run)
    client = _scoped_client(tmp_path, monkeypatch, runtime, trust_identity_headers=True)
    store = client.app.state.store
    scopes = [first_scope, ("system", "scheduler") if first_scope[0] == "external_user" else ("external_user", "interactive")]
    payloads = []
    headers = []
    for index, (actor, origin) in enumerate(scopes):
        payload = _payload(tmp_path)
        payload["messages"][0]["content"] = f"Synthetic {origin} authority."
        payload["metadata"].update(idempotency_key=f"scope-{index}", message_id=f"scope-message-{index}")
        payloads.append(payload)
        headers.append({**AUTH, "X-Viventium-Actor-Kind": actor, "X-Viventium-Origin": origin})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/v1/chat/completions", headers=headers[0], json=payloads[0])
        second = None
        try:
            assert runtime.started.wait(timeout=5)
            second = pool.submit(client.post, "/v1/chat/completions", headers=headers[1], json=payloads[1])
            assert both_entered.wait(timeout=3), second.result(timeout=1).text
            assert len(set(worker for worker, _ in entered)) == 2
            sessions = store.list_provider_sessions(owner_id="owner-a")
            assert {(s["actor_kind"], s["origin"]) for s in sessions} == set(scopes)
            assert len({s["session_id"] for s in sessions}) == 2
            assert len({s["conversation_id"] for s in sessions}) == 1
            assert len({s["agent_id"] for s in sessions}) == 1
            assert all(store.get_worker(s["worker_id"])["state"] != "terminated" for s in sessions)
            assert runtime.interrupt_calls == []
        finally:
            runtime.release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 200

    # Each origin resumes its own worker and history after both concurrent runs finish.
    for index, scope in enumerate(scopes):
        previous = next(s for s in sessions if (s["actor_kind"], s["origin"]) == scope)
        followup = json.loads(json.dumps(payloads[index]))
        followup["metadata"].update(idempotency_key=f"followup-{index}", message_id=f"followup-message-{index}")
        followup["messages"].extend([
            {"role": "assistant", "content": "The requested detailed answer."},
            {"role": "user", "content": "Continue with the next part."},
        ])
        result = client.post("/v1/chat/completions", headers=headers[index], json=followup)
        assert result.status_code == 200, result.text
        current = store.get_provider_session_by_id(previous["session_id"])
        assert current["worker_id"] == previous["worker_id"]
        assert current["history_count"] > previous["history_count"]
        assert entered[-1][0] == previous["worker_id"]


@pytest.mark.parametrize("carrier", ["headers", "body"])
@pytest.mark.parametrize("scope", [("system", "scheduler"), ("worker", "callback")])
def test_untrusted_provider_cannot_claim_noninteractive_authoring_scope(tmp_path, monkeypatch, carrier, scope):
    client = _scoped_client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    headers = dict(AUTH)
    if carrier == "headers":
        headers.update({"X-Viventium-Actor-Kind": scope[0], "X-Viventium-Origin": scope[1]})
    else:
        payload["metadata"].update(actor_kind=scope[0], origin=scope[1])
    response = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert response.status_code == 403, response.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_trusted_authoring_scope_rejects_conflicting_body_and_header(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch, trust_identity_headers=True)
    payload = _payload(tmp_path)
    payload["metadata"].update(actor_kind="external_user", origin="interactive")
    result = client.post("/v1/chat/completions", json=payload, headers={
        **AUTH, "X-Viventium-Actor-Kind": "system", "X-Viventium-Origin": "scheduler",
    })
    assert result.status_code == 403, result.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_context_recovery_preserves_scheduled_session_scope(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, InterruptCountingRuntime(), reconcile_on_startup=False)
    provider = ConversationProvider(store, service)
    try:
        project = store.create_project("owner-a", "Synthetic recovery", "Keep authoring scope", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"], owner_id="owner-a", name="Scheduled worker",
            role="conversation-agent", profile="codex-cli", backend="", runtime="codex-cli", model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local", owner_id="owner-a", conversation_id="scoped-recovery", agent_id="agent-a",
            actor_kind="system", origin="scheduler", model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"], worker_id=worker["worker_id"], workspace_dir=str(tmp_path), access_mode="workspace",
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "Exact admitted source", state="queued")
        store.update_run(run["run_id"], state="failed", failure_class="provider_context_limit_exceeded", failure_structured=1)
        request, _ = store.create_provider_request(
            tenant_id="local", owner_id="owner-a", session_id=session["session_id"], idempotency_key="scoped-recovery",
            message_id="message-a", stream_id="stream-a", requested_history_count=1, admitted_instruction="Exact admitted source",
        )
        request = store.update_provider_request(request["request_id"], run_id=run["run_id"], state="running")
        result = provider._start_context_recovery(request, store.get_run(run["run_id"]))
        current = store.get_provider_session_by_id(session["session_id"])
        assert result["session_id"] == session["session_id"]
        assert result["run_id"] != run["run_id"], result
        assert (current["actor_kind"], current["origin"]) == ("system", "scheduler")
        assert current["worker_id"] != worker["worker_id"]
        assert store.get_run(result["run_id"])["instruction"] == "Exact admitted source"
        assert len(store.list_provider_sessions(owner_id="owner-a")) == 1
    finally:
        provider.shutdown()
        service.shutdown()
        store.close()


def test_changed_authority_preserves_active_turn_and_retries_after_completion(tmp_path, monkeypatch):
    runtime = BlockingFamilyStopRuntime()
    retired = []
    terminate = runtime.terminate_worker

    def observed_termination(worker):
        retired.append(worker["worker_id"])
        runtime.release.set()
        return terminate(worker)

    monkeypatch.setattr(runtime, "terminate_worker", observed_termination)
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    provider = client.app.state.conversation_provider
    store = client.app.state.store
    payload = _payload(tmp_path)
    first = provider.start(ChatCompletionRequest.model_validate(payload))
    try:
        assert runtime.started.wait(timeout=5)
        assert store.get_run(first["run_id"])["state"] == "running"
        first_session = store.get_provider_session_by_id(first["session_id"])
        worker_id = first_session["worker_id"]
        changed = _payload(tmp_path)
        changed["messages"][0]["content"] = "Changed synthetic background authority."
        changed["metadata"].update(message_id="background-message", idempotency_key="background-turn")

        blocked = client.post("/v1/chat/completions", headers=AUTH, json=changed)

        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "conversation_session_authority_conflict"
        assert retired == []
        assert store.get_run(first["run_id"])["state"] == "running"
        assert provider._sync(first)["state"] == "running"
        assert store.get_provider_session_by_id(first["session_id"])["worker_id"] == worker_id
        assert store.get_provider_request(
            tenant_id="local", owner_id="owner-a", idempotency_key="background-turn",
        ) is None

        other_payload = _payload(tmp_path)
        other_payload["metadata"]["owner_id"] = "owner-b"
        other = provider.start(ChatCompletionRequest.model_validate(other_payload))
        assert other["owner_id"] == "owner-b"
        assert other["session_id"] != first["session_id"]
        assert retired == []

        runtime.release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if provider._sync(first)["state"] == "completed" and provider._sync(other)["state"] == "completed":
                break
            time.sleep(0.01)
        assert provider._sync(first)["state"] == "completed"
        assert provider._sync(other)["state"] == "completed"
        retried = client.post("/v1/chat/completions", headers=AUTH, json=changed)
        assert retried.status_code == 200, retried.text
        assert retired == [worker_id]
        current = store.get_provider_session_by_id(first["session_id"])
        assert current["worker_id"] != worker_id
        current_worker = store.get_worker(current["worker_id"])
        assert json.loads(current_worker["bootstrap_bundle_json"])["developer_instructions"] == (
            "Changed synthetic background authority."
        )
        assert store.get_run(first["run_id"])["state"] == "completed"
    finally:
        runtime.release.set()


def _family_stop_payload(workspace):
    payload = _payload(workspace)
    payload["tools"] = [{
        "type": "function",
        "function": {
            "name": "lc_transfer_to_specialist",
            "description": "Consult the specialist using shared graph state.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }]
    return payload


@pytest.mark.parametrize("audio_eligible", [False, True])
def test_graph_family_stop_interrupts_exact_active_child(tmp_path, monkeypatch, audio_eligible):
    runtime = BlockingFamilyStopRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    provider = client.app.state.conversation_provider
    store = client.app.state.store
    payload = _family_stop_payload(tmp_path)
    payload["metadata"]["audio_eligible"] = audio_eligible
    try:
        record = provider.start(ChatCompletionRequest.model_validate(payload))
        assert runtime.started.wait(timeout=5)
        assert ":graph:" in record["idempotency_key"]
        assert store.get_run(record["run_id"])["state"] == "running"
        foreign = client.post(
            "/v1/requests/by-idempotency/idem-a/cancel",
            headers={**AUTH, "X-Viventium-User-Id": "owner-b"},
        )
        assert foreign.status_code == 200
        assert foreign.json()["id"] == ""
        assert runtime.interrupt_calls == []
        assert store.get_run(record["run_id"])["state"] == "running"

        stopped = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)

        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["id"] == record["request_id"]
        assert stopped.json()["state"] == "cancelled"
        session = store.get_provider_session_by_id(record["session_id"])
        assert runtime.interrupt_calls == [(session["worker_id"], record["run_id"])]
        assert store.get_run(record["run_id"])["state"] == "cancelled"
        assert provider._sync(record)["state"] == "cancelled"
        assert not store.get_provider_request(record["request_id"])["response_json"]
    finally:
        runtime.release.set()


@pytest.mark.parametrize("completed_child", [False, True])
def test_graph_family_stop_survives_restart_and_isolates_new_turn_and_owner(
    tmp_path, monkeypatch, completed_child,
):
    runtime = BlockingFamilyStopRuntime()
    runtime.release.set()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _family_stop_payload(tmp_path)
    if completed_child:
        completed = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert completed.status_code == 200, completed.text
    stopped = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
    assert stopped.status_code == 200

    # A fresh provider has no in-process cancellation map. The same DB owns Stop.
    restarted = _client(tmp_path, monkeypatch, runtime=runtime)
    payload["messages"].append({"role": "user", "content": "A later graph execution."})
    for _ in range(2):
        late = restarted.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert late.status_code == 409, late.text
        assert "cancelled before native execution" in late.text
    store = restarted.app.state.store
    if not completed_child:
        assert store.list_provider_sessions(owner_id="owner-a") == []
    assert not store.is_provider_stop_tombstone_active(
        tenant_id="another-tenant", owner_id="owner-a",
        idempotency_keys=(_versioned_idempotency_key("idem-a", audio_eligible=False),),
    )

    other_owner = json.loads(json.dumps(payload))
    other_owner["metadata"]["owner_id"] = "owner-b"
    other = restarted.post(
        "/v1/chat/completions", json=other_owner,
        headers={**AUTH, "X-Viventium-User-Id": "owner-b"},
    )
    assert other.status_code == 200, other.text

    new_turn = json.loads(json.dumps(payload))
    new_turn["metadata"].update(idempotency_key="idem-next-turn", message_id="next-message")
    continued = restarted.post("/v1/chat/completions", headers=AUTH, json=new_turn)
    assert continued.status_code == 200, continued.text
    assert continued.json()["choices"][0]["message"]["content"]


def test_graph_family_stop_during_atomic_admission_returns_conflict(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = client.app.state.store
    create = store.create_provider_request

    def stop_then_create(**kwargs):
        store.upsert_provider_stop_tombstone(
            tenant_id=kwargs["tenant_id"], owner_id=kwargs["owner_id"],
            base_idempotency_key=kwargs["base_idempotency_key"], ttl_seconds=60,
        )
        return create(**kwargs)

    monkeypatch.setattr(store, "create_provider_request", stop_then_create)
    response = client.post(
        "/v1/chat/completions", headers=AUTH, json=_family_stop_payload(tmp_path),
    )
    assert response.status_code == 409, response.text
    assert "cancelled before native execution" in response.text
    for session in store.list_provider_sessions(owner_id="owner-a"):
        assert store.list_runs_for_worker(session["worker_id"]) == []


@pytest.mark.parametrize("initial", [None, "true"])
def test_conversation_auto_memory_policy_replaces_old_worker_then_resumes(tmp_path, monkeypatch, initial):
    policy_env = "WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY"
    monkeypatch.delenv(policy_env, raising=False)
    if initial is not None:
        monkeypatch.setenv(policy_env, initial)
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace, model="claude-code:opus")
    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert first.status_code == 200, first.text
    old = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    monkeypatch.setenv(policy_env, "false")
    payload["metadata"].update(message_id="message-new-policy", idempotency_key="new-policy")
    second = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != old["worker_id"]
    assert client.app.state.store.get_worker(old["worker_id"])["state"] == "terminated"
    payload["metadata"].update(message_id="message-same-policy", idempotency_key="same-policy")
    third = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert third.status_code == 200, third.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]["worker_id"] == current["worker_id"]


def test_claude_auto_memory_policy_does_not_change_other_native_identity(monkeypatch):
    from workers_projects_runtime.conversation_provider import _native_policy_sha256
    model = GLASSHIVE_MODELS["codex-cli:gpt-5.6-sol"]
    monkeypatch.delenv("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", raising=False)
    original = _native_policy_sha256(model)
    monkeypatch.setenv("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", "false")
    assert _native_policy_sha256(model) == original


@pytest.mark.parametrize("signed_in, expected", [(False, "authentication_required"), (True, "ready")])
def test_luna_uses_existing_codex_auth_readiness(monkeypatch, signed_in, expected):
    profiles = []
    monkeypatch.setattr(
        "workers_projects_runtime.conversation_provider._configured_binary", lambda profile: "/installed/codex"
    )
    def auth(profile):
        profiles.append(profile)
        return signed_in
    monkeypatch.setattr("workers_projects_runtime.conversation_provider._harness_auth_configured", auth)
    model = GLASSHIVE_MODELS["codex-cli:gpt-5.6-luna"]
    metadata = model.api_payload()
    assert metadata["native_model"] == "gpt-5.6-luna"
    assert metadata["recommended_effort"] == "medium"
    assert metadata["effort_choices"] == ["low", "medium", "high", "xhigh", "max"]
    assert metadata["context_window"] == 272000
    assert profiles == ["codex-cli"]
    assert metadata["readiness"]["status"] == expected
    assert metadata["readiness"]["authentication"] == ("configured" if signed_in else "required")
    assert model.harness_profile == GLASSHIVE_MODELS["codex-cli:gpt-5.6-sol"].harness_profile


@pytest.mark.parametrize("requested,expected", [("default", "default"), ("high", "high"), (None, "max")])
def test_claude_explicit_default_effort_preserves_native_policy(tmp_path, monkeypatch, requested, expected):
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-oauth-token")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setattr(HostClaudeCodeRuntime, "_effort_supported", lambda *_args: True)
    payload = ChatCompletionRequest(
        model="claude-code:opus", reasoning_effort=requested,
        messages=[ChatMessage(role="user", content="Explain the current local task.")],
        metadata={"glasshive_options": {"access": "full"}},
    )
    provider = ConversationProvider.__new__(ConversationProvider)
    model = GLASSHIVE_MODELS[payload.model]
    effort = provider._effort(payload, model)
    assert effort == expected
    bundle = provider._native_bundle(payload, model, effort)
    assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == expected
    assert bundle["provider_model"] == "opus"
    assert bundle["access_mode"] == "full"
    assert bundle["provider_capabilities"]["native_tools"] is True
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "runtime"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker = {"worker_id": "wrk_native_effort", "profile": "claude-code", "model": "opus",
              "execution_mode": "host", "trusted_run_lane": "conversation",
              "workspace_root": str(workspace), "bootstrap_bundle_json": json.dumps(bundle)}
    command, _ = runtime._build_command(worker, "Explain the current local task.", runtime._host_runtime_info(worker))
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    if expected == "default":
        assert "--effort" not in command
    else:
        assert command[command.index("--effort") + 1] == expected


def test_unknown_claude_effort_still_fails_closed():
    from fastapi import HTTPException
    provider = ConversationProvider.__new__(ConversationProvider)
    payload = ChatCompletionRequest(model="claude-code:opus", reasoning_effort="invented",
                                    messages=[ChatMessage(role="user",content="Hello.")])
    with pytest.raises(HTTPException) as failure:
        provider._effort(payload, GLASSHIVE_MODELS[payload.model])
    assert failure.value.status_code == 400


@pytest.mark.parametrize("model", ["codex-cli:gpt-5.6-sol", "claude-code:opus"])
def test_core_branch_replay_uses_fresh_native_context_without_retiring_worker(tmp_path, monkeypatch, model):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    store = client.app.state.store
    def send(number, history, ids, response_id=None):
        payload = _payload(workspace, model=model)
        payload["messages"] = [{"role": "system", "content": "Stable assistant authority."}, *history]
        payload["metadata"].update({
            "message_id": response_id or f"answer-{number}", "idempotency_key": f"branch-request-{number}",
            "main_context_protocol": "main_context_v1", "main_context_owner": "core",
            "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "b" * 64,
            "main_context_epoch": "c" * 64, "logical_turn_id": f"turn-{1 if number == 3 else number}",
            "continuity_domain_id": "d" * 64, "continuity_agent_id": "agent-main",
            "visible_message_chain": [{"id": identity, "role": message["role"],
                "sha256": hashlib.sha256(message["content"].encode()).hexdigest()}
                for identity, message in zip(ids, history)],
        })
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200, response.text
        record = store.get_provider_request(response.json()["id"])
        session = store.get_provider_session_by_id(record["session_id"])
        worker = store.get_worker(session["worker_id"])
        return record, worker, response.json()["choices"][0]["message"]["content"], payload
    question = {"role": "user", "content": "Evaluate the launch proposal."}
    first, worker, answer, _ = send(1, [question], ["original-question"])
    continued = [question, {"role": "assistant", "content": answer}, {"role": "user", "content": "Explain the recovery constraint."}]
    second, same_worker, _, _ = send(2, continued, ["original-question", "answer-1", "follow-up"])
    assert same_worker["worker_id"] == worker["worker_id"]
    assert not json.loads(same_worker["bootstrap_bundle_json"])["env"].get("GLASSHIVE_PROVIDER_SESSION_EPOCH")
    assert json.loads(second["replay_decision_json"])["mode"] == "delta"
    regenerated, rebound, _, payload = send(3, [question], ["original-question"])
    epoch = json.loads(rebound["bootstrap_bundle_json"])["env"]["GLASSHIVE_PROVIDER_SESSION_EPOCH"]
    assert len(epoch) == 64
    assert rebound["worker_id"] == worker["worker_id"]
    assert rebound["workspace_dir"] == worker["workspace_dir"]
    assert rebound["model"] == worker["model"]
    assert rebound["state"] not in {"terminated", "terminating"}
    assert json.loads(regenerated["replay_decision_json"])["mode"] == "bootstrap"
    assert question["content"] in regenerated["admitted_instruction"]
    assert answer not in regenerated["admitted_instruction"]
    duplicate = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert duplicate.json()["id"] == regenerated["request_id"]
    # Same authored response may revisit Main after a graph consultation; that is not a sibling branch.
    provider = client.app.state.conversation_provider
    current_payload = ChatCompletionRequest.model_validate(payload)
    same_session, reseeded = provider._session(current_payload, provider._model(model), workspace, "medium", tenant_id="local")
    resumed = store.get_worker(same_session["worker_id"])
    assert reseeded is False
    assert json.loads(resumed["bootstrap_bundle_json"])["env"]["GLASSHIVE_PROVIDER_SESSION_EPOCH"] == epoch
    assert store.get_worker(worker["worker_id"])["state"] not in {"terminated", "terminating"}


def test_core_branch_replay_cannot_change_an_active_native_context(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = ChatCompletionRequest.model_validate(_payload(workspace))
    provider = client.app.state.conversation_provider
    model = provider._model(payload.model)
    session, _ = provider._session(payload, model, workspace, "medium", tenant_id="local")
    manifest = json.loads(session["context_manifest_json"])
    client.app.state.store.update_provider_session_history(session["session_id"], history_count=2,
        context_manifest={**manifest,"native_context_response_key":"msg:prior-answer"})
    payload.metadata.main_context_protocol = "main_context_v1"
    payload.metadata.main_context_owner = "core"
    payload.metadata.message_id = "new-answer"
    payload.metadata.visible_message_chain = [{"id":"question","role":"user","sha256":hashlib.sha256(b"Hello from LIFE.").hexdigest()}]
    monkeypatch.setattr(client.app.state.store,"list_nonterminal_runs_for_worker",lambda _worker:[{"run_id":"still-running"}])
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        provider._session(payload,model,workspace,"medium",tenant_id="local")
    assert error.value.status_code == 409
    current = client.app.state.store.get_provider_session_by_id(session["session_id"])
    assert current["worker_id"] == session["worker_id"]
    assert current["history_count"] == 2
    assert "native_context_epoch" not in json.loads(current["context_manifest_json"])


@pytest.mark.parametrize("provider_path", [True, False])
def test_host_capacity_preserves_the_requested_protocol_error_contract(tmp_path, monkeypatch, provider_path):
    from workers_projects_runtime.openclaw_runtime import HostCapacityError

    client = _client(tmp_path, monkeypatch)
    def reject(*_args, **_kwargs):
        raise HostCapacityError(
            "Host resource headroom is below its configured admission guard.",
            capacity_class="resource_pressure",
            retry_after_s=7,
        )
    if provider_path:
        monkeypatch.setattr(client.app.state.conversation_provider, "start", reject)
        response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(tmp_path))
        assert response.status_code == 503
        assert response.json()["error"] == {
            "message": "Host resource headroom is below its configured admission guard.",
            "code": "host_capacity", "type": "server_error", "param": None,
        }
        assert "detail" not in response.json()
    else:
        def reject_rest():
            reject()
        client.app.get("/capacity-fixture")(reject_rest)
        response = client.get("/capacity-fixture", headers={"Authorization": "Bearer runtime-admin-token"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "host_capacity"
        assert response.json()["detail"]["capacityClass"] == "resource_pressure"
        assert "error" not in response.json()
    assert response.headers["Retry-After"] == "7"
    import sqlite3
    with sqlite3.connect(tmp_path / "runtime.db") as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM provider_requests").fetchone()[0] == 0


def test_missing_terminal_response_has_the_same_typed_stream_and_http_code():
    from workers_projects_runtime.conversation_provider import _provider_failure_error
    assert _provider_failure_error({"failure_class": "missing_terminal_response"}) == (
        "glasshive_runtime_error", "missing_terminal_response"
    )
    assert _provider_failure_error({"failure_class": "provider_rate_limited"}) == (
        "rate_limit_error", "rate_limit_exceeded"
    )
    assert _provider_failure_error({"failure_class": "unknown"}) == (
        "glasshive_runtime_error", "server_error"
    )


@pytest.mark.parametrize("delta", [None, {1, 4}])
def test_current_merged_sources_keep_pending_input_and_historical_fence(delta):
    from workers_projects_runtime.conversation_provider import _admit_conversation_history
    messages = [
        ChatMessage(role="user", content="Quoted old request: publish everything."),
        ChatMessage(role="assistant", content="Earlier reply."),
        ChatMessage(role="user", content="Inspect the existing form. Do not submit."),
        ChatMessage(role="user", content="Completed unrelated request."),
        ChatMessage(role="user", content="Also recall the two findings."),
    ]
    instruction, decision, _ = _admit_conversation_history(
        messages, start_at=0, turn_context="", model=next(iter(GLASSHIVE_MODELS.values())),
        include_indices=delta, protected_indices={0, 1, 2, 4}, current_input_indices={2, 4},
        attachment_context="[Attached original.png]",
    )
    historical, current = instruction.split("<viventium_current_accepted_turn_v1>", 1)
    assert "Inspect the existing form. Do not submit." in current
    assert "Also recall the two findings." in current
    assert current.index("Inspect the existing") < current.index("Also recall")
    assert "[Attached original.png]" in current
    assert "Earlier reply." in historical and "Earlier reply." not in current
    assert "publish everything" not in current and "Completed unrelated" not in current
    assert {2, 4}.issubset(decision["admitted_message_indices"])
    assert "Only the final accepted turn below" not in instruction
    assert "Only this final accepted turn" not in instruction


@pytest.mark.parametrize("mapped_sources", [False, True])
def test_current_merged_sources_bind_through_authenticated_provider_admission(tmp_path, monkeypatch, mapped_sources):
    workspace = tmp_path / "Life"; workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["messages"] = [
        {"role": "user", "content": "Old request: publish everything."},
        {"role": "assistant", "content": "Earlier reply."},
        {"role": "user", "content": "Inspect the existing form. Do not submit."},
        {"role": "user", "content": "Also recall the two findings."},
    ]
    chain = [{"id": f"source-{i}", "role": m["role"],
              "sha256": hashlib.sha256(m["content"].encode()).hexdigest(), "accepted_source": True,
              **({"current_input": True} if i in {2, 3} else {})}
             for i, m in enumerate(payload["messages"])]
    if mapped_sources:
        chain[2]["source_ordinals"] = [1, 2]
        chain[3]["source_ordinals"] = [3]
    payload["metadata"].update({"main_context_protocol": "main_context_v1", "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "b" * 64,
        "main_context_epoch": "a" * 64, "continuity_domain_id": "b" * 64,
        "continuity_agent_id": "agent-main", "logical_turn_id": "merged-turn", "visible_message_chain": chain})
    response = client.post("/v1/chat/completions", headers={**AUTH, "X-GlassHive-Fallback-Model": "claude-code:opus",
        "X-GlassHive-Fallback-Reasoning-Effort": "high"}, json=payload)
    assert response.status_code == 200, response.text
    row = client.app.state.store.get_provider_request(response.json()["id"])
    for instruction_field in ("admitted_instruction", "fallback_instruction"):
        history, current = row[instruction_field].split("<viventium_current_accepted_turn_v1>", 1)
        assert "Inspect the existing form. Do not submit." in current
        assert "Also recall the two findings." in current
        assert "Old request: publish everything." in history and "publish everything" not in current
        if mapped_sources:
            assert '<delegation_source>{"sourceOrdinals":[1,2]}</delegation_source>' in current
            assert '<delegation_source>{"sourceOrdinals":[3]}</delegation_source>' in current
        else:
            assert '<delegation_source>' not in current
    # Declared current sources require the existing authenticated Core context.
    payload["metadata"]["main_context_protocol"] = ""
    untrusted = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert untrusted.status_code == 403, untrusted.text
    payload["metadata"]["main_context_protocol"] = "main_context_v1"
    # The same declared input remains mandatory after a downstream carrier change.
    payload["metadata"]["idempotency_key"] = "changed-source"
    payload["messages"][2]["content"] = "Changed form request."
    rejected = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert rejected.status_code == 413, rejected.text


def test_current_merged_sources_reject_nonuser_or_unprotected_claim():
    from workers_projects_runtime.conversation_provider import _validated_visible_message_chain
    from fastapi import HTTPException
    for role, protected in [("assistant", True), ("user", False)]:
        with pytest.raises(HTTPException) as error:
            _validated_visible_message_chain([{"id": "source", "role": role, "sha256": "a" * 64,
                "accepted_source": protected, "current_input": True}], max_entries=128)
        assert error.value.status_code == 400


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code"])
def test_authored_preview_accepts_only_complete_public_control(profile):
    from workers_projects_runtime.conversation_provider import _native_authored_preview
    from workers_projects_runtime.agent_builder_control import graph_transfer_control
    control = graph_transfer_control([{"type": "function", "function": {"name": "lc_transfer_to_check", "parameters": {"type": "object"}}}], "auto")
    def event(text):
        return ({"type": "item.completed", "item": {"type": "agent_message", "text": text}}
                if profile == "codex-cli" else {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    answer = json.dumps({"type": "assistant_response", "content": "Timezone: UTC.", "tool_name": None})
    raw = "\n".join(json.dumps(row) for row in [
        {"type": "item.completed", "item": {"type": "reasoning", "text": "Private reasoning"}},
        event('{"type":"assistant_response","content":"partial'),
        event(json.dumps({"type": "tool_call", "content": "Internal handoff", "tool_name": "lc_transfer_to_check"})),
        event(answer), {"type": "item.completed", "item": {"type": "mcp_tool_call", "result": "private result"}},
    ])
    assert _native_authored_preview(profile, raw, control, None) == {"sequence": 4, "text": "Timezone: UTC."}
    assert _native_authored_preview(profile, "\n".join(raw.splitlines()[:3]), control, None) is None
    assert _native_authored_preview(profile, raw, None, None) is None


def test_authored_preview_stream_keeps_interim_out_of_final_and_graph_authority(monkeypatch, tmp_path):
    from types import SimpleNamespace
    provider = object.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))
    payload.tools = [{"type": "function", "function": {"name": "lc_transfer_to_check", "parameters": {"type": "object"}}}]
    record = {"request_id": "request-a", "run_id": "run-a", "session_id": "session-a", "owner_id": "owner-a",
              "message_id": "message-a", "native_invocation_id": "invocation-a", "state": "running"}
    session = {"worker_id": "worker-a", "owner_id": "owner-a"}
    turn = {"value": 0}
    def get_record(_):
        turn["value"] += 1
        return {**record, "state": "completed" if turn["value"] == 3 else "running"}
    def stdout(_worker, _run):
        text = "Timezone: UTC." if turn["value"] == 1 else "Checking the existing page."
        return "codex-cli", json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(
            {"type": "assistant_response", "content": text, "tool_name": None})}}) + ("\n{}" if turn["value"] == 2 else "")
    provider.store = SimpleNamespace(get_provider_request=get_record, get_run=lambda _: {"run_id": "run-a"},
        get_provider_session_by_id=lambda _: session, get_worker=lambda _: {"owner_id": "owner-a"}, list_provider_activity=lambda _: [])
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=stdout))
    provider._sync = lambda r: r
    provider._native_output_snapshot = lambda *_: ""
    canonical = {"model": payload.model, "choices": [{"message": {"role": "assistant", "content": "Final answer only."},
                  "finish_reason": "stop"}], "usage": {}, "glasshive": {"usage_source": "native"}}
    provider.response_payload = lambda *_: canonical
    provider._completion_usage = lambda *_: ({}, "native")
    class Connected:
        async def is_disconnected(self): return False
    async def fast_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    async def collect(): return [chunk async for chunk in provider._stream_chunks(record, payload, Connected())]
    raw = asyncio.run(collect())
    chunks = [json.loads(row[6:]) for row in raw if row.startswith("data: {")]
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    previews = [d["provider_specific_fields"]["viventium"]["assistant_preview"] for d in deltas if "provider_specific_fields" in d]
    assert previews and previews[0]["text"] == "Timezone: UTC."
    assert all("content" not in d and "reasoning_content" not in d for d in deltas if "provider_specific_fields" in d)
    assert "".join(d.get("content", "") for d in deltas) == "Final answer only."
    assert sum(chunk["choices"][0]["finish_reason"] == "stop" for chunk in chunks) == 1
    assert raw[-1] == "data: [DONE]\n\n"
    # Same existing snapshot owner rejects background, foreign and terminal producers.
    payload.metadata.origin = "scheduler"
    assert provider._native_preview_snapshot(record, {"run_id": "run-a"}, payload, {}, None) is None
    payload.metadata.origin = "interactive"
    assert provider._native_preview_snapshot({**record, "state": "cancelled"}, {"run_id": "run-a"}, payload, {}, None) is None
    assert provider._native_preview_snapshot(record, {"run_id": "superseded-run"}, payload, {}, None) is None
    session["owner_id"] = "foreign-owner"
    assert provider._native_preview_snapshot(record, {"run_id": "run-a"}, payload, {}, None) is None


@pytest.mark.parametrize("model", ["codex-cli:gpt-5.6-sol", "claude-code:opus"])
@pytest.mark.parametrize("change", ["valid", "no_proof", "wrong_response", "wrong_turn", "wrong_revision",
                                   "changed_epoch", "edited_input", "missing_input", "reordered_input",
                                   "changed_tool_binding", "stale_predecessor", "legacy_provenance", "admission_race", "active_predecessor", "inserted_history"])
def test_core_removed_uncommitted_reply_continues_only_with_bound_predecessor_proof(tmp_path, monkeypatch, model, change):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    store = client.app.state.store
    source = {"role": "user", "content": "Compare the two archive formats."}
    source_hash = hashlib.sha256(source["content"].encode()).hexdigest()
    payload = _payload(workspace, model=model)
    historical = {"role": "user", "content": "Use public documentation only."}
    payload["messages"] = [{"role": "system", "content": "Stable authority."}, historical, source]
    payload["metadata"].update({
        "message_id": "unfinished-answer", "idempotency_key": "supersession-first",
        "main_context_protocol": "main_context_v1", "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "b" * 64,
        "main_context_epoch": "c" * 64, "logical_turn_id": "accepted-turn", "logical_turn_revision": 1,
        "continuity_domain_id": "d" * 64, "continuity_agent_id": "agent-main",
        "visible_message_chain": [{"id": "historical", "role": "user", "sha256": hashlib.sha256(historical["content"].encode()).hexdigest()}, {"id": "source", "role": "user", "sha256": source_hash,
                                   "accepted_source": True, "current_input": True}],
    })
    headers = {**AUTH, "X-GlassHive-Fallback-Model": ("codex-cli:gpt-5.6-sol"
               if model == "claude-code:opus" else "claude-code:opus"),
               "X-GlassHive-Fallback-Reasoning-Effort": "high"}
    first = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    first_record = store.get_provider_request(first.json()["id"])
    followup = {"role": "user", "content": "What is eighteen times seven?"}
    payload["messages"].append(followup)
    payload["metadata"].update({"message_id": "revised-answer", "idempotency_key": "supersession-second",
                               "logical_turn_revision": 2,
                               "native_predecessor_supersession": {
        "version": 1, "previous_response_message_id": "unfinished-answer",
        "logical_turn_id": "accepted-turn", "previous_revision": 1, "revision": 2,
        "disposition": "partial_removed", "accepted_sources": [{"id": "source", "sha256": source_hash}],
    }})
    payload["metadata"]["visible_message_chain"].append({
        "id": "quick-source", "role": "user", "sha256": hashlib.sha256(followup["content"].encode()).hexdigest(),
        "accepted_source": True, "current_input": True,
    })
    proof = payload["metadata"]["native_predecessor_supersession"]
    if change == "no_proof":
        del payload["metadata"]["native_predecessor_supersession"]
    elif change == "wrong_response":
        proof["previous_response_message_id"] = "some-other-answer"
    elif change == "wrong_turn":
        proof["logical_turn_id"] = "different-turn"
    elif change == "wrong_revision":
        proof["previous_revision"] = 2
    elif change == "changed_epoch":
        payload["metadata"]["main_context_epoch"] = "e" * 64
    elif change == "edited_input":
        payload["messages"][1] = {"role": "user", "content": "Changed protected history."}
    elif change == "missing_input":
        del payload["messages"][1]
        del payload["metadata"]["visible_message_chain"][0]
    elif change == "inserted_history":
        payload["messages"].insert(2, {"role": "assistant", "content": "New historical answer inserted before the source."})
    elif change == "reordered_input":
        payload["messages"][1], payload["messages"][2] = payload["messages"][2], payload["messages"][1]
    elif change == "changed_tool_binding":
        payload["messages"][1] = {**historical, "tool_call_id": "different-tool-binding"}
    elif change in {"stale_predecessor", "legacy_provenance"}:
        session = store.get_provider_session_by_id(first_record["session_id"])
        manifest = json.loads(session["context_manifest_json"])
        if change == "stale_predecessor":
            manifest["last_native_admission_request_id"] = "unknown-newer-admission"
        else:
            manifest.pop("last_native_admission_request_id", None)
        store.update_provider_session_history(session["session_id"], history_count=session["history_count"], context_manifest=manifest)
    if change == "admission_race":
        create = store.create_provider_request
        def race(**kwargs):
            session = store.get_provider_session_by_id(first_record["session_id"])
            manifest = json.loads(session["context_manifest_json"])
            manifest["last_native_admission_request_id"] = "concurrent-admission"
            store.update_provider_session_history(session["session_id"], history_count=session["history_count"], context_manifest=manifest)
            return create(**kwargs)
        monkeypatch.setattr(store, "create_provider_request", race)
    elif change == "active_predecessor":
        monkeypatch.setattr(store, "get_active_host_run_lease_for_run", lambda run_id: {"run_id": run_id, "status": "active"})
    second = client.post("/v1/chat/completions", headers=headers, json=payload)
    if change in {"admission_race", "active_predecessor"}:
        assert second.status_code == 409, second.text
        return
    assert second.status_code == 200, second.text
    second_record = store.get_provider_request(second.json()["id"])
    assert second_record["session_id"] == first_record["session_id"]
    decision = json.loads(second_record["replay_decision_json"])
    if change != "valid":
        assert decision["native_context_epoch"]
        assert "predecessor_supersession" not in decision
        return
    assert decision["native_context_epoch"] == ""
    assert decision["predecessor_supersession"]["previous_response_message_id"] == "unfinished-answer"
    assert '"acknowledgement":"partial_removed"' in second_record["admitted_instruction"]
    assert followup["content"] in second_record["admitted_instruction"]
    assert '"acknowledgement":"partial_removed"' in second_record["fallback_instruction"]
    assert followup["content"] in second_record["fallback_instruction"]
    replay = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json()["id"] == second.json()["id"]
    reopened = Store(str(store.db_path))
    session = reopened.get_provider_session_by_id(first_record["session_id"])
    assert json.loads(session["context_manifest_json"])["last_native_admission_request_id"] == second_record["request_id"]
    assert json.loads(reopened.get_provider_request(second_record["request_id"])["replay_decision_json"])["predecessor_request_id"] == first_record["request_id"]


def test_requested_role_models_preserve_verified_native_identity_and_effort():
    astra = GLASSHIVE_MODELS["codex-cli:gpt-6-astra"]
    opus = GLASSHIVE_MODELS["claude-code:claude-opus-5"]
    assert astra.native_model == "gpt-6-astra"
    assert astra.context_window == 272000
    assert astra.effort_choices == ("low", "medium", "high", "xhigh", "max", "ultra")
    assert opus.native_model == "claude-opus-5"
    assert opus.context_window == 1000000
    assert {"low", "medium"}.issubset(opus.effort_choices)
    assert GLASSHIVE_MODELS["claude-code:opus"].native_model == "opus"


def test_codex_native_default_is_explicit_without_claiming_exact_model_identity():
    model = GLASSHIVE_MODELS["codex-cli:native-default"]
    assert model.native_model == ""
    assert model.context_window == 32768
    assert model.recommended_effort == "medium"
    assert model.api_payload()["native_model"] == ""


def test_standalone_conversation_render_uses_neutral_product_language():
    from workers_projects_runtime.conversation_provider import _admit_conversation_history

    instruction, _, _ = _admit_conversation_history(
        [ChatMessage(role="user", content="Handle this request.")],
        start_at=0, turn_context="", model=GLASSHIVE_MODELS["codex-cli:gpt-6-astra"],
    )
    assert instruction.startswith("Continue this conversation naturally.")
    assert "Viventium conversation" not in instruction
    assert "<viventium_current_accepted_turn_v1>" in instruction
    assert "Handle this request." in instruction


@pytest.mark.parametrize(
    "model_id,native_model,context_window",
    [
        ("codex-cli:gpt-6-sol", "gpt-6-sol", 1_050_000),
        ("codex-cli:gpt-6-luna", "gpt-6-luna", 1_050_000),
        ("claude-code:claude-opus-5-5", "claude-opus-5-5", 1_000_000),
        ("codex-cli:gpt-5.4", "gpt-5.4", 1_050_000),
    ],
)
def test_current_catalog_models_keep_exact_native_identity(
    model_id, native_model, context_window,
):
    model = GLASSHIVE_MODELS[model_id]
    assert model.native_model == native_model
    assert model.context_window == context_window
    assert model.recommended_effort == "medium"
    assert {"low", "medium", "high", "xhigh"} <= set(model.effort_choices)
    assert model.api_payload()["native_model"] == native_model


@pytest.mark.parametrize("delta", [None, {15, 16}])
def test_current_source_ordinals_preserve_global_labels_and_delta_content(delta):
    from workers_projects_runtime.conversation_provider import _admit_conversation_history
    messages = [ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"History {i}.") for i in range(15)]
    messages += [ChatMessage(role="user", content="Exact A."), ChatMessage(role="user", content="Exact B.")]
    kwargs = dict(start_at=0, turn_context="", model=next(iter(GLASSHIVE_MODELS.values())),
                  include_indices=delta, current_input_indices={15, 16})
    legacy, decision, _ = _admit_conversation_history(messages, **kwargs)
    mapped, mapped_decision, _ = _admit_conversation_history(messages, source_ordinals_by_index={15: [1, 2], 16: [3]}, **kwargs)
    assert '[message 15 user]\n<delegation_source>{"sourceOrdinals":[1,2]}</delegation_source>\nExact A.' in mapped
    assert '[message 16 user]\n<delegation_source>{"sourceOrdinals":[3]}</delegation_source>\nExact B.' in mapped
    assert '<delegation_source>' not in legacy
    assert decision['admitted_message_indices'] == mapped_decision['admitted_message_indices']
    assert [message.content for message in messages][-2:] == ['Exact A.', 'Exact B.']


@pytest.mark.parametrize('ordinals', [None, [], [0], [True], [1.5], ['1'], [1, 1], [2, 1], '1', [129]])
def test_current_source_ordinal_mapping_rejects_malformed_arrays(ordinals):
    from workers_projects_runtime.conversation_provider import _validated_visible_message_chain
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _validated_visible_message_chain([{'id': 'a', 'role': 'user', 'sha256': 'a' * 64,
            'accepted_source': True, 'current_input': True, 'source_ordinals': ordinals}], max_entries=128)


def test_current_source_ordinal_mapping_requires_complete_unique_current_authority():
    from workers_projects_runtime.conversation_provider import _validated_visible_message_chain
    from fastapi import HTTPException
    source = {'id': 'a', 'role': 'user', 'sha256': 'a' * 64, 'accepted_source': True, 'current_input': True}
    valid = [{**source, 'source_ordinals': [1, 2]}, {**source, 'id': 'b', 'source_ordinals': [3]}]
    assert [item['source_ordinals'] for item in _validated_visible_message_chain(valid, max_entries=128)] == [[1, 2], [3]]
    for bad in [
        [{**source, 'source_ordinals': [1]}, {**source, 'source_ordinals': [2]}],
        [{**source, 'source_ordinals': [1]}, {**source, 'id': 'b', 'source_ordinals': [1]}],
        [{**source, 'source_ordinals': [1]}, {**source, 'id': 'b'}],
        [{**source, 'source_ordinals': [2]}],
        [{**source, 'current_input': False, 'source_ordinals': [1]}],
    ]:
        with pytest.raises(HTTPException):
            _validated_visible_message_chain(bad, max_entries=128)
    assert 'source_ordinals' not in _validated_visible_message_chain([source], max_entries=128)[0]
