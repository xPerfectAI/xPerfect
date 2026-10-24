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
from workers_projects_runtime.bootstrap import conversation_file_delivery_instructions
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
    _native_terminal_text,
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


@pytest.mark.parametrize("selected", [True, False])
def test_native_library_files_bind_before_first_start_and_session_reuse(tmp_path, monkeypatch, selected):
    client = _client(tmp_path, monkeypatch)
    service, store = client.app.state.service, client.app.state.store
    provider = client.app.state.conversation_provider
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    payload = _payload(tmp_path, model="codex-cli:gpt-6.1-sol")
    payload["metadata"]["bootstrap_bundle"] = {}
    expected = []
    if selected:
        item = service.personal_library.create_item("local", "owner-a", {"name":"Native selected note", "kind":"note", "text":"Exact native selected source"}, "native-note")
        ref = {key:item[key] for key in ("item_id", "version_id")}
        resolved = service.personal_library.resolve_refs("local", "owner-a", [ref])
        payload["metadata"]["bootstrap_bundle"]["library_selection"] = {"refs":[ref], "items":[{key:item[key] for key in ("item_id","version_id","name","kind","version")}], "pending":[], "upload_ids":resolved["upload_ids"], "upload_revisions":resolved["upload_revisions"]}
        expected = resolved["upload_ids"]
    parsed = ChatCompletionRequest.model_validate(payload)
    original_resume, resumed = service.resume_worker, []
    def resume(worker_id):
        worker = store.get_worker(worker_id)
        bundle = json.loads(worker["bootstrap_bundle_json"])
        entries = bundle.get("files", [])
        if selected:
            assert entries[0]["managed_upload_id"] == expected[0]
            assert entries[0]["managed_projection_id"]
            assert (tmp_path / entries[0]["path"]).read_text() == "Exact native selected source"
        else:
            assert not entries
        resumed.append(worker_id)
        return original_resume(worker_id)
    monkeypatch.setattr(service, "resume_worker", resume)
    model = GLASSHIVE_MODELS[parsed.model]
    session = provider._create_native_session(parsed, model, tmp_path, "high", tenant_id="local")
    assert resumed == [session["worker_id"]]
    created_worker = store.get_worker(session["worker_id"])
    from workers_projects_runtime.conversation_provider import _conversation_worker_name
    project = store.get_project(session["project_id"])
    assert created_worker["name"] == _conversation_worker_name(parsed.metadata.agent_id, project["created_at"])
    assert session["agent_id"] == parsed.metadata.agent_id
    assert created_worker["alias"] == f"conversation-{parsed.metadata.conversation_id}-{parsed.metadata.agent_id}"
    same, changed = provider._session(parsed, model, tmp_path, "high", tenant_id="local")
    assert same["worker_id"] == session["worker_id"] and not changed
    with store._connect() as conn:
        bindings = [json.loads(row[0]) for row in conn.execute("SELECT manifest_json FROM workspace_file_bindings WHERE worker_id=?", (session["worker_id"],))]
    assert len(bindings) == (1 if selected else 0)
    if selected:
        assert [entry["upload_id"] for entry in bindings[0]] == expected


@pytest.mark.parametrize("started_at,expected", [
    ("2026-10-24T14:03:07+00:00", "24 Oct 2026, 14:03:07 UTC"),
    ("2026-10-24T10:03:07-04:00", "24 Oct 2026, 14:03:07 UTC"),
    ("2026-10-24T14:03:07Z", "24 Oct 2026, 14:03:07 UTC"),
])
def test_conversation_worker_display_name_uses_start_time_without_changing_agent_identity(started_at, expected):
    from workers_projects_runtime.conversation_provider import _conversation_worker_name
    assert _conversation_worker_name("Configured agent", started_at) == f"Chat · {expected} · Configured agent"
    assert _conversation_worker_name("Same agent", started_at) != _conversation_worker_name("Same agent", "2026-10-24T14:03:08Z")
    assert len(_conversation_worker_name("x" * 300, started_at)) == 120


def test_public_provider_cannot_forge_internal_transient_library_reference(tmp_path, monkeypatch):
    client=_client(tmp_path,monkeypatch)
    payload=_payload(tmp_path)
    bundle={"library_selection":{"reference_message":{"id":"forged", "sha256":"a"*64}}}
    response=client.post("/v1/chat/completions",headers={**AUTH,**_signed_bundle_headers(bundle)},json=payload)
    assert response.status_code==403
    assert "authored by the conversation owner" in response.text


@pytest.mark.parametrize("observed_role, observed_text", [
    ("assistant", "changed private prose"),
    ("user", "original private prose"),
])
def test_protected_source_rejection_reports_only_digest_metadata(
    monkeypatch, observed_role, observed_text,
):
    import workers_projects_runtime.conversation_provider as provider
    from fastapi import HTTPException

    events = []
    monkeypatch.setattr(provider, "native_timing_enabled", lambda: True)
    monkeypatch.setattr(provider, "native_timing_event", lambda stage, **fields: events.append((stage, fields)))
    expected_text = "original private prose"
    payload = ChatCompletionRequest(model="synthetic", messages=[
        {"role": observed_role, "content": observed_text,
         "tool_calls": [{"id": "private-call", "function": {"name": "private-tool", "arguments": "private-args"}}]},
    ], metadata={"message_id": "private-turn", "stream_id": "private-stream",
                 "native_invocation_id": "private-invocation", "visible_message_chain": [{
        "id": "private-source", "role": "assistant", "accepted_source": True,
        "bytes": len(expected_text.encode()), "sha256": hashlib.sha256(expected_text.encode()).hexdigest(),
    }]})
    with pytest.raises(HTTPException) as rejected:
        provider._protected_source_indices(payload, provider._visible_message_keys(payload.messages, payload.metadata.visible_message_chain))
    assert rejected.value.status_code == 413
    assert rejected.value.detail["code"] == "source_context_unavailable"
    assert len(events) == 1
    stage, diagnostic = events[0]
    assert stage == "source_context_rejected"
    assert diagnostic["expectedRole"] == "assistant"
    assert diagnostic["expectedSha256"] == hashlib.sha256(expected_text.encode()).hexdigest()
    assert diagnostic["messageCount"] == 1
    assert diagnostic["observedMessages"][0]["authoredSha256"] == hashlib.sha256(observed_text.encode()).hexdigest()
    assert diagnostic["observedMessages"][0]["toolCallCount"] == 1
    serialized = json.dumps(diagnostic)
    for private in (expected_text, observed_text, "private-source", "private-turn", "private-stream", "private-invocation", "private-call", "private-tool", "private-args"):
        assert private not in serialized


def test_protected_source_diagnostic_does_not_log_accepted_sources_or_when_disabled(monkeypatch):
    import workers_projects_runtime.conversation_provider as provider
    from fastapi import HTTPException

    events = []
    monkeypatch.setattr(provider, "native_timing_event", lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(provider, "native_timing_enabled", lambda: True)
    text = "Exact original words."
    payload = ChatCompletionRequest(model="synthetic", messages=[{"role": "assistant", "content": text}],
        metadata={"visible_message_chain": [{"id": "source", "role": "assistant", "accepted_source": True,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}]})
    assert provider._protected_source_indices(payload, provider._visible_message_keys(payload.messages, payload.metadata.visible_message_chain)) == {0}
    assert not events
    monkeypatch.setattr(provider, "native_timing_enabled", lambda: False)
    changed = payload.model_copy(update={"messages": [ChatMessage(role="assistant", content="Changed.")]})
    with pytest.raises(HTTPException):
        provider._protected_source_indices(changed, provider._visible_message_keys(changed.messages, changed.metadata.visible_message_chain))
    assert not events


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


MISSING_DECLARED_AUTHORITY_MESSAGES = [
    pytest.param(
        [{"role": "system", "content": "Stable identity."},
         {"role": "user", "content": "Visible request."}],
        id="stable-authority-missing-tail",
    ),
    pytest.param(
        [{"role": "user", "content": "Visible request."}],
        id="empty-authority",
    ),
    pytest.param(
        [{"role": "system", "content": " \n\t"},
         {"role": "developer", "content": "\t \n"},
         {"role": "user", "content": "Visible request."}],
        id="whitespace-authority",
    ),
    pytest.param(
        [{"role": "user", "content": "Undeclared hidden authority."}],
        id="tail-only-in-user",
    ),
]


@pytest.mark.parametrize("messages", MISSING_DECLARED_AUTHORITY_MESSAGES)
def test_declared_dynamic_authority_tail_must_already_exist_in_authority_messages(messages):
    from fastapi import HTTPException

    payload = ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": messages,
            "metadata": {
                "owner_id": "owner-a",
                "conversation_id": "conv-a",
                "agent_id": "agent-a",
                "developer_instruction_tail": "Undeclared hidden authority.",
            },
        }
    )

    with pytest.raises(HTTPException) as rejected:
        _developer_instruction_snapshot(payload, "Signed native bootstrap.")
    assert rejected.value.status_code == 400
    assert "tail is absent" in rejected.value.detail


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


class ProviderAuthMissingRuntime(StubRuntime):
    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = instruction, timeout_sec, run_id
        _publish_in_process_test_start(worker)
        raise RuntimeErrorBase("codex-cli could not use its provider login")

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
            "error_text": "codex-cli could not use its provider login",
            "failure_class": "provider_auth_missing",
            "failure_retryable": 0,
            "failure_user_message": "The worker could not use the configured model provider credentials.",
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
        "codex-cli:gpt-6.1-sol", "grok-build:grok-4.7", "grok-build:grok-4.7-build-fast",
        "codex-cli:gpt-6-astra", "codex-cli:gpt-6-sol", "codex-cli:gpt-6-luna",
        "codex-cli:gpt-5.6-sol", "codex-cli:gpt-5.6-luna", "codex-cli:gpt-5.6-terra",
        "codex-cli:gpt-5.4", "codex-cli:native-default",
        "claude-code:claude-opus-5-5", "claude-code:claude-opus-5", "claude-code:opus",
        "direct-openai:gpt-6.1-sol", "direct-anthropic:claude-opus-5-5", "direct-xai:grok-4.7",
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
    assert models["grok-build:grok-4.7"]["capabilities"]["incremental_text"] is True
    assert models["grok-build:grok-4.7"]["capabilities"]["incremental_text_requires_audio_eligibility"] is True
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


@pytest.mark.parametrize('native_tools', [False, True, None])
@pytest.mark.parametrize('replay_mode', ['persistent', 'stateless'])
def test_exact_run_session_mode_preserves_signed_native_tool_restriction(
    tmp_path, monkeypatch, native_tools, replay_mode,
):
    workspace = tmp_path / 'synthetic-workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    bundle = {'env': {'GLASSHIVE_PROVIDER_SESSION_MODE': 'stateless'}}
    if native_tools is not None:
        bundle['provider_capabilities'] = {'native_tools': native_tools}
    response = client.post('/v1/chat/completions',
        headers={**AUTH, **_signed_bundle_headers(bundle),
                 'X-GlassHive-Provider-Session-Mode': replay_mode}, json=_payload(workspace))
    assert response.status_code == 200, response.text
    request = client.app.state.store.get_provider_request(response.json()['id'])
    expected = 'stateless' if native_tools is False else replay_mode
    decision = json.loads(request['replay_decision_json'])
    assert decision['provider_session_mode'] == expected
    if native_tools is False:
        # Historical requests persisted the caller mode despite the signed restriction.
        client.app.state.store.update_provider_request(request['request_id'],
            replay_decision_json=json.dumps({**decision, 'provider_session_mode': replay_mode}))
    worker = client.app.state.store.get_worker(
        client.app.state.store.get_run(request['run_id'])['worker_id'])
    admitted = json.loads(worker['bootstrap_bundle_json'])
    projected = client.app.state.service._exact_provider_session_bundle_for_run(admitted, request['run_id'])
    assert projected['env'].get('GLASSHIVE_PROVIDER_SESSION_MODE', 'persistent') == expected
    assert projected['provider_capabilities']['native_tools'] is (native_tools is not False)
    assert projected['developer_instructions'] == admitted['developer_instructions']


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
def test_structured_quota_failure_continues_the_turn_on_the_serial_fallback(tmp_path, authoring_scope, monkeypatch):
    # A primary run that fails with structured provider quota evidence must not fail the request:
    # the armed serial fallback is claimed exactly once and the exact turn continues on a new
    # worker running the fallback model.
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, InterruptCountingRuntime(), reconcile_on_startup=False)
    provider: ConversationProvider | None = None
    try:
        provider = ConversationProvider(store, service)
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
        provider._remember_request_local_bundle(request["request_id"], run["run_id"], {
            "_authority_recovery_source": {"messages": [{"role": "user", "content": "Private recovery source."}]},
            "env": {"SYNTHETIC_RUN_LOCAL": "retained"},
        })
        dispatched_bundles = []
        original_assign_run = service.assign_run
        def capture_assignment(*args, **kwargs):
            dispatched_bundles.append(kwargs.get("run_local_bundle"))
            return original_assign_run(*args, **kwargs)
        monkeypatch.setattr(service, "assign_run", capture_assignment)

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
        from workers_projects_runtime.conversation_provider import _conversation_worker_name
        assert fallback_worker["name"] == _conversation_worker_name(session["agent_id"], session["created_at"])
        current_session = store.get_provider_session_by_id(session["session_id"])
        assert (current_session["actor_kind"], current_session["origin"]) == authoring_scope
        assert current_session["worker_id"] == fallback_worker["worker_id"]
        assert len(store.list_provider_sessions(owner_id="owner-a")) == 1
        assert len(dispatched_bundles) == 1
        assert "_authority_recovery_source" not in dispatched_bundles[0]
        assert dispatched_bundles[0]["env"]["SYNTHETIC_RUN_LOCAL"] == "retained"
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
    # Audio consumers must receive the decision on the same content chunk.
    assert all(
        choice["delta"]["provider_specific_fields"]["viventium"]["delivery_disposition"] == disposition
        for chunk in chunks for choice in chunk.get("choices", [])
        if choice.get("delta", {}).get("content")
    )


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
    assert all(
        choice["delta"]["provider_specific_fields"]["viventium"]["delivery_disposition"] == disposition
        for chunk in chunks for choice in chunk.get("choices", [])
        if choice.get("delta", {}).get("content")
    )


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



@pytest.mark.parametrize("carrier", ["header", "body"])
def test_read_only_text_compactor_signed_zero_tool_grok_admission(tmp_path, monkeypatch, carrier):
    client = _scoped_client(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code,grok-build")
    payload = _payload(tmp_path, model="grok-build:grok-4.7-build-fast")
    payload["reasoning_effort"] = "high"
    headers = {**AUTH, "X-GlassHive-Access": "read_only", "X-GlassHive-Provider-Session-Mode": "persistent",
        **_signed_bundle_headers({"provider_capabilities": {"native_tools": False}})}
    if carrier == "body":
        payload["metadata"]["bootstrap_bundle_b64"] = headers.pop("X-GlassHive-Bootstrap-Bundle-B64")
    response = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    session, = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    worker = client.app.state.store.get_worker(session["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert session["access_mode"] == bundle["access_mode"] == "read_only"
    assert bundle["provider_capabilities"]["native_tools"] is False
    assert bundle["env"]["GLASSHIVE_PROVIDER_SESSION_MODE"] == "stateless"
    model = GLASSHIVE_MODELS["grok-build:grok-4.7-build-fast"]
    fallback = ConversationProvider._fallback_bundle(worker, model, "high")
    assert fallback["access_mode"] == "read_only"
    assert fallback["provider_capabilities"]["native_tools"] is False
    assert fallback["env"]["WPR_GROK_REASONING_EFFORT"] == "high"


@pytest.mark.parametrize("declaration", [None, True, "false"])
def test_read_only_text_compactor_nonzero_grok_remains_unsupported(tmp_path, monkeypatch, declaration):
    client = _scoped_client(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code,grok-build")
    payload = _payload(tmp_path, model="grok-build:grok-4.7-build-fast")
    headers = {**AUTH, "X-GlassHive-Access": "read_only"}
    if declaration is not None:
        headers.update(_signed_bundle_headers({"provider_capabilities": {"native_tools": declaration}}))
    response = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "unsupported_access_mode"
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_read_only_text_compactor_unsigned_plain_grok_cannot_narrow_auth(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code,grok-build")
    payload = _payload(tmp_path, model="grok-build:grok-4.7-build-fast")
    payload["metadata"]["bootstrap_bundle"] = {"provider_capabilities": {"native_tools": False}}
    response = client.post("/v1/chat/completions", headers={**AUTH, "X-GlassHive-Access": "read_only"}, json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "unsupported_access_mode"
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_read_only_text_compactor_zero_tool_docker_stays_unsupported():
    from fastapi import HTTPException
    model = GLASSHIVE_MODELS["grok-build:grok-4.7-build-fast"]
    with pytest.raises(HTTPException) as error:
        ConversationProvider._fallback_bundle({"execution_mode": "docker", "bootstrap_bundle_json": json.dumps({
            "access_mode": "read_only", "provider_capabilities": {"native_tools": False}})}, model, "high")
    assert error.value.detail["code"] == "unsupported_access_mode"

def test_read_only_header_reaches_the_native_bundle_without_widening_access(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token", "X-GlassHive-Access": "read_only"},
        json=_payload(tmp_path),
    )
    assert response.status_code == 200, response.text
    session, = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert session["access_mode"] == "read_only"
    worker = client.app.state.store.get_worker(session["worker_id"])
    assert json.loads(worker["bootstrap_bundle_json"])["access_mode"] == "read_only"


def test_read_only_access_rejects_unsupported_primary_and_fallback_harnesses(tmp_path, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr("workers_projects_runtime.conversation_provider._host_claude_read_only_supported", lambda: False)
    client = _scoped_client(tmp_path, monkeypatch)
    model = next(item for item in GLASSHIVE_MODELS.values() if item.harness_profile == "claude-code")
    payload = _payload(tmp_path, model=model.id)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token", "X-GlassHive-Access": "read_only"},
        json=payload,
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "unsupported_access_mode"
    with pytest.raises(HTTPException) as error:
        ConversationProvider._fallback_bundle(
            {"bootstrap_bundle_json": json.dumps({"access_mode": "read_only"})}, model, "high",
        )
    assert error.value.detail["code"] == "unsupported_access_mode"
    codex_model = GLASSHIVE_MODELS["codex-cli:gpt-5.6-sol"]
    with pytest.raises(HTTPException) as error:
        ConversationProvider._fallback_bundle(
            {"execution_mode": "docker", "bootstrap_bundle_json": json.dumps({"access_mode": "read_only"})},
            codex_model, "high",
        )
    assert error.value.detail["code"] == "unsupported_access_mode"


def test_read_only_claude_primary_and_fallback_keep_access_when_native_controls_exist(tmp_path, monkeypatch):
    monkeypatch.setattr("workers_projects_runtime.conversation_provider._host_claude_read_only_supported", lambda: True)
    client = _scoped_client(tmp_path, monkeypatch)
    model = next(item for item in GLASSHIVE_MODELS.values() if item.harness_profile == "claude-code")
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token", "X-GlassHive-Access": "read_only"},
        json=_payload(tmp_path, model=model.id),
    )
    assert response.status_code == 200, response.text
    session, = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert session["access_mode"] == "read_only"
    worker = client.app.state.store.get_worker(session["worker_id"])
    assert json.loads(worker["bootstrap_bundle_json"])["access_mode"] == "read_only"
    fallback = ConversationProvider._fallback_bundle(worker, model, "high")
    assert fallback["access_mode"] == "read_only"
    assert fallback["env"]["WPR_CLAUDE_CODE_EFFORT"] == "high"


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
    tmp_path, monkeypatch, caplog
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
    diagnostics = [record.getMessage() for record in caplog.records
                   if record.getMessage().startswith("Provider request rejected")]
    assert any("origin=request_validation" in message for message in diagnostics)
    assert any("origin=_" in message for message in diagnostics)
    assert all("Hello." not in message and "provider-test-token" not in message
               and "unsafe_shape" not in message and str(tmp_path) not in message
               for message in diagnostics)


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


def test_p0_session_observation_distinguishes_create_reuse_and_model_rebind(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("VIVENTIUM_VOICE_LOG_LATENCY", "1")
    from workers_projects_runtime import native_model_selection
    caplog.set_level(logging.INFO, logger=native_model_selection._logger.name)
    monkeypatch.setattr(native_model_selection._logger, "propagate", True)
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    second_payload = _payload(workspace)
    second_payload["metadata"].update(message_id="synthetic-second-message", idempotency_key="synthetic-second-idem")
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)
    assert second.status_code == 200, second.text
    second_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert first_session["worker_id"] == second_session["worker_id"]
    third_payload = _payload(workspace, model="claude-code:opus")
    third_payload["metadata"].update(message_id="synthetic-third-message", idempotency_key="synthetic-third-idem")
    third = client.post("/v1/chat/completions", headers=AUTH, json=third_payload)
    assert third.status_code == 200, third.text
    rows = [json.loads(record.message.removeprefix("[NativeP0] ")) for record in caplog.records if record.message.startswith("[NativeP0] ")]
    decisions = [row for row in rows if row["stage"] == "session_binding_decision"]
    assert [row["decision"] for row in decisions] == ["create", "reuse", "rebind"]
    assert decisions[-1]["modelChanged"] is True
    assert decisions[1]["workerHash"] == decisions[-1]["workerHash"]
    assert any(row["stage"] == "session_reused" for row in rows)
    assert str(workspace) not in json.dumps(rows)
    assert "owner-a" not in json.dumps(rows)
    assert "synthetic-second-message" not in json.dumps(rows)


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
        "Quiet Feeling capsule.\n\n" + conversation_file_delivery_instructions().strip()
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
    assert current_bundle["developer_instructions"] == "Joyful Feeling capsule.\n\n" + conversation_file_delivery_instructions().strip()
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
        "Be a thoughtful assistant.\n\n" + conversation_file_delivery_instructions().strip()
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
    # A completed response can precede the processor's final worker-state update.
    # Inject the synthetic crash only after that earlier run has fully settled.
    service = client.app.state.service
    deadline = time.monotonic() + 5
    while service._local_processor_owns(first_session["worker_id"]) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not service._local_processor_owns(first_session["worker_id"])
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


@pytest.mark.parametrize("with_current", [False, True])
def test_typed_current_uploads_keep_history_outside_current_turn_and_fallback(tmp_path, monkeypatch, with_current):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    uploads = tmp_path / "uploads"
    owner_dir = uploads / "owner-a"
    owner_dir.mkdir(parents=True)
    records = []
    for file_id, filename, mime in [("current-file", "request.wav", "audio/wav"),
                                   ("history-file", "shapes.png", "image/png")]:
        data = ("synthetic-" + file_id).encode()
        (owner_dir / f"{file_id}__{filename}").write_bytes(data)
        records.append({"file_id": file_id, "filename": filename, "type": mime,
                        "source": "local", "bytes": len(data)})
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads))
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["messages"] = [payload["messages"][0],
        {"role": "user", "content": "Name the earlier shapes."},
        {"role": "assistant", "content": "A square and a circle."},
        {"role": "user", "content": "" if with_current else "Use the prior recording."}]
    bundle = {"viventium_upload_context": {
        "selected_uploads": records,
        "current_upload_ids": ["current-file"] if with_current else [],
    }}
    response = client.post("/v1/chat/completions", json=payload, headers={
        **AUTH, **_signed_bundle_headers(bundle),
        "X-GlassHive-Fallback-Model": "claude-code:opus",
        "X-GlassHive-Fallback-Reasoning-Effort": "high",
    })
    assert response.status_code == 200, response.text
    record = client.app.state.store.get_provider_request(response.json()["glasshive"]["request_id"])
    for instruction in (record["admitted_instruction"], record["fallback_instruction"]):
        assert "Read the supplied attachment representations" not in instruction
        assert "Historical conversation files remain available" not in instruction
        history, current = instruction.split("<viventium_current_accepted_turn_v1>", 1)
        assert '<viventium_historical_workspace_files version="1">' in history
        assert '"file_id":"history-file"' in history
        assert "uploads/shapes.png" in history and "uploads/shapes.png" not in current
        assert '"file_id":"history-file"' not in current
        if with_current:
            assert '"file_id":"current-file"' in current
            assert "uploads/request.wav" in current and "uploads/request.wav" not in history
        else:
            assert "uploads/request.wav" in history and "uploads/request.wav" not in current
            assert "<viventium_attached_workspace_files" not in current
    run = client.app.state.store.get_run(record["run_id"])
    worker = client.app.state.store.get_worker(run["worker_id"])
    retained = json.loads(worker["bootstrap_bundle_json"])
    assert [item["path"] for item in retained["files"]] == ["uploads/request.wav", "uploads/shapes.png"]


@pytest.mark.parametrize("current_ids", [None, "file-a", [1], ["x" * 257]])
def test_native_attachment_scope_rejects_malformed_current_ids(tmp_path, current_ids):
    from fastapi import HTTPException
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path))
    payload.metadata.bootstrap_bundle = {"viventium_upload_context": {
        "selected_uploads": [], "current_upload_ids": current_ids,
    }}
    with pytest.raises(HTTPException) as error:
        ConversationProvider._request_attachment_context(payload)
    assert error.value.status_code == 400


def test_selected_packet_preserves_explicit_empty_current_upload_scope(tmp_path):
    bundle = {"viventium_upload_context": {
        "selected_uploads": [{"file_id": "history-file", "filename": "report.txt", "source": "text", "text": "Historical content."}],
        "current_upload_ids": [],
    }, "viventium_delegation_packet": {"selected_files": [{"ordinal": 0, "ref": "history-file"}]}}
    scoped = ConversationProvider._selected_request_bundle(bundle)
    assert scoped["viventium_upload_context"]["current_upload_ids"] == []
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path))
    payload.metadata.bootstrap_bundle = scoped
    assert ConversationProvider._request_attachment_context(payload) == ""
    assert '"file_id":"history-file"' in ConversationProvider._request_attachment_context(payload, historical=True)


@pytest.mark.parametrize("tool_mode", ["full", "coordination"])
def test_native_attachment_context_identifies_actual_supplied_representations(tmp_path, monkeypatch, tool_mode):
    uploads = tmp_path / "uploads"
    owner_dir = uploads / "owner-a"
    owner_dir.mkdir(parents=True)
    originals = [("image-current", "picture.png", "image/png"),
                 ("audio-current", "request.m4a", "audio/mp4")]
    records = []
    for file_id, filename, mime in originals:
        data = b"synthetic-original-bytes-" + file_id.encode()
        (owner_dir / f"{file_id}__{filename}").write_bytes(data)
        records.append({"file_id": file_id, "filename": filename, "source": "local",
                        "type": mime, "bytes": len(data)})
    records.extend([
        {"file_id": "declared-text", "filename": "note.txt", "source": "text",
         "text": "Exact supplied extraction.\n"},
        {"file_id": "text-fallback", "filename": "fallback.txt", "source": "local",
         "text": "Text without original bytes.\n"},
        {"file_id": "missing-original", "filename": "report.pdf", "source": "local",
         "text": "Extracted text cannot replace this original."},
        {"file_id": "stored-extraction", "filename": "stored.txt", "source": "text",
         "text": "Stored extracted text.\n"},
    ])
    (owner_dir / "stored-extraction__stored.txt").write_text("Different original bytes.\n")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads))
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path))
    payload.metadata.bootstrap_bundle = {"conversation_tool_mode": tool_mode,
        "viventium_upload_context": {"selected_uploads": records}}
    projected = ConversationProvider._projected_request_uploads(payload)
    context = ConversationProvider._request_attachment_context(payload)
    ledger = json.loads(context.splitlines()[1])["selected_uploads"]
    assert [item["file_id"] for item in ledger] == [item["file_id"] for item in records]
    assert [item["inputKind"] for item in ledger] == [
        "original", "original", "extracted_text", "extracted_text", "metadata_only", "extracted_text",
    ]
    assert "Read each original attachment" not in context
    assert "Read the supplied attachment representations" not in context
    assert "Historical conversation files remain available" not in context
    assert "Extracted text does not establish access to original bytes" in context
    assert "Metadata-only entries report unavailable input" in context
    for index, item in enumerate(projected, 1):
        assert f"{index}. {item['path']}" in context
    for item in projected[:2]:
        assert Path(item["source_path"]).read_bytes().startswith(b"synthetic-original-bytes-")
    assert projected[2]["content"] == records[2]["text"]
    assert projected[3]["content"] == records[3]["text"]
    assert projected[4]["upload_blocker"]
    assert json.loads(projected[4]["content"])["source_status"] == "original_bytes_unavailable"
    assert "source_path" not in projected[5]
    assert projected[5]["content"] == "Stored extracted text.\n"


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
        "Be a thoughtful assistant.\n\n" + broker_instruction + "\n\n" + conversation_file_delivery_instructions().strip()
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


@pytest.mark.parametrize("carrier", ["body", "header", "legacy_small"])
def test_signed_bootstrap_transport_preserves_file_bytes_and_existing_header_limit(
    tmp_path, monkeypatch, carrier,
):
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(tmp_path / "synthetic-uploads"))
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    size = 200 * 1024 if carrier != "legacy_small" else 1024
    records = [
        {"file_id": "first", "filename": "01-first.txt", "source": "text", "text": "a" * size},
        {"file_id": "second", "filename": "02-second.txt", "source": "text", "text": "b" * size},
    ]
    bundle = {"viventium_upload_context": {"selected_uploads": records}}
    headers = {**AUTH, **_signed_bundle_headers(bundle)}
    if carrier == "body":
        payload["metadata"]["bootstrap_bundle_b64"] = headers.pop("X-GlassHive-Bootstrap-Bundle-B64")
    raw_body = json.dumps(payload, separators=(",", ":"))
    body_sha256 = hashlib.sha256(raw_body.encode()).hexdigest()
    headers.update({"Content-Type": "application/json",
                    "X-Viventium-Native-Invocation-Id": "synthetic-bootstrap-invocation",
                    "X-Viventium-Native-Body-SHA256": body_sha256})
    response = client.post("/v1/chat/completions", headers=headers, content=raw_body)

    if carrier == "header":
        assert response.status_code == 431, response.text
        assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []
        return
    assert response.status_code == 200, response.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    worker = client.app.state.store.get_worker(sessions[0]["worker_id"])
    admitted = json.loads(worker["bootstrap_bundle_json"])
    assert [(file["file_id"], file["path"], file["content"]) for file in admitted["files"]] == [
        (record["file_id"], "uploads/" + record["filename"], record["text"]) for record in records
    ]
    assert [len(file["content"].encode()) for file in admitted["files"]] == [size, size]
    assert "bootstrap_bundle_b64" not in admitted
    request_record = client.app.state.store.get_provider_request(response.json()["id"])
    assert request_record["native_invocation_id"] == "synthetic-bootstrap-invocation"
    assert request_record["native_body_sha256"] == body_sha256


def test_signed_bootstrap_body_does_not_silently_drop_context_without_native_read_route(tmp_path, monkeypatch):
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    monkeypatch.delenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", raising=False)
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    bundle = {"viventium_upload_context": {"selected_uploads": [
        {"file_id": "first", "filename": "01-first.txt", "source": "text", "text": "a" * (200 * 1024)},
        {"file_id": "second", "filename": "02-second.txt", "source": "text", "text": "b" * (200 * 1024)},
    ]}}
    headers = {**AUTH, **_signed_bundle_headers(bundle)}
    payload["metadata"]["bootstrap_bundle_b64"] = headers.pop("X-GlassHive-Bootstrap-Bundle-B64")
    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 502, response.text
    assert "no way to read the rest" in response.json()["error"]["message"]


@pytest.mark.parametrize("case, expected", [
    ("unsigned", 403), ("tampered", 403), ("expired", 403),
    ("both_identical", 400), ("both_different", 400),
])
def test_signed_bootstrap_body_fails_closed_before_provider_admission(
    tmp_path, monkeypatch, case, expected,
):
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    bundle = {"project_definition": "Exact synthetic signed authority."}
    signed = _signed_bundle_headers(bundle, timestamp=int(time.time()) - 601 if case == "expired" else None)
    encoded = signed.pop("X-GlassHive-Bootstrap-Bundle-B64")
    headers = {**AUTH, **signed}
    payload["metadata"]["bootstrap_bundle_b64"] = encoded
    if case == "unsigned":
        headers.pop("X-GlassHive-Bootstrap-Signature")
    elif case == "tampered":
        payload["metadata"]["bootstrap_bundle_b64"] = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    elif case.startswith("both_"):
        headers["X-GlassHive-Bootstrap-Bundle-B64"] = encoded
        if case == "both_different":
            payload["metadata"]["bootstrap_bundle_b64"] = encoded + "A"
    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == expected, response.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_unsigned_plain_bootstrap_body_cannot_supply_native_file_authority(tmp_path, monkeypatch):
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["metadata"]["bootstrap_bundle"] = {
        "viventium_upload_context": {"selected_uploads": [
            {"file_id": "forged", "filename": "forged.txt", "text": "Untrusted file bytes."},
        ]},
    }
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    admitted = json.loads(client.app.state.store.get_worker(session["worker_id"])["bootstrap_bundle_json"])
    assert not admitted.get("files")
    assert "Untrusted file bytes." not in json.dumps(admitted)


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


@pytest.mark.parametrize("context_usage,expected,scope", [
    ({"source": "sdk_last_response", "input_tokens": 185000, "output_tokens": 9}, 185000, "native_prompt_tokens"),
    (None, 365000, "native_prompt_tokens_upper_bound"),
    ({"source": "sdk_last_response", "input_tokens": 0, "output_tokens": 9}, 365000, "native_prompt_tokens_upper_bound"),
    ({"source": "unverified", "input_tokens": 10, "output_tokens": 1}, 365000, "native_prompt_tokens_upper_bound"),
    ({"source": "unavailable", "unmeasured_tool_results": True}, 500000, "native_prompt_tokens_upper_bound"),
])
def test_direct_context_measurement_does_not_charge_tool_round_sum_as_resident_context(
    tmp_path, monkeypatch, context_usage, expected, scope
):
    class DirectUsageRuntime(StubRuntime):
        def provider_activity_log(self, worker, run_id):
            event = {"type": "grok.result", "executor": "direct", "session_id": "direct:fixture",
                "stop_reason": "end_turn", "output": "Native answer.",
                "usage": {"input_tokens": 365000, "output_tokens": 18}}
            if context_usage is not None:
                event["context_usage"] = context_usage
            return "direct-xai", json.dumps({"type": "grok.session.started", "session_id": "direct:fixture",
                "model": "grok-4.7", "requestedReasoningEffort": "high"}) + "\n" + json.dumps(event)
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, DirectUsageRuntime())
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code,direct-xai")
    monkeypatch.setenv("XAI_API_KEY", "synthetic-not-used")
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace, model="direct-xai:grok-4.7"))
    assert response.status_code == 200, response.text
    assert response.json()["usage"]["prompt_tokens"] == 365000
    store = client.app.state.store
    request = store.get_provider_request(response.json()["id"])
    session = store.get_provider_session_by_id(request["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    assert manifest["latest_native_prompt_tokens"] == expected
    assert manifest["native_occupancy_scope"] == scope
    projection = ConversationProvider._native_occupancy_projection(GLASSHIVE_MODELS["direct-xai:grok-4.7"], manifest, {
        "instruction_bytes": 40, "output_reserve_tokens": 32768})
    assert projection["state"] == ("healthy" if expected == 185000 else "pressure")
    if scope.endswith("upper_bound"):
        assert "usage_calibration_request_ids" not in manifest


@pytest.mark.parametrize("measurement", [None, {}, {"source": "sdk_last_response", "input_tokens": True, "output_tokens": 1},
    {"source": "sdk_last_response", "input_tokens": -1, "output_tokens": 1},
    {"source": "sdk_last_response", "input_tokens": 400000, "output_tokens": 1},
    {"source": "sdk_last_response", "input_tokens": 180000, "output_tokens": 99}])
def test_missing_or_inconsistent_direct_response_usage_uses_only_conservative_bound(measurement):
    from workers_projects_runtime.conversation_provider import _native_context_usage
    billing = {"prompt_tokens": 365000, "completion_tokens": 18, "total_tokens": 365018}
    log = json.dumps({"type": "grok.result", "executor": "direct", "context_usage": measurement})
    for profile in ("direct-xai", "direct-openai", "direct-anthropic"):
        usage, scope = _native_context_usage(profile, log, billing, context_window=500000)
        assert usage == billing and scope == "native_prompt_tokens_upper_bound"
    unknown, scope = _native_context_usage("direct-xai", "", None, context_window=500000)
    assert unknown["prompt_tokens"] == 500000 and scope == "native_prompt_tokens_upper_bound"
    assert _native_context_usage("codex-cli", log, None, context_window=500000) == (None, "native_prompt_tokens")


def test_occupancy_bound_scope_can_recover_without_weakening_epoch_or_order_fences(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, NativeUsageRuntime())
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    store = client.app.state.store
    request = store.get_provider_request(response.json()["id"])
    session = store.get_provider_session_by_id(request["session_id"])
    epoch = json.loads(session["context_manifest_json"]).get("native_context_epoch", "")
    def record(identity, count, scope, timestamp, expected_epoch=epoch):
        store.record_provider_session_native_occupancy(session["session_id"], request_id=identity,
            prompt_tokens=count, total_tokens=count + 9, measurement_epoch=expected_epoch,
            measurement_scope=scope, measurement_created_at=timestamp)
        return json.loads(store.get_provider_session_by_id(session["session_id"])["context_manifest_json"])
    bound = record("new-bound", 365000, "native_prompt_tokens_upper_bound", "2090-01-01T00:00:00+00:00")
    assert bound["latest_native_prompt_tokens"] == 365000
    assert bound["native_occupancy_scope"] == "native_prompt_tokens_upper_bound"
    assert record("late", 1, "native_prompt_tokens", "2000-01-01T00:00:00+00:00")["latest_native_prompt_tokens"] == 365000
    assert record("wrong-epoch", 1, "native_prompt_tokens", "2091-01-01T00:00:00+00:00", "other")["latest_native_prompt_tokens"] == 365000
    assert record("wrong-scope", 1, "unrelated_scope", "2091-01-01T00:00:00+00:00")["latest_native_prompt_tokens"] == 365000
    measured = record("new-measured", 185000, "native_prompt_tokens", "2092-01-01T00:00:00+00:00")
    assert measured["latest_native_prompt_tokens"] == 185000
    assert measured["native_occupancy_scope"] == "native_prompt_tokens"


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


@pytest.mark.parametrize("media", [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}},
    {"type": "file", "file": {"filename": "result.pdf", "file_data": "c3ludGhldGlj"}},
])
def test_authored_visible_chain_matches_retained_media_without_invented_labels(media):
    from workers_projects_runtime.conversation_provider import (
        _visible_message_keys, _protected_source_indices, _message_delivery_indices,
        _message_text,
    )
    caption = "Inspect this retained attachment."
    message = ChatMessage(role="user", content=[{"type": "text", "text": caption}, media])
    chain = [{"id": "source-media", "role": "user", "sha256": hashlib.sha256(caption.encode()).hexdigest(),
              "accepted_source": True, "current_input": True}]
    payload = ChatCompletionRequest(model="codex-cli:configured", messages=[message],
                                    metadata={"visible_message_chain": chain})
    keys = _visible_message_keys(payload.messages, chain)
    assert keys[0].startswith("msg:source-media:")
    assert _protected_source_indices(payload, keys, current_input=True) == {0}
    # Prompt text keeps the existing attachment context; only the chain hash is authored text.
    assert "[Attached " in _message_text(message.content)
    changed_caption = message.model_copy(update={"content": [{"type": "text", "text": "Changed."}, media]})
    with pytest.raises(Exception) as rejected:
        changed = payload.model_copy(update={"messages": [changed_caption]})
        _protected_source_indices(changed, _visible_message_keys(changed.messages, chain))
    assert rejected.value.detail["code"] == "source_context_unavailable"
    for content in ([{"type": "text", "text": caption}],
                    [{"type": "text", "text": caption}, {**media, "filename": "changed"}]):
        changed = message.model_copy(update={"content": content})
        assert _visible_message_keys([changed], chain)[0] != keys[0]
        assert not ConversationProvider._native_source_coverage_proven(
            {"accepted_visible_message_keys": list(keys.values())}, {}, _visible_message_keys([changed], chain),
            new_native_session=False,
        )
    assistant = message.model_copy(update={"role": "assistant"})
    delivery_chain = [{"id": "answer-media", "role": "assistant", "sha256": chain[0]["sha256"],
                       "delivery": {"state": "committed", "revision": 1}}]
    answer = payload.model_copy(update={"messages": [assistant], "metadata": payload.metadata.model_copy(
        update={"visible_message_chain": delivery_chain})})
    assert _message_delivery_indices(answer, _visible_message_keys(answer.messages, delivery_chain)) == {
        0: delivery_chain[0]["delivery"]}


@pytest.mark.parametrize("content,authored", [
    (["One.", {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}}, "Two."], "One.\nTwo."),
    ([{"type": "output_text", "output_text": "Result."}], "Result."),
    ([{"type": "input_text", "input_text": "Input."}, {"type": "text", "text": "Caption."}], "Input.\nCaption."),
])
def test_authored_visible_chain_matches_core_known_text_shapes(content, authored):
    from workers_projects_runtime.conversation_provider import _visible_message_keys, _protected_source_indices
    chain = [{"id": "source-known", "role": "user", "sha256": hashlib.sha256(authored.encode()).hexdigest(),
              "accepted_source": True, "current_input": True}]
    payload = ChatCompletionRequest(model="codex-cli:configured", messages=[{"role": "user", "content": content}],
                                    metadata={"visible_message_chain": chain})
    keys = _visible_message_keys(payload.messages, chain)
    assert keys[0].startswith("msg:source-known:")
    assert _protected_source_indices(payload, keys, current_input=True) == {0}


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


@pytest.mark.parametrize("prior_log", ["present", "empty", "unsupported", "collector_missing", "oversized_replay"])
def test_native_pressure_defers_when_prior_native_tool_evidence_is_not_replayed(
    tmp_path, monkeypatch, prior_log
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = NativeToolEvidenceRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    retained_source = "The limit is 731 units." if prior_log != "oversized_replay" else "x" * 150000
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
    if prior_log == "oversized_replay":
        # The authorized delta fits. A full replay would exceed the byte budget,
        # but cannot be used because native-only tool evidence still owns this epoch.
        second_payload["metadata"]["turn_context"] = "y" * 70000
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
    if prior_log == "oversized_replay":
        assert decision["mode"] == "delta"
        assert decision["instruction_bytes"] < decision["budget_bytes"]
        assert retained_source not in second_request["admitted_instruction"]
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
    assert denied.json() == {
        "id": "",
        "object": "glasshive.request",
        "state": "cancelled",
        "capacityReleased": True,
        "responseTimeoutS": 660.0,
    }
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
            # This legacy row has no retained response contract: its state,
            # activity and history settle in separate writes within one pass.
            with provider._detached_reconciliation_lock:
                pending = request_record["request_id"] in provider._detached_reconciliations
            if not pending and store.get_provider_request(request_record["request_id"])["state"] == "completed":
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
            "path": ('/U' + 'sers/private-person/Documents/secret-project/file.txt'),
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

    visible = redactor.feed(('Path /U' + 'sers/synthetic/\nDocuments/private.txt\n'))
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

    visible = redactor.feed(('Safe prefix. --' + '---BEGIN PRIVATE KEY-----\n'))
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


def test_native_visible_text_delivers_every_claude_turn_result_in_order():
    # Each native `result` ends one authored turn. A later turn, such as one a finished
    # background task starts, may refer to an earlier one, so the reply carries them all.
    turns = [
        "None of the three tasks is finished yet.",
        "Two workers finished. The poem is:\n\n> The moon leans close",
        "Working through the last one.\nFINAL REPORT:\nAll three finished; the poem is in my last message.",
    ]
    raw = "\n".join(
        line
        for text in turns
        for line in (
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": text}),
        )
    )

    assert _native_visible_text("claude-code", raw) == "\n\n".join(
        [turns[0], turns[1], "All three finished; the poem is in my last message."]
    )


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


@pytest.mark.parametrize("case, expected", [
    ("empty", ""),
    ("visible", "Ready."),
    ("unfinished", None),
    ("later_tool", None),
    ("late_tool", None),
    ("late_tool_update", None),
    ("missing_author", None),
    ("invalid_author", None),
    ("failed", None),
])
def test_codex_terminal_presence_requires_the_last_completed_authored_item(case, expected):
    tool = {"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "synthetic-broker", "tool": "synthetic_tool",
        "status": "completed", "error": None,
    }}
    authored = {"type": "item.completed", "item": {
        "type": "agent_message", "text": "Ready." if case == "visible" else "",
    }}
    events = [{"type": "thread.started", "thread_id": "synthetic-session"},
              {"type": "turn.started"}, tool, authored]
    if case == "missing_author":
        events.pop()
    elif case == "invalid_author":
        authored["item"]["text"] = None
    elif case == "later_tool":
        events.append(tool)
    if case != "unfinished":
        events.append({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}})
    if case == "late_tool":
        events.append(tool)
    elif case == "late_tool_update":
        events.append({"type": "item.updated", "item": {**tool["item"], "status": "in_progress"}})
    elif case == "failed":
        events.append({"type": "turn.failed", "error": {"message": "Synthetic failure"}})
    stdout = "\n".join(json.dumps(event) for event in events)

    assert _native_terminal_text("codex-cli", stdout) == expected
    assert _native_visible_text("codex-cli", stdout) == (expected or "")


def test_empty_native_terminal_is_an_empty_main_result_and_never_reinvokes(tmp_path, monkeypatch):
    class EmptyTerminalRuntime(StubRuntime):
        calls = 0

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            self.calls += 1
            _publish_in_process_test_start(worker)
            return "The harness completed without a user-facing response."

        def provider_activity_log(self, worker, run_id):
            return "codex-cli", "\n".join(json.dumps(event) for event in [
                {"type": "item.completed", "item": {"type": "agent_message", "text": ""}},
                {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}},
            ])

    runtime = EmptyTerminalRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(tmp_path, model="codex-cli:gpt-6.1-sol")
    headers = {**AUTH, "X-GlassHive-Fallback-Model": "",
               "X-GlassHive-Fallback-Reasoning-Effort": ""}

    for _ in range(2):
        response = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == "provider_response_failed"
    failed = client.app.state.store.list_provider_requests_by_state({"failed"})
    assert len(failed) == 1
    run = client.app.state.store.get_run(failed[0]["run_id"])
    assert run["state"] == "failed"
    assert run["failure_class"] == "provider_response_failed"
    assert not failed[0]["fallback_state"]
    assert run["retry_attempts"] == 0
    assert runtime.calls == 1


@pytest.mark.parametrize("case", ["eligible", "skip", "malformed", "plain", "new_turn", "failed", "unfinished", "unframed"])
def test_codex_quiet_terminal_keeps_only_valid_current_turn_public_content(case):
    from workers_projects_runtime.agent_builder_control import parse_conversation_output

    delivery = {"version": 1, "audio_eligible": True}
    prior = json.dumps({"type": "assistant_response", "tool_name": None,
                        "content": "Observed answer.", "voice": "skip" if case == "skip" else "eligible"})
    if case == "malformed":
        prior = json.dumps({"type": "assistant_response", "tool_name": "undeclared", "content": "Unsafe.", "voice": "eligible"})
    elif case == "plain":
        prior = "Private working draft."
    quiet = json.dumps({"type": "assistant_response", "tool_name": None, "content": "", "voice": "skip"})
    events = [{"type": "turn.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": prior}},
              {"type": "item.started", "item": {"type": "context_compaction"}},
              {"type": "item.completed", "item": {"type": "context_compaction"}},
              {"type": "item.completed", "item": {"type": "reasoning"}}]
    if case == "new_turn":
        events += [{"type": "turn.completed"}, {"type": "turn.started"}]
    events.append({"type": "item.completed", "item": {"type": "agent_message", "text": quiet}})
    if case != "unfinished":
        events.append({"type": "turn.failed" if case == "failed" else "turn.completed"})
    output = _native_terminal_text("codex-cli", "\n".join(map(json.dumps, events)),
                                   delivery_control=None if case == "unframed" else delivery)
    if case in {"failed", "unfinished"}:
        assert output is None
        return
    parsed = parse_conversation_output(output, None, delivery)
    assert parsed["content"].strip() == ("Observed answer." if case in {"eligible", "skip"} else "")
    assert parsed["delivery_disposition"]["audio"] == ("eligible" if case == "eligible" else "skip")


def test_codex_authored_answer_survives_compaction_quiet_terminal_and_exact_replay(tmp_path, monkeypatch):
    answer = json.dumps({"type": "assistant_response", "tool_name": None, "content": "Observed answer.", "voice": "eligible"})
    quiet = json.dumps({"type": "assistant_response", "tool_name": None, "content": "", "voice": "skip"})

    class CompactedTerminalRuntime(StubRuntime):
        calls = 0

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            self.calls += 1
            _publish_in_process_test_start(worker)
            return quiet

        def provider_activity_log(self, worker, run_id):
            return "codex-cli", "\n".join(map(json.dumps, [
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
                {"type": "item.started", "item": {"type": "context_compaction"}},
                {"type": "item.completed", "item": {"type": "context_compaction"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": quiet}},
                {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}},
            ]))

    runtime = CompactedTerminalRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(tmp_path, model="codex-cli:gpt-6.1-sol")
    payload["metadata"]["audio_eligible"] = True
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    message = response.json()["choices"][0]["message"]
    assert message["content"].strip() == "Observed answer."
    assert message["provider_specific_fields"]["viventium"]["delivery_disposition"]["audio"] == "eligible"
    replay = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert replay.json() == response.json()
    assert runtime.calls == 1


@pytest.mark.parametrize("voice", ["eligible", "skip"])
def test_codex_terminal_graph_transfer_is_not_replaced_by_earlier_public_content(voice):
    graph = {"version": 1, "tools": [{"name": "lc_transfer_to_specialist", "description": "Consult."}]}
    delivery = {"version": 1, "audio_eligible": True}
    prior = json.dumps({"type": "assistant_response", "tool_name": None, "content": "Observed answer.", "voice": "eligible"})
    transfer = json.dumps({"type": "tool_call", "tool_name": "lc_transfer_to_specialist", "content": "Consulting.", "voice": voice})
    stdout = "\n".join(map(json.dumps, [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": prior}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": transfer}},
        {"type": "turn.completed"},
    ]))
    assert _native_terminal_text("codex-cli", stdout, graph, delivery) == transfer


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


def _claude_final(text):
    return "\n".join((json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}),
                      json.dumps({"type": "result", "result": text})))


def _codex_final(text):
    return "\n".join((json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
                      json.dumps({"type": "turn.completed"})))


@pytest.mark.parametrize("profile, stdout", [
    ("claude-code", _claude_final("I saved it.\n\nFINAL REPORT:\n`tides.md` is saved.")),
    ("codex-cli", _codex_final("I saved it.\n\nFINAL REPORT:\n`tides.md` is saved.")),
])
def test_native_visible_text_shows_only_the_final_report_for_every_harness(profile, stdout):
    # A conversation reply is the user-facing report, as for Grok and worker runs: never the
    # narration before it, the marker itself, or the same answer twice.
    assert _native_visible_text(profile, stdout) == "`tides.md` is saved."


@pytest.mark.parametrize("profile, stdout", [
    ("claude-code", _claude_final("Hello from LIFE.")), ("codex-cli", _codex_final("Hello from LIFE.")),
])
def test_native_visible_text_keeps_a_reply_without_a_final_report(profile, stdout):
    assert _native_visible_text(profile, stdout) == "Hello from LIFE."


@pytest.mark.parametrize("profile", ["claude-code", "codex-cli", "grok-build"])
def test_native_visible_text_keeps_an_indented_literal_heading(profile):
    reply = "    FINAL REPORT:\n    Status: ready"
    stdout = {
        "claude-code": _claude_final(reply),
        "codex-cli": _codex_final(reply),
        "grok-build": "\n".join((json.dumps({"type": "grok.session.started", "session_id": "s"}),
                                 json.dumps({"type": "grok.result", "session_id": "s", "stop_reason": "end_turn", "output": reply}))),
    }[profile]
    assert _native_visible_text(profile, stdout) == "FINAL REPORT:\n    Status: ready"


@pytest.mark.parametrize("profile", ["claude-code", "codex-cli", "grok-build"])
def test_native_visible_text_keeps_a_literal_final_report_inside_the_reply(profile):
    reply = "Drafted.\n\nFINAL REPORT:\nHere is document:\n```text\nTitle: Notes\nFINAL REPORT:\nStatus: ready\n```"
    stdout = {
        "claude-code": _claude_final(reply),
        "codex-cli": _codex_final(reply),
        "grok-build": "\n".join((json.dumps({"type": "grok.session.started", "session_id": "s"}),
                                 json.dumps({"type": "grok.result", "session_id": "s", "stop_reason": "end_turn", "output": reply}))),
    }[profile]
    assert _native_visible_text(profile, stdout) == "Here is document:\n```text\nTitle: Notes\nFINAL REPORT:\nStatus: ready\n```"


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
                        "changes": [{"path": ('/U' + 'sers/private-person/Documents/private.txt')}],
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
                        "input": {"file_path": ('/U' + 'sers/private-person/Documents/private.txt')},
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
            "Changed synthetic background authority.\n\n" + conversation_file_delivery_instructions().strip()
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


def test_family_stop_reports_release_only_after_every_exact_run_releases(tmp_path, monkeypatch):
    runtime = BlockingFamilyStopRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    provider = client.app.state.conversation_provider
    store = client.app.state.store
    payload = _family_stop_payload(tmp_path)
    try:
        record = provider.start(ChatCompletionRequest.model_validate(payload))
        assert runtime.started.wait(timeout=5)
        done = {"request_id": "absent", "state": "completed", "run_id": ""}
        # Release covers every record in the family, whichever record is last.
        assert provider._family_capacity_released([done, record]) is False
        assert provider._family_capacity_released([record, done]) is False

        retained = {record["run_id"]}
        exact_lease = store.get_active_host_run_lease_for_run
        monkeypatch.setattr(
            store,
            "get_active_host_run_lease_for_run",
            lambda run_id: {"run_id": run_id} if run_id in retained else exact_lease(run_id),
        )
        stopped = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)

        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "cancelled"
        assert store.get_run(record["run_id"])["state"] == "cancelled"
        # Terminal state alone is not release while the exact execution lease is retained.
        assert stopped.json()["capacityReleased"] is False
        retained.clear()
        released = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
        assert released.json()["capacityReleased"] is True
        assert released.json()["id"] == record["request_id"]
        # The owner of the deadline states it; a waiting successor does not guess it.
        assert released.json()["responseTimeoutS"] == provider.effective_response_timeout_seconds()
        refreshed = store.get_provider_request(record["request_id"])
        assert provider._family_capacity_released([done, refreshed]) is True
        # A named run that cannot be found proves nothing; a stop before any run exists does.
        missing = {**refreshed, "run_id": "run-that-does-not-exist"}
        assert provider._family_capacity_released([missing]) is False
        assert provider._family_capacity_released([{**refreshed, "run_id": ""}]) is True
    finally:
        runtime.release.set()


def test_revision_start_anchors_the_provider_response_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", "120")
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    started = datetime.now(UTC) - timedelta(seconds=30)
    payload = _payload(workspace)
    payload["metadata"]["response_started_at"] = started.isoformat()

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    record = client.app.state.store.get_provider_request(response.json()["id"])
    deadline = datetime.fromisoformat(record["response_deadline_at"])
    assert abs((deadline - (started + timedelta(seconds=120))).total_seconds()) < 1


def test_future_revision_start_never_extends_the_provider_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", "120")
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["metadata"]["response_started_at"] = (
        datetime.now(UTC) + timedelta(hours=1)
    ).isoformat()

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    record = client.app.state.store.get_provider_request(response.json()["id"])
    deadline = datetime.fromisoformat(record["response_deadline_at"])
    assert deadline <= datetime.now(UTC) + timedelta(seconds=121)


def test_expired_revision_start_is_refused_before_native_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", "120")
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["metadata"]["response_started_at"] = (
        datetime.now(UTC) - timedelta(seconds=200)
    ).isoformat()

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_response_deadline_exceeded"
    store = client.app.state.store
    assert store.list_provider_sessions(owner_id="owner-a") == []
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM provider_requests").fetchone()[0] == 0


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
    # This command-only fixture supplies the same private authority file that the
    # normal host runtime materializes before launch.
    authority_path = runtime._state_dir(worker["worker_id"]) / "developer-instructions.txt"
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_text(bundle["developer_instructions"])
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
            "capacityClass": "resource_pressure",
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


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("content", ["", " \n\t"])
def test_empty_authored_answer_is_a_typed_provider_failure(tmp_path, monkeypatch, stream, content):
    class EmptyAnswerRuntime(StubRuntime):
        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            _publish_in_process_test_start(worker)
            return json.dumps({"type": "assistant_response", "content": content, "tool_name": None})

    client = _client(tmp_path, monkeypatch, runtime=EmptyAnswerRuntime())
    payload = _payload(tmp_path, stream=stream)
    payload["tools"] = [{"type": "function", "function": {
        "name": "lc_transfer_to_specialist", "description": "Consult a specialist.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }}]
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    if stream:
        assert response.status_code == 200
        assert '"code":"provider_response_failed"' in response.text
    else:
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "provider_response_failed"
    failed = client.app.state.store.list_provider_requests_by_state({"failed"})
    assert len(failed) == 1
    run = client.app.state.store.get_run(failed[0]["run_id"])
    assert run["state"] == "failed"
    assert run["failure_class"] == "provider_response_failed"
    assert run["failure_retryable"] == 1
    assert run["output_text"] == json.dumps({"type": "assistant_response", "content": content, "tool_name": None})
    assert not failed[0]["response_json"]


@pytest.mark.parametrize("kind", ["answer", "transfer", "explicit_silence", "background"])
def test_empty_answer_guard_preserves_answer_transfer_and_declared_silence(tmp_path, monkeypatch, kind):
    class AuthoredAnswerRuntime(StubRuntime):
        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            _publish_in_process_test_start(worker)
            output = {"type": "assistant_response", "content": "Ready." if kind == "answer" else "", "tool_name": None}
            if kind == "transfer":
                output.update(type="tool_call", tool_name="lc_transfer_to_specialist")
            if kind == "explicit_silence":
                output["voice"] = "skip"
            return json.dumps(output)

    client = _client(tmp_path, monkeypatch, runtime=AuthoredAnswerRuntime())
    payload = _payload(tmp_path)
    payload["tools"] = [{"type": "function", "function": {
        "name": "lc_transfer_to_specialist", "description": "Consult a specialist.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }}]
    if kind == "explicit_silence":
        payload["metadata"]["audio_eligible"] = True
    headers = {**AUTH, "X-Viventium-Actor-Kind": "system", "X-Viventium-Origin": "scheduler"} if kind == "background" else AUTH
    response = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["finish_reason"] == ("tool_calls" if kind == "transfer" else "stop")


def test_typed_failure_classes_reach_the_client_error_code():
    from workers_projects_runtime.conversation_provider import _provider_failure_error
    for failure_class in ("provider_unavailable", "native_mcp_unavailable", "provider_auth_missing", "provider_quota_exhausted", "provider_progress_stalled", "host_capacity", "native_input_declined", "native_input_expired", "native_input_cancelled", "native_turn_cancelled"):
        assert _provider_failure_error({"failure_class": failure_class}) == (
            "glasshive_runtime_error", failure_class
        )
    assert _provider_failure_error({"failure_class": "unknown"}) == (
        "glasshive_runtime_error", "server_error"
    )


@pytest.mark.parametrize("stream", [False, True])
def test_provider_liveness_attention_keeps_typed_code_and_rejects_late_answer(
    tmp_path, monkeypatch, stream
):
    class AttentionRuntime(StubRuntime):
        def set_provider_liveness_observer(self, observer):
            self.observe_liveness = observer

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            _publish_in_process_test_start(worker)
            for sequence in range(1, 4):
                self.observe_liveness({
                    "worker_id": worker["worker_id"], "run_id": run_id,
                    "attempt_id": worker["_run_attempt_id"], "kind": "internal_retry",
                    "failure_class": "provider_internal_retry", "runtime": "codex-cli",
                    "model": worker["model"], "source_sequence": sequence,
                    "source_digest": hashlib.sha256(f"boundary-retry-{sequence}".encode()).hexdigest(),
                })
            return "Late answer from the closed attempt."

    client = _client(tmp_path, monkeypatch, runtime=AttentionRuntime())
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(tmp_path, stream=stream))
    if stream:
        assert response.status_code == 200
        assert '"code":"provider_progress_stalled"' in response.text
        assert "Late answer from the closed attempt." not in response.text
        assert '"code":"server_error"' not in response.text
    else:
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "provider_progress_stalled"
    failed = client.app.state.store.list_provider_requests_by_state({"failed"})
    assert len(failed) == 1
    run = client.app.state.store.get_run(failed[0]["run_id"])
    assert run["state"] == "needs_input"
    assert run["failure_class"] == "provider_progress_stalled"
    assert run["provider_liveness_route_locked"] == 1
    assert not failed[0]["response_json"]


@pytest.mark.parametrize("provider_path", [True, False])
def test_missing_provider_login_keeps_its_typed_code_on_each_protocol(tmp_path, monkeypatch, provider_path):
    from workers_projects_runtime.openclaw_runtime import ProviderAuthenticationMissingError

    client = _client(tmp_path, monkeypatch)

    def reject(*_args, **_kwargs):
        raise ProviderAuthenticationMissingError(
            "Installed Claude Code login is unavailable; reconnect through the installed native owner.",
            runtime_name="claude-code", profile="claude-code", execution_mode="host",
            dependency_label="Claude Code authentication",
            recovery_hint="Reconnect the installed Claude Code login, then retry the same request.",
        )

    if provider_path:
        monkeypatch.setattr(client.app.state.conversation_provider, "start", reject)
        response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(tmp_path, model="claude-code:opus"))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "provider_auth_missing"
        assert set(response.json()) == {"error"}
    else:
        def reject_rest():
            reject()
        client.app.get("/dependency-fixture")(reject_rest)
        response = client.get("/dependency-fixture", headers={"Authorization": "Bearer runtime-admin-token"})
        assert response.status_code == 409
        assert response.json()["status"] == "blocked"
        assert response.json()["failure_class"] == "provider_auth_missing"
    import sqlite3
    with sqlite3.connect(tmp_path / "runtime.db") as db:
        assert db.execute("SELECT COUNT(*) FROM provider_requests").fetchone()[0] == 0


def test_failed_run_streams_its_typed_class(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=ProviderAuthMissingRuntime())
    with client.stream(
        "POST", "/v1/chat/completions", headers=AUTH, json=_payload(workspace, stream=True)
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert '"code":"provider_auth_missing"' in body
    assert body.count('"error"') == 1


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


@pytest.mark.parametrize("content", ["", [], [{"type": "input_audio", "input_audio": {"data": "synthetic"}}]])
def test_empty_current_source_keeps_its_message_and_delegation_identity(content):
    from workers_projects_runtime.conversation_provider import _admit_conversation_history
    messages = [
        ChatMessage(role="user", content="Name the earlier shapes."),
        ChatMessage(role="assistant", content="A square and a circle."),
        ChatMessage(role="user", content=content),
    ]
    instruction, decision, _ = _admit_conversation_history(
        messages, start_at=0, turn_context="", model=next(iter(GLASSHIVE_MODELS.values())),
        current_input_indices={2}, source_ordinals_by_index={2: [1]},
        attachment_context="[Attached current original]",
    )
    historical, current = instruction.split("<viventium_current_accepted_turn_v1>", 1)
    assert '[message 2 user]\n<delegation_source>{"sourceOrdinals":[1]}</delegation_source>' in current
    assert "[Attached current original]" in current
    assert "Name the earlier shapes." in historical and "Name the earlier shapes." not in current
    assert "A square and a circle." not in current
    assert 2 in decision["admitted_message_indices"]
    assert messages[-1].content == content


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


@pytest.mark.parametrize("actual_model", [None, "grok-build:grok-4.7"])
def test_authored_preview_stream_keeps_interim_out_of_final_and_graph_authority(monkeypatch, tmp_path, actual_model):
    from types import SimpleNamespace
    provider = object.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))
    payload.tools = [{"type": "function", "function": {"name": "lc_transfer_to_check", "parameters": {"type": "object"}}}]
    record = {"request_id": "request-a", "run_id": "run-a", "session_id": "session-a", "owner_id": "owner-a",
              "message_id": "message-a", "native_invocation_id": "invocation-a", "state": "running",
              "created_at": datetime.now(UTC).isoformat()}
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
    def unexpected_terminal_snapshot(*_):
        raise AssertionError('Structured graph streams must not collect unused terminal text')
    provider._native_output_snapshot = unexpected_terminal_snapshot
    canonical = {"model": actual_model or payload.model, "choices": [{"message": {"role": "assistant", "content": "Final answer only."},
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
    assert chunks[-1]["model"] == canonical["model"]
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
                                   "changed_tool_binding", "stale_predecessor", "legacy_provenance", "admission_race", "active_predecessor", "inserted_history", "unadmitted_revision", "missing_unadmitted_proof", "wrong_unadmitted_revision", "reordered_unadmitted_revisions"] )
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
    if change in {"unadmitted_revision", "missing_unadmitted_proof", "wrong_unadmitted_revision", "reordered_unadmitted_revisions"}:
        payload["metadata"]["logical_turn_revision"] = 3
        proof["revision"] = 3
        if change != "missing_unadmitted_proof":
            proof["unadmitted_revisions"] = [2] if change == "unadmitted_revision" else [1]
        if change == "reordered_unadmitted_revisions":
            payload["metadata"]["logical_turn_revision"] = 4
            proof["revision"] = 4
            proof["unadmitted_revisions"] = [3, 2]
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
    if change not in {"valid", "unadmitted_revision"}:
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
    assert model.recommended_effort == ("high" if native_model == "claude-opus-5-5" else "medium")
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


def test_native_input_route_is_authenticated_and_owner_scoped(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    completed = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    assert completed.status_code == 200
    url = "/v1/requests/by-idempotency/idem-a/native-input"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={**AUTH, "X-Viventium-User-Id": "owner-b"}).status_code == 404
    response = client.get(url, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"version": 1, "state": "completed", "pending": []}
    assert client.post(url, headers=AUTH, json={"version":1}).status_code == 400


@pytest.mark.parametrize('audio_eligible', [False, True])
def test_native_input_base_key_resolves_the_exact_owner_graph_child(tmp_path, monkeypatch, audio_eligible):
    runtime = BlockingFamilyStopRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _family_stop_payload(tmp_path)
    payload['metadata']['audio_eligible'] = audio_eligible
    record = client.app.state.conversation_provider.start(ChatCompletionRequest.model_validate(payload))
    try:
        assert runtime.started.wait(timeout=5)
        assert ':graph:' in record['idempotency_key']
        observed = []
        monkeypatch.setattr('workers_projects_runtime.conversation_provider.native_input_state',
            lambda provider, current: observed.append(current['request_id']) or
                {'version': 1, 'state': current['state'], 'pending': []})
        url = '/v1/requests/by-idempotency/idem-a/native-input'
        assert client.get(url, headers={**AUTH, 'X-Viventium-User-Id': 'owner-b'}).status_code == 404
        assert observed == []
        result = client.get(url, headers=AUTH)
        assert result.status_code == 200
        assert observed == [record['request_id']]
    finally:
        runtime.release.set()


@pytest.mark.parametrize("available", [["grok-4.7", "grok-4.7-build-fast"], ["grok-4.7"], []])
def test_optional_grok_fast_turn_does_not_use_service_account_catalog(monkeypatch, available):
    calls = []
    monkeypatch.setattr("workers_projects_runtime.conversation_provider.native_grok_models",
                        lambda: calls.append(True) or available)
    provider = ConversationProvider.__new__(ConversationProvider)
    selected = provider._model("grok-build:grok-4.7-build-fast")
    assert selected.native_model == "grok-4.7-build-fast"
    assert calls == []
    assert selected.recommended_effort == "high"


def test_grok_graph_contract_stays_typed_with_exact_pinned_authority():
    capsule = '<viventium_feeling_state>synthetic</viventium_feeling_state>'
    payload = ChatCompletionRequest.model_validate({
        'model': 'grok-build:grok-4.7',
        'messages': [{'role': 'system', 'content': 'Synthetic application authority.\n'+capsule},
                     {'role': 'user', 'content': 'Consider the decision.'}],
        'metadata': {'glasshive_options': {'access': 'full'}, 'developer_instruction_tail': capsule},
    })
    control = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult specialist.'}]}
    provider = ConversationProvider.__new__(ConversationProvider)
    bundle = provider._native_bundle(payload, GLASSHIVE_MODELS[payload.model], 'high', graph_control=control)
    authority = bundle['developer_instructions']
    assert authority.endswith(capsule)
    assert authority.count(capsule) == 1
    assert bundle['agent_builder_control'] == control
    assert 'Put the user-facing answer in content.' not in authority
    assert bundle['application_developer_instructions'] == 'Synthetic application authority.\n\n'+capsule


def test_grok_preview_requires_complete_typed_answer_and_discards_pre_tool_segment():
    from workers_projects_runtime.conversation_provider import _native_authored_preview
    control = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult specialist.'}]}
    answer = json.dumps({'type': 'assistant_response', 'content': 'Try a reversible test.', 'tool_name': None})
    def event(text):
        return json.dumps({'type': 'grok.session.update', 'update': {
            'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}})
    partial = event(answer[:20])
    assert _native_authored_preview('grok-build', partial, control, None) is None
    complete = partial + '\n' + event(answer[20:])
    assert _native_authored_preview('grok-build', complete, control, None) == {
        'sequence': 2, 'text': 'Try a reversible test.'}
    assert _native_authored_preview('grok-build', complete, None, None) is None
    tool = json.dumps({'type':'grok.session.update', 'update': {'sessionUpdate':'tool_call'}})
    assert _native_authored_preview('grok-build', complete+'\n'+tool+'\n'+event('Progress.'), control, None) is None


@pytest.mark.parametrize('second', ['The answer.', 'A useful addition.'])
def test_native_generation_projection_preserves_distinct_completed_generations(second):
    from workers_projects_runtime.conversation_provider import _native_spoken_content, _native_authored_preview
    events = [{'type': 'grok.session.started', 'session_id': 'native-exact'}]
    def chunk(text, stream):
        return {'type': 'grok.session.update', 'session_id': 'native-exact', 'meta': {
            'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}, 'update': {
            'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}
    def envelope(text):
        return json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': text})
    delivery = {'version': 1, 'audio_eligible': True}
    first = envelope('The answer.')
    events.extend([chunk(first, 100), {'type': 'grok.session.update', 'session_id': 'native-exact',
        'update': {'sessionUpdate': 'response_completed'}}, chunk(envelope(second)[:80], 200)])
    partial = _native_spoken_content('grok-build', '\n'.join(map(json.dumps, events)), None, delivery)
    assert partial['text'] == 'The answer.'
    events.append(chunk(envelope(second)[80:], 200))
    raw = '\n'.join(map(json.dumps, events))
    expected = 'The answer.\n\n' + second
    result = _native_spoken_content('grok-build', raw, None, delivery)
    assert result['text'] == expected
    assert len(result['completed_parts']) == 2
    assert _native_authored_preview('grok-build', raw, None, delivery)['text'] == expected


@pytest.mark.parametrize('first_voice', ['eligible', 'skip'])
@pytest.mark.parametrize('final_voice', ['eligible', 'skip'])
def test_native_tool_retirement_keeps_spoken_and_authored_projection(first_voice, final_voice):
    from workers_projects_runtime.conversation_provider import _native_spoken_content, _native_authored_preview
    events = [{'type': 'grok.session.started', 'session_id': 'native-exact'}]
    def put(kind, text='', stream=100):
        events.append({'type': 'grok.session.update', 'session_id': 'native-exact', 'meta': {
            'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}, 'update': {
            'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}})
    envelope = lambda voice: json.dumps({'type': 'assistant_response', 'tool_name': None,
                                         'voice': voice, 'content': 'Same public text.'})
    put('agent_message_chunk', envelope(first_voice))
    put('response_completed')
    for _ in range(2):
        put('tool_call')
        put('agent_thought_chunk', 'Private reasoning.')
        put('response_completed')
    put('agent_message_chunk', envelope(final_voice), 200)
    put('response_completed', stream=200)
    raw = '\n'.join(map(json.dumps, events))
    delivery = {'version': 1, 'audio_eligible': True}
    expected = 'Same public text.\n\nSame public text.'
    projected = _native_spoken_content('grok-build', raw, None, delivery)
    assert projected['text'] == expected  # Public text and audio intent are separate.
    expected_parts = ([{'part_index': 1, 'text_end': len('Same public text.')}] if first_voice == 'eligible' else [])
    if final_voice == 'eligible':
        expected_parts.append({'part_index': 4, 'text_end': len(expected)})
    assert projected['completed_parts'] == expected_parts
    assert [part['delivery_disposition']['audio'] for part in projected['presentation_parts']] == [first_voice, final_voice]
    assert _native_authored_preview('grok-build', raw, None, delivery)['text'] == expected


@pytest.mark.parametrize('boundary', ['tool', 'generation'])
@pytest.mark.parametrize('kind,voice', [('assistant_response', 'skip'), ('tool_call', 'eligible'), ('tool_call', 'skip')])
def test_native_generation_terminal_stream_controls_match_existing_tool_parts(monkeypatch, tmp_path, boundary, kind, voice, first_voice='eligible'):
    from types import SimpleNamespace
    from workers_projects_runtime.agent_builder_control import NativeConversationParts, parse_conversation_output
    from workers_projects_runtime.conversation_provider import _native_spoken_content, _native_authored_preview
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    delivery = {'version': 1, 'audio_eligible': True}
    first = json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': first_voice, 'content': 'I will check.'})
    final = {'type': kind, 'tool_name': 'lc_transfer_to_specialist' if kind == 'tool_call' else None,
             'voice': voice, 'content': 'The final contribution.'}
    events = [{'type': 'grok.session.started', 'session_id': 'native-a'}]
    def put(kind, text='', stream=100):
        events.append({'type': 'grok.session.update', 'session_id': 'native-a', 'meta': {
            'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}, 'update': {
            'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}})
    put('agent_message_chunk', first)
    initial = '\n'.join(map(json.dumps, events))
    put('tool_call' if boundary == 'tool' else 'response_completed')
    stream = 200 if boundary == 'generation' else 100
    put('agent_message_chunk', json.dumps(final), stream)
    put('response_completed', stream=stream)
    full = '\n'.join(map(json.dumps, events))
    parts = NativeConversationParts(graph, delivery)
    for event in events[1:]:
        parts.update(event['update'], event['meta'])
    canonical = parse_conversation_output(parts.finish(), graph, delivery)
    assert canonical['content'] == 'I will check.\n\nThe final contribution.'
    expected_terminal_voice = voice if kind == 'tool_call' else ('eligible' if first_voice == voice == 'eligible' else 'skip')
    assert canonical['type'] == kind and canonical['delivery_disposition']['audio'] == expected_terminal_voice
    projected = _native_spoken_content('grok-build', full, graph, delivery)
    expected_text = 'I will check.\n\nThe final contribution.'
    withheld_graph_text = kind == 'tool_call' and voice == 'skip'
    expected_projection = 'I will check.' if withheld_graph_text else expected_text
    expected_parts = ([{'part_index': 1, 'text_end': len('I will check.')}] if first_voice == 'eligible' else [])
    if voice == 'eligible':
        expected_parts.append({'part_index': 2, 'text_end': len(expected_text)})
    assert projected['text'] == expected_projection
    assert projected['completed_parts'] == expected_parts
    assert [part['delivery_disposition']['audio'] for part in projected['presentation_parts']] == ([first_voice] if withheld_graph_text else [first_voice, voice])
    preview = _native_authored_preview('grok-build', full, graph, delivery)
    if kind == 'assistant_response':
        assert preview['text'] == canonical['content']
    elif boundary == 'tool':
        assert preview is None
    else:
        assert preview['text'] == 'I will check.'  # Replaceable prior assistant preview, never graph authority.

    provider = object.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))
    payload.metadata.audio_eligible = True
    payload.metadata.actor_kind = 'external_user'
    payload.metadata.origin = 'interactive'
    payload.tools = [{'type': 'function', 'function': {'name': 'lc_transfer_to_specialist',
        'description': 'Consult.', 'parameters': {'type': 'object', 'properties': {},
                                                'required': [], 'additionalProperties': False}}}]
    record = {'request_id': 'request-a', 'run_id': 'run-a', 'session_id': 'session-a', 'owner_id': 'owner-a',
              'message_id': 'message-a', 'stream_id': 'stream-a', 'native_invocation_id': 'invocation-a',
              'state': 'running', 'created_at': datetime.now(UTC).isoformat()}
    stage = {'value': 0}
    def get_record(_):
        stage['value'] += 1
        return {**record, 'state': 'completed' if stage['value'] == 3 else 'running'}
    receipts = []
    provider.store = SimpleNamespace(get_provider_request=get_record, get_run=lambda _: {'run_id': 'run-a'},
        get_provider_session_by_id=lambda _: {'owner_id': 'owner-a'}, get_worker=lambda _: {'owner_id': 'owner-a'},
        list_provider_activity=lambda _: [], add_provider_activity_once=lambda *args: receipts.append(args))
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda *_:
        ('grok-build', initial if stage['value'] == 1 else full)))
    provider._sync = lambda value: value
    provider.response_payload = lambda *_: {'model': payload.model, 'choices': [{'message': {
        'role': 'assistant', 'content': canonical['content'], 'provider_specific_fields': {'viventium': {
            'delivery_disposition': canonical['delivery_disposition']}}, **({'tool_calls': [{
                'id': 'call-transfer', 'type': 'function', 'function': {
                    'name': canonical['tool_name'], 'arguments': '{}'}}]} if kind == 'tool_call' else {})},
        'finish_reason': 'tool_calls' if kind == 'tool_call' else 'stop'}], 'usage': {}, 'glasshive': {'usage_source': 'native'}}
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    async def collect():
        return [row async for row in provider._stream_chunks(record, payload, Connected())]
    rows = asyncio.run(collect())
    data = [json.loads(row[6:]) for row in rows if row.startswith('data: {')]
    assert not any('error' in item for item in data)
    deltas = [item['choices'][0]['delta'] for item in data if item.get('choices') and 'delta' in item['choices'][0]]
    content = [item for item in deltas if item.get('content')]
    assert ''.join(item['content'] for item in content) == canonical['content']
    audio = [item['provider_specific_fields']['viventium']['delivery_disposition']['audio'] for item in content]
    assert audio == [first_voice, voice]
    assert sum(bool(item.get('provider_specific_fields', {}).get('viventium', {}).get('public_part_complete'))
               for item in deltas) == len(expected_parts)
    transfers = [item['tool_calls'] for item in deltas if item.get('tool_calls')]
    assert bool(transfers) is (kind == 'tool_call')
    if transfers:
        assert transfers[0][0]['function']['name'] == canonical['tool_name']
    assert len(receipts) == 1  # Text-only public output still has an authoring receipt.
    assert all(receipt[1] == 'authored-response' for receipt in receipts)
    assert rows[-1] == 'data: [DONE]\n\n'


@pytest.mark.parametrize('voice', ['eligible', 'skip'])
def test_native_silent_predecessor_stream_keeps_later_controls(monkeypatch, tmp_path, voice):
    test_native_generation_terminal_stream_controls_match_existing_tool_parts(
        monkeypatch, tmp_path, 'generation', 'assistant_response', voice, first_voice='skip')


@pytest.mark.parametrize('voice', ['eligible', 'skip'])
def test_native_projection_silent_predecessor_keeps_exact_later_delivery(voice):
    from workers_projects_runtime.agent_builder_control import NativeConversationParts, parse_conversation_output
    from workers_projects_runtime.conversation_provider import _native_spoken_content, _native_authored_preview
    delivery = {'version': 1, 'audio_eligible': True}
    parts = NativeConversationParts(None, delivery)
    events = [{'type': 'grok.session.started', 'session_id': 'native-exact'}]
    for stream, audio, text in [(100, 'skip', 'Silent body.'), (200, voice, 'Later contribution.')]:
        for update in [{'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps({
            'type': 'assistant_response', 'tool_name': None, 'voice': audio, 'content': text})}},
            {'sessionUpdate': 'response_completed'}]:
            meta = {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}
            parts.update(update, meta)
            events.append({'type': 'grok.session.update', 'session_id': 'native-exact', 'meta': meta, 'update': update})
    canonical = parse_conversation_output(parts.finish(), None, delivery)
    expected_text = 'Silent body.\n\nLater contribution.'
    assert canonical['content'] == expected_text
    assert canonical['delivery_disposition']['audio'] == 'skip'  # Mixed canonical audio is conservative; part ranges retain exact intent.
    raw = '\n'.join(map(json.dumps, events))
    spoken = _native_spoken_content('grok-build', raw, None, delivery)
    assert spoken['text'] == expected_text
    assert spoken['delivery_disposition']['audio'] == voice
    assert [part['delivery_disposition']['audio'] for part in spoken['presentation_parts']] == ['skip', voice]
    assert spoken['completed_parts'] == ([{'part_index': 2, 'text_end': len(expected_text)}] if voice == 'eligible' else [])
    assert _native_authored_preview('grok-build', raw, None, delivery)['text'] == canonical['content']


@pytest.mark.parametrize('voice', ['eligible', 'skip'])
def test_native_projection_preserves_terminal_graph_when_optional_generation_arrives(voice):
    from workers_projects_runtime.agent_builder_control import NativeConversationParts, parse_conversation_output
    from workers_projects_runtime.conversation_provider import _native_spoken_content, _native_authored_preview
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    delivery = {'version': 1, 'audio_eligible': True}
    parts = NativeConversationParts(graph, delivery)
    events = [{'type': 'grok.session.started', 'session_id': 'native-exact'}]
    for stream, body in [(75, {'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': 'Accepted narration.'}),
                         (100, {'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist', 'voice': voice, 'content': 'Consulting.'}),
                         (200, {'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': 'Late addition.'})]:
        for update in [{'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps(body)}},
                       {'sessionUpdate': 'response_completed'}]:
            meta = {'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}
            parts.update(update, meta)
            events.append({'type': 'grok.session.update', 'session_id': 'native-exact', 'meta': meta, 'update': update})
    canonical = parse_conversation_output(parts.finish(), graph, delivery)
    assert canonical['type'] == 'tool_call' and canonical['tool_name'] == 'lc_transfer_to_specialist'
    assert canonical['content'] == 'Accepted narration.\n\nConsulting.'
    assert canonical['delivery_disposition']['audio'] == voice
    raw = '\n'.join(map(json.dumps, events));spoken = _native_spoken_content('grok-build', raw, graph, delivery)
    assert spoken['text'] == ('Accepted narration.\n\nConsulting.' if voice == 'eligible' else 'Accepted narration.')
    assert spoken['completed_parts'] == ([{'part_index': 1, 'text_end': len('Accepted narration.')}]
        + ([{'part_index': 2, 'text_end': len(spoken['text'])}] if voice == 'eligible' else []))
    assert _native_authored_preview('grok-build', raw, graph, delivery)['text'] == 'Accepted narration.'


def test_native_spoken_content_retains_public_parts_and_fences_session_controls():
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    delivery = {"version": 1, "audio_eligible": True}
    def update(kind, text='', session='exact'):
        return {"type": "grok.session.update", "session_id": session,
                "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}}
    first = '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"I will check it."}'
    second = '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"The result is'
    events = [{"type": "grok.session.started", "session_id": "exact"},
              update('agent_thought_chunk', 'Private thought'),
              update('agent_message_chunk', first, 'foreign'),
              update('agent_message_chunk', first), update('tool_call'),
              update('agent_message_chunk', second)]
    result = _native_spoken_content('grok-build', '\n'.join(map(json.dumps, events)), None, delivery)
    assert result['text'] == 'I will check it.\n\nThe result is'
    assert result['sequence'] == 6
    assert result['delivery_disposition']['valid'] is True
    assert _native_spoken_content('grok-build', '\n'.join(map(json.dumps, events[1:])), None, delivery) is None
    assert _native_spoken_content('claude-code', '\n'.join(map(json.dumps, events)), None, delivery) is None


@pytest.mark.parametrize("handoff", [False, True])
def test_native_voice_stream_sends_public_prefix_before_terminal_without_replay(monkeypatch, tmp_path, handoff):
    from types import SimpleNamespace
    provider = object.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))
    payload.metadata.audio_eligible = True
    if handoff:
        payload.tools = [{'type': 'function', 'function': {'name': 'lc_transfer_to_specialist',
            'description': 'Transfer in the current graph.', 'parameters': {
                'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}}]
    payload.metadata.actor_kind = 'external_user'
    payload.metadata.origin = 'interactive'
    record = {"request_id": "request-a", "run_id": "run-a", "session_id": "session-a", "owner_id": "owner-a",
              "message_id": "message-a", "native_invocation_id": "invocation-a", "state": "running",
              "created_at": datetime.now(UTC).isoformat()}
    turn = {"value": 0}
    def get_record(_):
        turn['value'] += 1
        return {**record, 'state': 'completed' if turn['value'] == 3 else 'running'}
    def stdout(_worker, _run):
        text = 'The useful response' + ('.' if turn['value'] > 1 else '')
        controls = {'type': 'tool_call' if handoff else 'assistant_response',
                    'tool_name': 'lc_transfer_to_specialist' if handoff else None, 'voice': 'eligible'}
        output = json.dumps(controls)[:-1] + ',"content":"' + text
        events = [{"type": "grok.session.started", "session_id": "native-a"},
                  {"type": "grok.session.update", "session_id": "native-a", "update": {
                      "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": output}}}]
        return 'grok-build', '\n'.join(map(json.dumps, events))
    provider.store = SimpleNamespace(get_provider_request=get_record, get_run=lambda _: {'run_id': 'run-a'},
        get_provider_session_by_id=lambda _: {'owner_id': 'owner-a'},
        get_worker=lambda _: {'owner_id': 'owner-a'}, list_provider_activity=lambda _: [])
    public_receipts = []
    provider.store.add_provider_activity_once = lambda *args: public_receipts.append(args)
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=stdout))
    provider._sync = lambda record: record
    def unexpected_terminal_snapshot(*_):
        raise AssertionError('Structured Voice must not collect an unused terminal text snapshot')
    provider._native_output_snapshot = unexpected_terminal_snapshot
    disposition = {'version': 1, 'audio': 'eligible', 'required': True, 'valid': True, 'source': 'model'}
    provider.response_payload = lambda *_: {'model': payload.model, 'choices': [{'message': {
        'role': 'assistant', 'content': 'The useful response.', 'provider_specific_fields': {
            'viventium': {'delivery_disposition': disposition}},
        **({'tool_calls': [{'id': 'call-transfer', 'type': 'function', 'function': {
            'name': 'lc_transfer_to_specialist', 'arguments': '{}'}}]} if handoff else {})},
        'finish_reason': 'tool_calls' if handoff else 'stop'}],
        'usage': {}, 'glasshive': {'usage_source': 'native'}}
    def unexpected_completion_usage(*_):
        raise AssertionError('Completed streams must use canonical response usage')
    provider._completion_usage = unexpected_completion_usage
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    async def collect():
        emitted = []
        async for row in provider._stream_chunks(record, payload, Connected()):
            emitted.append((turn['value'], row))
        return emitted
    raw = asyncio.run(collect())
    deltas = [(stage, json.loads(row[6:])['choices'][0]['delta']) for stage, row in raw if row.startswith('data: {')]
    content = [(stage, delta) for stage, delta in deltas if delta.get('content')]
    assert content[0][0] < 3
    assert len(public_receipts) == 1 and public_receipts[0][1] == 'authored-response'
    assert content[0][1]['provider_specific_fields']['viventium']['delivery_disposition'] == disposition
    assert ''.join(delta['content'] for _, delta in content) == 'The useful response.'
    assert raw[-1][1] == 'data: [DONE]\n\n'
    transfers = [delta['tool_calls'] for _, delta in deltas if delta.get('tool_calls')]
    assert bool(transfers) is handoff
    if handoff:
        assert transfers[0][0]['function']['name'] == 'lc_transfer_to_specialist'
    payload.metadata.origin = 'scheduler'
    assert provider._native_spoken_snapshot(record, {'run_id': 'run-a'}, payload, None, {'version': 1, 'audio_eligible': True}) is None
    payload.metadata.origin = 'interactive'
    assert provider._native_spoken_snapshot(record, {'run_id': 'wrong'}, payload, None, {'version': 1, 'audio_eligible': True}) is None


def _spoken_family_fixture():
    from types import SimpleNamespace
    scope = {"tenant_id": "local", "owner_id": "owner-a", "session_id": "session-a",
             "message_id": "message-a", "stream_id": "stream-a"}
    identity = {"logical_turn_id": "turn-a", "logical_turn_revision": 1,
                "context_epoch": 1, "main_context_snapshot_sha256": "a" * 64,
                "advancement_key": "turn-a:1", "response_message_key": "msg:message-a",
                "main_context_protocol": "main_context_v1", "main_context_owner": "core"}
    decision = {**identity, "admission_state": "reserved", "admission_sequence": 3,
                "family_continuation_v1": True, "family_continuation_of_request_id": "parent-a"}
    record = {**scope, "request_id": "request-a", "run_id": "run-a", "state": "running",
              "native_invocation_id": "", "native_body_sha256": "",
              "created_at": datetime.now(UTC).isoformat(),
              "replay_decision_json": json.dumps(decision)}
    parent = {**scope, "request_id": "parent-a", "state": "completed", "response_json": "{}",
              "native_invocation_id": "invocation-a", "native_body_sha256": "b" * 64,
              "replay_decision_json": json.dumps({**identity, "admission_state": "accepted"})}
    session = {"owner_id": "owner-a", "context_manifest_json": json.dumps({
        "admission_sequence": 3, "last_native_admission_request_id": "request-a"})}
    payload = ChatCompletionRequest(model="grok-build:synthetic", messages=[{
        "role": "user", "content": "Synthetic request."}], stream=True, metadata={
        "audio_eligible": True, "actor_kind": "external_user", "origin": "interactive"})
    provider = object.__new__(ConversationProvider)
    state = {"record": record, "parent": parent, "session": session, "run": {"run_id": "run-a"}}
    def get_request(request_id):
        return next((value for key in ("record", "parent", "ancestor")
                     if (value := state.get(key)) and value["request_id"] == request_id), None)
    provider.store = SimpleNamespace(get_provider_request=get_request,
        get_provider_session_by_id=lambda _: session, get_worker=lambda _: {"owner_id": "owner-a"},
        get_run=lambda _: state["run"], list_provider_activity=lambda _: [],
        add_provider_activity_once=lambda *args: state.setdefault("public_receipts", []).append(args))
    prefix = '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Useful public words. '
    events = [{"type": "grok.session.started", "session_id": "native-a"},
              {"type": "grok.session.update", "session_id": "native-a", "update": {
                  "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": prefix}}}]
    provider.service = SimpleNamespace(runtime=SimpleNamespace(
        provider_activity_log=lambda *_: ("grok-build", "\n".join(map(json.dumps, events)))))
    return state, provider, payload


def _project_spoken_family(state, provider, payload):
    return provider._native_spoken_snapshot(state["record"], state["run"], payload, None,
                                           {"version": 1, "audio_eligible": True})


@pytest.mark.parametrize("prior,answer", [
    (None, "  An indented answer."),
    ("", "Useful answer."),
    ("", "\n  Useful answer.\n"),
    (" \n\t", "Useful answer."),
    (None, "Code example:  \n    return value\n"),
    (None, "\r\n\tIndented  \r\nNext line.\r"),
    ("Quiet context.", "Spoken answer."),
])
def test_native_stream_preserves_canonical_leading_bytes(monkeypatch, prior, answer):
    """An empty quiet part must not make a valid live answer fail at completion."""
    from types import SimpleNamespace
    from workers_projects_runtime.agent_builder_control import NativeConversationParts

    state, provider, payload = _spoken_family_fixture()
    parts = NativeConversationParts(None, {"version": 1, "audio_eligible": True})
    events = [{"type": "grok.session.started", "session_id": "native-a"}]

    def add(update):
        parts.update(update)
        events.append({"type": "grok.session.update", "session_id": "native-a", "update": update})

    for text, voice in ([] if prior is None else [(prior, "skip")]) + [(answer, "eligible")]:
        if parts.current:
            add({"sessionUpdate": "tool_call"})
        body = json.dumps({"type": "assistant_response", "tool_name": None,
                           "voice": voice, "content": text})
        add({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": body}})
    output = json.loads(parts.finish())["content"]
    canonical = {"model": payload.model, "choices": [{"message": {"role": "assistant", "content": output},
        "finish_reason": "stop"}], "usage": {}, "glasshive": {"usage_source": "native"}}
    provider.service = SimpleNamespace(runtime=SimpleNamespace(
        provider_activity_log=lambda *_: ("grok-build", "\n".join(map(json.dumps, events)))))
    provider._graph_transfer_control = lambda *_: None
    state["stage"] = 0

    def sync(record):
        state["stage"] += 1
        if state["stage"] == 2:
            record.update(state="completed", response_json=json.dumps(canonical))
        return record

    provider._sync = sync
    provider.response_payload = lambda *_: canonical

    class Connected:
        async def is_disconnected(self): return False

    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def collect():
        return [(state["stage"], row) async for row in provider._stream_chunks(state["record"], payload, Connected())]

    frames = asyncio.run(collect())
    chunks = [(stage, json.loads(row[6:])) for stage, row in frames if row.startswith("data: {")]
    assert not [chunk for _, chunk in chunks if chunk.get("error")]
    content = [(stage, chunk["choices"][0]["delta"]) for stage, chunk in chunks if chunk.get("choices")]
    assert next(stage for stage, delta in content if delta.get("content")) == 1
    assert "".join(delta.get("content", "") for _, delta in content) == output
    assert frames[-1][1] == "data: [DONE]\n\n"


def test_native_spoken_family_reuses_current_reservation_without_invocation_preview():
    state, provider, payload = _spoken_family_fixture()
    assert _project_spoken_family(state, provider, payload)["text"] == "Useful public words."
    assert provider._native_preview_snapshot(state["record"], state["run"], payload, None,
                                            {"version": 1, "audio_eligible": True}) is None
    assert state["record"]["native_invocation_id"] == ""


@pytest.mark.parametrize("field", ["tenant_id", "owner_id", "session_id", "message_id", "stream_id"])
def test_native_spoken_family_withholds_foreign_parent_record(field):
    state, provider, payload = _spoken_family_fixture()
    state["parent"][field] = "foreign"
    assert _project_spoken_family(state, provider, payload) is None


@pytest.mark.parametrize("field", ["logical_turn_id", "logical_turn_revision", "context_epoch",
    "main_context_snapshot_sha256", "advancement_key", "response_message_key"])
def test_native_spoken_family_withholds_foreign_parent_decision(field):
    state, provider, payload = _spoken_family_fixture()
    decision = json.loads(state["parent"]["replay_decision_json"])
    decision[field] = "foreign"
    state["parent"]["replay_decision_json"] = json.dumps(decision)
    assert _project_spoken_family(state, provider, payload) is None


@pytest.mark.parametrize("case", ["missing_parent", "unaccepted_parent", "unfinished_parent",
    "parent_no_body", "parent_no_invocation", "rejected_current", "not_core", "missing_family",
    "cycle", "newer_admission", "cancelled", "wrong_run", "scheduled", "internal", "restore_hold"])
def test_native_spoken_family_preserves_authority_and_cancel_fences(case):
    state, provider, payload = _spoken_family_fixture()
    if case == "missing_parent":
        state["parent"] = None
    elif case == "unfinished_parent":
        state["parent"]["state"] = "running"
    elif case in {"parent_no_body", "parent_no_invocation"}:
        state["parent"]["native_body_sha256" if case == "parent_no_body" else "native_invocation_id"] = ""
    elif case == "unaccepted_parent":
        decision = json.loads(state["parent"]["replay_decision_json"])
        decision["admission_state"] = "reserved"
        state["parent"]["replay_decision_json"] = json.dumps(decision)
    elif case in {"rejected_current", "not_core", "missing_family", "cycle"}:
        decision = json.loads(state["record"]["replay_decision_json"])
        key, value = {"rejected_current": ("admission_state", "rejected"),
            "not_core": ("main_context_owner", "provider_legacy"),
            "missing_family": ("family_continuation_v1", False),
            "cycle": ("family_continuation_of_request_id", "request-a")}[case]
        decision[key] = value
        state["record"]["replay_decision_json"] = json.dumps(decision)
    elif case == "newer_admission":
        state["session"]["context_manifest_json"] = json.dumps({
            "admission_sequence": 4, "last_native_admission_request_id": "newer-request"})
    elif case == "cancelled":
        state["record"]["state"] = "cancelled"
    elif case == "wrong_run":
        state["run"]["run_id"] = "foreign"
    elif case == "scheduled":
        payload.metadata.origin = "scheduled"
    elif case == "internal":
        payload.metadata.actor_kind = "internal_ai"
    elif case == "restore_hold":
        state["record"]["restore_hold"] = 1
    assert _project_spoken_family(state, provider, payload) is None


def test_native_spoken_family_rechecks_admission_after_public_projection():
    state, provider, payload = _spoken_family_fixture()
    original = provider._native_spoken_content_snapshot
    def crossed(*args):
        result = original(*args)
        state["session"]["context_manifest_json"] = json.dumps({
            "admission_sequence": 4, "last_native_admission_request_id": "newer-request"})
        return result
    provider._native_spoken_content_snapshot = crossed
    assert _project_spoken_family(state, provider, payload) is None


def test_native_spoken_family_follows_completed_accepted_chain_without_new_identity():
    state, provider, payload = _spoken_family_fixture()
    state["ancestor"] = {**state["parent"]}
    state["parent"] = {**state["parent"], "request_id": "intermediate-a",
                       "native_invocation_id": "", "native_body_sha256": ""}
    decision = json.loads(state["parent"]["replay_decision_json"])
    decision.update(family_continuation_v1=True, family_continuation_of_request_id="parent-a")
    state["parent"]["replay_decision_json"] = json.dumps(decision)
    current = json.loads(state["record"]["replay_decision_json"])
    current["family_continuation_of_request_id"] = "intermediate-a"
    state["record"]["replay_decision_json"] = json.dumps(current)
    assert _project_spoken_family(state, provider, payload)


def test_native_spoken_family_streams_before_terminal_without_replaying_final(monkeypatch):
    state, provider, payload = _spoken_family_fixture()
    provider._graph_transfer_control = lambda *_: None
    state["stage"] = 0
    final = {"model": payload.model, "choices": [{"message": {
        "role": "assistant", "content": "Useful public words. Complete answer."}, "finish_reason": "stop"}],
        "usage": {}, "glasshive": {"usage_source": "native"}}
    def sync(record):
        state["stage"] += 1
        if state["stage"] == 2:
            record["state"] = "completed"
            record["response_json"] = json.dumps(final)
        return record
    provider._sync = sync
    provider.response_payload = lambda *_: final
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    async def collect():
        return [(state["stage"], row) async for row in provider._stream_chunks(
            state["record"], payload, Connected())]
    rows = asyncio.run(collect())
    content = [(stage, json.loads(row[6:])["choices"][0]["delta"].get("content", ""))
               for stage, row in rows if row.startswith("data: {") and json.loads(row[6:]).get("choices")]
    assert next(stage for stage, text in content if text) == 1
    assert "".join(text for _, text in content) == final["choices"][0]["message"]["content"]
    assert len(state["public_receipts"]) == 1
    assert rows[-1][1] == "data: [DONE]\n\n"


def test_typed_voice_redactor_releases_first_safe_words_without_fixed_tail():
    redactor = StreamingRedactor(overlap=0)
    assert redactor.feed('The useful ') == 'The useful '
    assert redactor.feed('response.') == ''
    assert redactor.flush() == 'response.'


@pytest.mark.parametrize("target", ["file.txt", "./file.txt", "../file.txt", "/tmp/file.txt",
    "file:///tmp/file.txt", "artifact_sha256:" + "a" * 64, "<file%20name.txt>"])
def test_native_selected_file_link_waits_for_canonical_projection(target):
    from workers_projects_runtime.conversation_provider import _mutable_native_file_link_start
    prefix = "Useful words.\n\n"
    assert _mutable_native_file_link_start(prefix + "[File](" + target + ")", incomplete=False) == len(prefix)


@pytest.mark.parametrize("text", [
    "Useful [source](https://example.invalid/doc).",
    "Useful [source](https://example.invalid/doc#part).",
    "Useful [source](//example.invalid/doc).", "Useful [source](part#anchor).",
    "Useful [source](mailto:qa@example.invalid).", "Useful `literal [File](file.txt)` code.",
    "Useful\n```text\n[File](file.txt)\n```", "Useful\n    [File](file.txt)",
    "Useful\n> [File](file.txt)", "Useful [literal].", "Useful \\[literal\\].",
])
def test_native_file_projection_preserves_public_url_code_and_literal_controls(text):
    from workers_projects_runtime.conversation_provider import _mutable_native_file_link_start
    assert _mutable_native_file_link_start(text, incomplete=False) is None


@pytest.mark.parametrize("suffix", ["[Fi", "[File]", "[File](", "[File](fi", "[File](https://example.invalid"])
def test_native_file_projection_keeps_pending_markdown_out_of_accepted_prefix(suffix):
    from workers_projects_runtime.conversation_provider import _mutable_native_file_link_start
    prefix = "Useful words. "
    assert _mutable_native_file_link_start(prefix + suffix, incomplete=True) == len(prefix)


def test_native_file_projection_stays_monotonic_at_every_public_chunk_boundary():
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    output = json.dumps({"type": "assistant_response", "tool_name": None, "voice": "eligible",
                         "content": "Useful words.\n\n[File](file.txt)"})
    events = [{"type": "grok.session.started", "session_id": "native-a"}]
    last = ""
    for character in output:
        events.append({"type": "grok.session.update", "session_id": "native-a", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": character}}})
        snapshot = _native_spoken_content("grok-build", "\n".join(map(json.dumps, events)), None,
                                          {"version": 1, "audio_eligible": True})
        if snapshot:
            assert snapshot["text"].startswith(last)
            last = snapshot["text"]
    assert last == "Useful words."


@pytest.mark.parametrize("file_state", ["selected", "missing", "rejected", "changed_prefix"])
def test_native_file_projection_streams_words_before_exact_terminal_content_and_carrier(monkeypatch, file_state):
    from types import SimpleNamespace
    state, provider, payload = _spoken_family_fixture()
    raw_content = "Useful words.\n\n[File](file.txt)"
    output = json.dumps({"type": "assistant_response", "tool_name": None,
                         "voice": "eligible", "content": raw_content})
    events = [{"type": "grok.session.started", "session_id": "native-a"},
              {"type": "grok.session.update", "session_id": "native-a", "update": {
                  "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": output}}}]
    provider.service = SimpleNamespace(runtime=SimpleNamespace(
        provider_activity_log=lambda *_: ("grok-build", "\n".join(map(json.dumps, events)))))
    provider._graph_transfer_control = lambda *_: None
    file_bytes = b"Synthetic file.\n"
    selected = {"version": 1, "owner_id": "owner-a", "conversation_id": "conversation-a",
        "message_id": "message-a", "stream_id": "stream-a", "request_id": "request-a", "run_id": "run-a",
        "attempt_id": "attempt-a", "logical_turn_id": "turn-a", "logical_turn_revision": 1,
        "invocation_id": "", "files": [{"filename": "file.txt", "mime_type": "text/plain",
            "bytes": len(file_bytes), "sha256": hashlib.sha256(file_bytes).hexdigest(),
            "download_url": "https://example.invalid/files/selected"}]}
    content = ("Useful words.\n\n[File](https://example.invalid/files/selected)"
               if file_state == "selected" else raw_content)
    canonical = {"model": payload.model, "choices": [{"message": {"role": "assistant", "content": content},
                  "finish_reason": "stop"}], "usage": {}, "glasshive": {"usage_source": "native"}}
    if file_state in {"selected", "changed_prefix"}:
        canonical["glasshive"]["output_files"] = selected
        if file_state == "changed_prefix":
            canonical["choices"][0]["message"]["content"] = "Changed words.\n\n[File](https://example.invalid/files/selected)"
    elif file_state == "rejected":
        canonical["glasshive"]["output_file_rejections"] = [{"name": "file.txt", "code": "unreadable"}]
    state["stage"] = 0
    def sync(record):
        state["stage"] += 1
        if state["stage"] == 2:
            record.update(state="completed", response_json=json.dumps(canonical))
        return record
    provider._sync = sync
    provider.response_payload = lambda *_: canonical
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    async def collect():
        return [(state["stage"], row) async for row in provider._stream_chunks(
            state["record"], payload, Connected())]
    rows = asyncio.run(collect())
    chunks = [(stage, json.loads(row[6:])) for stage, row in rows if row.startswith("data: {")]
    failures = [chunk["error"] for _, chunk in chunks if chunk.get("error")]
    assert ([failure["code"] for failure in failures] == ["provider_response_failed"]
            if file_state == "changed_prefix" else not failures)
    deltas = [(stage, chunk["choices"][0]["delta"]) for stage, chunk in chunks if chunk.get("choices")]
    assert next((stage, delta["content"]) for stage, delta in deltas if delta.get("content")) == (1, "Useful words.")
    assert "".join(delta.get("content", "") for _, delta in deltas) == (
        "Useful words." if file_state == "changed_prefix" else content)
    carriers = [(stage, delta["provider_specific_fields"]["viventium"]["output_files"])
                for stage, delta in deltas
                if delta.get("provider_specific_fields", {}).get("viventium", {}).get("output_files")]
    assert carriers == ([(2, selected)] if file_state == "selected" else [])
    assert len(state["public_receipts"]) == 1
    assert rows[-1][1] == "data: [DONE]\n\n"


@pytest.mark.parametrize('state', ['failed', 'cancelled'])
def test_structured_voice_terminal_failure_preserves_native_usage(state, tmp_path):
    from types import SimpleNamespace
    provider = object.__new__(ConversationProvider)
    provider.service = SimpleNamespace()
    payload_dict = _payload(tmp_path, stream=True)
    payload_dict['stream_options'] = {'include_usage': True}
    payload = ChatCompletionRequest.model_validate(payload_dict)
    payload.metadata.audio_eligible = True
    record = {'request_id': 'request-a', 'run_id': 'run-a', 'state': state,
              'created_at': datetime.now(UTC).isoformat()}
    run = {'run_id': 'run-a', 'failure_user_message': 'Synthetic native stop.'}
    provider.store = SimpleNamespace(get_provider_request=lambda _: record,
        get_run=lambda _: run, list_provider_activity=lambda _: [])
    provider._sync = lambda row: row
    provider._native_effective_model = lambda *_: payload.model
    provider._native_spoken_snapshot = lambda *_: None
    provider._native_preview_snapshot = lambda *_: None
    def unexpected_terminal_snapshot(*_):
        raise AssertionError('Structured Voice does not collect unused terminal text')
    provider._native_output_snapshot = unexpected_terminal_snapshot
    usage_calls = []
    usage = {'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16}
    def completion_usage(*args):
        usage_calls.append(args)
        return usage, 'native'
    provider._completion_usage = completion_usage
    class Connected:
        async def is_disconnected(self): return False
    async def collect():
        return [row async for row in provider._stream_chunks(record, payload, Connected())]
    rows = asyncio.run(collect())
    events = [json.loads(row[6:]) for row in rows if row.startswith('data: {')]
    assert len(usage_calls) == 1
    assert usage_calls[0] == (record, run, payload, '')
    assert events[-1]['usage'] == usage
    assert events[-1]['glasshive']['usage_source'] == 'native'
    assert any(event.get('error', {}).get('message') == 'Synthetic native stop.' for event in events)
    assert rows[-1] == 'data: [DONE]\n\n'


@pytest.mark.parametrize('secret', [
    'Bearer synthetic-credential', 'api_key=synthetic-credential',
    ('sk' + '-synthetic-credential'), 'ghp_synthetic-credential',
    ('xo' + 'xb-synthetic-credential'), ('/U' + 'sers/synthetic/private.txt'),
    ('--' + '---BEGIN RSA PRIVATE KEY-----\nSYNTHETICBODY\n-----END RSA PRIVATE KEY-----'),
])
def test_typed_voice_redactor_preserves_split_secret_guards_without_fixed_tail(secret):
    for split in range(len(secret) + 1):
        redactor = StreamingRedactor(overlap=0)
        output = redactor.feed('Safe words. ' + secret[:split])
        output += redactor.feed(secret[split:] + '\nSafe tail.')
        output += redactor.flush()
        assert 'synthetic-credential' not in output
        assert '/Users/synthetic' not in output
        assert 'SYNTHETICBODY' not in output
        assert 'Safe words.' in output and 'Safe tail.' in output


@pytest.mark.parametrize('public_text', [False, True])
def test_native_recovery_cannot_replay_accepted_public_parts(public_text):
    from types import SimpleNamespace
    provider = object.__new__(ConversationProvider)
    provider.store = SimpleNamespace(get_provider_session_by_id=lambda _: {'model_id': 'grok-build:grok-4.7'})
    provider._model = lambda *_args, **_kwargs: True
    provider._native_output_snapshot = lambda *_: ''
    provider._native_spoken_content_snapshot = lambda *_: {'text': 'Accepted public text.'} if public_text else None
    record = {'fallback_model_id': 'claude-code:opus', 'fallback_instruction': 'Accepted request.',
              'admitted_instruction': 'Accepted request.', 'session_id': 'session'}
    run = {'state': 'failed', 'failure_class': 'provider_rate_limited', 'failure_retryable': 1,
           'failure_structured': 1, 'retry_attempts': 0}
    assert provider._serial_fallback_eligible(record, run, set()) is (not public_text)
    context_run = {**run, 'failure_class': 'provider_context_limit_exceeded'}
    assert provider._context_recovery_eligible(record, context_run, set()) is (not public_text)
    assert provider._serial_fallback_eligible(record, run, {'authored-response'}) is False
    assert provider._context_recovery_eligible(record, context_run, {'authored-response'}) is False


@pytest.mark.parametrize('text', [
    'Here is the result. api_key=synthetic-credential\nSafe tail.',
    ('Open [synthetic state file](/U' + 'sers/synthetic/state.txt) to see the result.'),
    'Use [public reference](https://example.com/reference) for the result.',
])
def test_typed_voice_stream_and_native_canonical_redaction_agree_across_json_escapes(text):
    from workers_projects_runtime.conversation_provider import _redact_provider_output
    envelope = json.dumps({'type': 'assistant_response', 'tool_name': None,
                           'voice': 'eligible', 'content': text})
    canonical = json.loads(_redact_provider_output(envelope))['content']
    for split in range(len(text) + 1):
        redactor = StreamingRedactor(overlap=0)
        streamed = redactor.feed(text[:split]) + redactor.feed(text[split:]) + redactor.flush()
        assert streamed == canonical


@pytest.mark.parametrize('text,changed', [
    ('\n  First useful words.  \n', False),
    ('First useful words.\x1b[31mPRIVATE\nSafe tail.', False),
    ('First useful words. \ue202turn1search0\ue201 More public text.', False),
    ('First useful words.', True),
])
def test_native_voice_stream_matches_terminal_public_sanitation_or_fails_without_replay(tmp_path, monkeypatch, text, changed):
    from types import SimpleNamespace
    from workers_projects_runtime.conversation_provider import _redact_provider_output, _sanitize_provider_output
    provider = object.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))
    payload.metadata.audio_eligible = True
    payload.metadata.actor_kind = 'external_user'
    payload.metadata.origin = 'interactive'
    record = {'request_id':'request-a','run_id':'run-a','session_id':'session-a','owner_id':'owner-a',
              'message_id':'message-a','native_invocation_id':'invocation-a','state':'running',
              'created_at':datetime.now(UTC).isoformat()}
    turn = {'value':0}
    def get_record(_):
        turn['value'] += 1
        return {**record,'state':'completed' if turn['value'] == 3 else 'running'}
    def stdout(_worker, _run):
        prefix = text[:text.index('words')]
        raw = json.dumps(text if turn['value'] > 1 else prefix, ensure_ascii=True)
        envelope = '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":' + raw
        envelope = envelope + '}' if turn['value'] > 1 else envelope[:-1]
        return 'grok-build','\n'.join(map(json.dumps,[
            {'type':'grok.session.started','session_id':'native-a'},
            {'type':'grok.session.update','session_id':'native-a','update':{
                'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':envelope}}}]))
    provider.store = SimpleNamespace(get_provider_request=get_record,get_run=lambda _: {'run_id':'run-a'},
        get_provider_session_by_id=lambda _: {'owner_id':'owner-a'},get_worker=lambda _: {'owner_id':'owner-a'},
        list_provider_activity=lambda _: [],add_provider_activity_once=lambda *_: None)
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=stdout))
    provider._sync = lambda record: record
    provider._native_output_snapshot = lambda *_: ''
    sources = [{'ref_id':'turn1search0','title':'Public source','url':'https://example.com/reference'}]
    raw_final = json.dumps({'type':'assistant_response','tool_name':None,'voice':'eligible',
                            'content':'Changed terminal response.' if changed else text})
    canonical = json.loads(_redact_provider_output(raw_final, sources))['content']
    provider.response_payload = lambda *_: {'model':payload.model,'choices':[{'message':{'content':canonical},'finish_reason':'stop'}],
                                          'usage':{},'glasshive':{'usage_source':'native'}}
    provider._completion_usage = lambda *_: ({},'native')
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio,'sleep',no_sleep)
    async def collect():
        result = []
        async for row in provider._stream_chunks(record,payload,Connected()): result.append((turn['value'],row))
        return result
    rows = asyncio.run(collect())
    events = [(stage,json.loads(row[6:])) for stage,row in rows if row.startswith('data: {')]
    pieces = [(stage,event['choices'][0]['delta'].get('content','')) for stage,event in events if event.get('choices')]
    assert any(stage < 3 and value for stage,value in pieces)
    spoken = ''.join(value for _,value in pieces)
    assert '\ue202' not in spoken and 'PRIVATE' not in spoken
    if changed:
        assert any(event.get('error',{}).get('code') == 'provider_response_failed' for _,event in events)
        assert 'Changed terminal response.' not in spoken and 'harness corrected' not in spoken
    else:
        assert spoken == canonical
    assert rows[-1][1] == 'data: [DONE]\n\n'


@pytest.mark.parametrize("private_suffix", [False, True])
def test_plain_native_stream_uses_canonical_public_projection_without_early_references(tmp_path, monkeypatch, private_suffix):
    from types import SimpleNamespace

    provider = object.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate(_payload(tmp_path, stream=True))
    payload.metadata.audio_eligible = False
    prefix = "  Useful ordinary words for the user. " * 8
    raw = prefix + ("\x1b[31mPRIVATE\nSafe tail. \ue202turn1search0\ue201 "
                    "[result](artifact_sha256:" + "a" * 64 + ")" if private_suffix else "Finished.")
    stage = {"value": 0}
    record = {"request_id": "request-a", "run_id": "run-a", "session_id": "session-a",
              "state": "running", "created_at": datetime.now(UTC).isoformat()}
    sources = [{"ref_id": "turn1search0", "title": "Public source", "url": "https://example.com/source"}]
    provider._native_output_snapshot = lambda *_: raw
    provider._native_citation_sources_snapshot = lambda *_: sources
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda *_: None),
        render_provider_native_images=lambda _request, _run, value, **_kwargs:
            value.replace("artifact_sha256:" + "a" * 64, "https://example.com/result"))
    output = provider._conversation_output(record, {"run_id": "run-a"})
    canonical = {"model": payload.model, "choices": [{"message": {"content": output}, "finish_reason": "stop"}],
                 "usage": {}, "glasshive": {"usage_source": "native"}}

    def get_record(_):
        stage["value"] += 1
        return {**record, **({"state": "completed", "response_json": json.dumps(canonical)}
                            if stage["value"] == 2 else {})}

    provider.store = SimpleNamespace(get_provider_request=get_record, get_run=lambda _: {"run_id": "run-a"},
                                     list_provider_activity=lambda _: [])
    provider._sync = lambda value: value
    provider._graph_transfer_control = lambda *_: None
    provider._native_effective_model = lambda *_: payload.model
    provider._native_spoken_snapshot = lambda *_: None
    provider._native_preview_snapshot = lambda *_: None
    provider.response_payload = lambda *_: canonical

    class Connected:
        async def is_disconnected(self): return False

    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def collect():
        return [(stage["value"], row) async for row in provider._stream_chunks(record, payload, Connected())]

    frames = asyncio.run(collect())
    chunks = [(when, json.loads(row[6:])) for when, row in frames if row.startswith("data: {")]
    content = [(when, chunk["choices"][0]["delta"].get("content", ""))
               for when, chunk in chunks if chunk.get("choices")]
    assert any(when == 1 and text for when, text in content)
    assert "".join(text for _, text in content) == output
    assert not any(chunk.get("error") for _, chunk in chunks)
    assert not any("PRIVATE" in text or "\ue202" in text or "harness corrected" in text for _, text in content)
    if private_suffix:
        assert not any("Public source" in text or "[result]" in text for when, text in content if when == 1)
        assert "[Public source](https://example.com/source)" in output
        assert "[result](https://example.com/result)" in output
    assert frames[-1][1] == "data: [DONE]\n\n"


@pytest.mark.parametrize("ensure_ascii", [False, True])
@pytest.mark.parametrize("segments", [
    [("", "skip"), ("Answer 🙂.", "eligible")],
    [(" \n\t", "skip"), ("Code:  \n    return value\r\n", "eligible")],
    [("Safe words.\x1b[31mPRIVATE\nSafe tail.", "eligible"), ("Text only.", "skip")],
    [("Source \ue202turn1search0\ue201", "skip"), ("Spoken addition.", "eligible")],
])
def test_native_public_projection_and_exact_utf16_ranges_share_sanitation(segments, ensure_ascii):
    from types import SimpleNamespace
    from workers_projects_runtime.agent_builder_control import NativeConversationParts
    from workers_projects_runtime.conversation_provider import _redact_provider_output

    delivery = {"version": 1, "audio_eligible": True}
    parts = NativeConversationParts(None, delivery)
    for index, (text, voice) in enumerate(segments):
        meta = {"promptId": "exact", "turnStartMs": 1, "streamStartMs": index + 2}
        parts.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": json.dumps({
            "type": "assistant_response", "tool_name": None, "voice": voice, "content": text})}}, meta)
        parts.update({"sessionUpdate": "response_completed"}, meta)
    raw = json.dumps(json.loads(parts.finish()), ensure_ascii=ensure_ascii)
    sources = [{"ref_id": "turn1search0", "title": "Public source", "url": "https://example.com/reference"}]
    provider = object.__new__(ConversationProvider)
    provider._native_output_snapshot = lambda *_: raw
    source_reads = []
    provider._native_citation_sources_snapshot = lambda *_: source_reads.append(True) or sources
    provider.store = SimpleNamespace(get_worker=lambda _: {"worker_id": "worker-a"})
    events = [
        {"type": "grok.session.started", "session_id": "native-a"},
        {"type": "grok.result", "session_id": "native-a", "stop_reason": "end_turn",
         "output": raw, "deliveryDispositionRanges": parts.delivery_disposition_ranges()},
    ]
    provider.service = SimpleNamespace(
        runtime=SimpleNamespace(provider_activity_log=lambda *_: ("grok-build", "\n".join(map(json.dumps, events)))),
        render_provider_native_images=lambda _request, _run, output, **_kwargs: output)
    record, run = {"request_id": "request-a"}, {"run_id": "run-a", "worker_id": "worker-a"}
    output = json.loads(provider._conversation_output(record, run))["content"]
    assert output == json.loads(_redact_provider_output(raw, sources))["content"]
    assert "PRIVATE" not in output and "\ue202" not in output
    ranges = provider._native_delivery_ranges(record, run, output)
    assert ranges and ranges[0]["start"] == 0
    assert ranges[-1]["end"] == len(output.encode("utf-16-le")) // 2
    assert all(a["end"] == b["start"] for a, b in zip(ranges, ranges[1:]))
    assert [item["disposition"]["audio"] for item in ranges] == [
        voice for text, voice in segments if text]
    assert len(source_reads) == (2 if "turn1search0" in raw else 0)
    if "turn1search0" in raw:
        assert "[Public source](https://example.com/reference)" in output


def test_native_terminal_public_projection_does_not_erase_duplicate_controls():
    from workers_projects_runtime.conversation_provider import _redact_provider_output, _sanitize_provider_output
    from workers_projects_runtime.agent_builder_control import parse_conversation_output
    raw = '{"type":"assistant_response","tool_name":null,"voice":"eligible","voice":"skip","content":"Visible."}'
    public = _redact_provider_output(_sanitize_provider_output(raw))
    with pytest.raises(ValueError):
        parse_conversation_output(public,None,{'version':1,'audio_eligible':True})


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("voice", ["eligible", "skip"])
def test_native_ordered_public_parts_survive_durable_completion_and_exact_retry(tmp_path, monkeypatch, handoff, voice):
    from workers_projects_runtime.agent_builder_control import ordered_conversation_output

    graph = {"version": 1, "tools": [{"name": "lc_transfer_to_specialist", "description": "Consult."}]}
    delivery = {"version": 1, "audio_eligible": True}
    final = json.dumps({"type": "tool_call" if handoff else "assistant_response",
                        "tool_name": "lc_transfer_to_specialist" if handoff else None,
                        "voice": voice, "content": "" if handoff else "The result is green."})
    ordered = ordered_conversation_output(["I will check the current state."], final, graph, delivery)

    class OrderedNativeRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.run_count = 0

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            _publish_in_process_test_start(worker)
            self.run_count += 1
            return ordered

        def provider_activity_log(self, worker, run_id):
            return "grok-build", "\n".join(map(json.dumps, [
                {"type": "grok.session.started", "session_id": "native-exact"},
                {"type": "grok.result", "session_id": "native-exact", "stop_reason": "end_turn", "output": ordered},
            ]))

    runtime = OrderedNativeRuntime()
    client = _client(tmp_path, monkeypatch, runtime)
    payload = _payload(tmp_path)
    payload["metadata"]["audio_eligible"] = True
    payload["tools"] = [{"type": "function", "function": {
        "name": "lc_transfer_to_specialist", "description": "Consult.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    }}]
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    completed = response.json()
    message = completed["choices"][0]["message"]
    assert message["content"] == "I will check the current state.\n\n" + ("" if handoff else "The result is green.")
    assert bool(message.get("tool_calls")) is handoff
    assert message["provider_specific_fields"]["viventium"]["delivery_disposition"]["audio"] == voice
    if handoff:
        assert message["tool_calls"][0]["function"]["name"] == "lc_transfer_to_specialist"
    record = client.app.state.store.get_provider_request(completed["id"])
    assert record["state"] == "completed"
    assert json.loads(record["response_json"]) == completed
    assert json.loads(client.app.state.store.get_run(record["run_id"])["output_text"])["content"] == message["content"]
    retry = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json() == completed
    assert runtime.run_count == 1


@pytest.mark.parametrize("model,profile_key", [("codex-cli:gpt-5.6-sol", "codex_md"), ("claude-code:opus", "claude_md"), ("grok-build:grok-4.7-build-fast", "grok_md")])
@pytest.mark.parametrize("native_tools", [False, True, None])
def test_host_conversation_projects_file_delivery_before_exact_feeling_tail(tmp_path, monkeypatch, model, profile_key, native_tools):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace, model=model)
    tail = "<viventium_feeling_state>Synthetic state.</viventium_feeling_state>"
    payload["messages"][0]["content"] += "\n\n" + tail
    payload["metadata"]["developer_instruction_tail"] = tail
    payload["metadata"]["bootstrap_bundle"] = {profile_key: "Signed broker authority."}
    if native_tools is not None:
        payload["metadata"]["bootstrap_bundle"]["provider_capabilities"] = {"native_tools": native_tools}
    parsed = ChatCompletionRequest.model_validate(payload)
    bundle = client.app.state.conversation_provider._native_bundle(parsed, GLASSHIVE_MODELS[model], "high")
    developer = bundle["developer_instructions"]
    if native_tools is False:
        assert "Conversation file delivery:" not in developer
        assert bundle["provider_capabilities"]["native_tools"] is False
        assert bundle["env"]["GLASSHIVE_PROVIDER_SESSION_MODE"] == "stateless"
        assert developer.index("Signed broker authority.") < developer.index(tail)
    else:
        assert developer.count("Conversation file delivery:") == 1
        assert developer.index("Signed broker authority.") < developer.index("Conversation file delivery:") < developer.index(tail)
    assert developer.endswith(tail) and developer.count(tail) == 1
    assert bundle["application_developer_instructions"] == payload["messages"][0]["content"]
    assert bundle[profile_key] == "Signed broker authority."
    assert "GlassHive Worker Contract" not in developer
    assert "wmctrl" not in developer and "FINAL REPORT:" not in developer


def test_existing_conversation_refreshes_registered_file_contract_without_rebinding(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first = _payload(workspace)
    first["metadata"]["stable_authority_sha256"] = "a" * 64
    response = client.post("/v1/chat/completions", headers=AUTH, json=first)
    assert response.status_code == 200, response.text
    store = client.app.state.store
    session = store.list_provider_sessions(owner_id="owner-a")[0]
    initial = json.loads(store.get_worker(session["worker_id"])["bootstrap_bundle_json"])
    assert "Conversation file delivery:" in initial["developer_instructions"]
    from workers_projects_runtime import conversation_provider as owner
    monkeypatch.setattr(owner, "conversation_file_delivery_instructions", lambda: "Updated registered delivery contract.")
    second = _payload(workspace)
    second["metadata"].update(message_id="message-current-file-contract", idempotency_key="idem-current-file-contract", stable_authority_sha256="a" * 64)
    second["messages"] = [{"role": "user", "content": "Continue."}]
    response = client.post("/v1/chat/completions", headers=AUTH, json=second)
    assert response.status_code == 200, response.text
    current = store.list_provider_sessions(owner_id="owner-a")[0]
    persisted = json.loads(store.get_worker(current["worker_id"])["bootstrap_bundle_json"])
    assert current["worker_id"] == session["worker_id"]
    assert persisted["application_developer_instructions"] == "Be a thoughtful assistant."
    assert persisted["developer_instructions"] == "Be a thoughtful assistant.\n\nUpdated registered delivery contract."


def test_docker_conversation_does_not_declare_host_managed_tmp(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    account = client.app.state.control_plane.create_provider_account(
        tenant_id="local", owner_id="owner-a", provider="codex", label="Synthetic selected account",
        auth_method="subscription", platform_support="supported", secret_locator="native-home://synthetic", status="ready")
    payload = _payload(workspace)
    payload["metadata"]["bootstrap_bundle"] = {"connection_id": account["account_id"], "codex_md": "Signed Docker broker authority."}
    payload["metadata"]["allowed_ai_origin_scope"] = {"version": 1, "tenant_id": "local", "owner_id": "owner-a", "project_id": "synthetic-project", "workspace_id": "", "connection_id": account["account_id"], "execution_mode": "docker"}
    bundle = client.app.state.conversation_provider._native_bundle(ChatCompletionRequest.model_validate(payload), GLASSHIVE_MODELS[payload["model"]], "high")
    assert bundle["developer_instructions"] == "Be a thoughtful assistant.\n\nSigned Docker broker authority."


def test_empty_application_snapshot_refreshes_structural_file_contract_without_promoting_old_authority(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first = _payload(workspace)
    first["messages"] = [{"role": "user", "content": "Hello."}]
    response = client.post("/v1/chat/completions", headers=AUTH, json=first)
    assert response.status_code == 200, response.text
    store = client.app.state.store
    session = store.list_provider_sessions(owner_id="owner-a")[0]
    from workers_projects_runtime import conversation_provider as owner
    monkeypatch.setattr(owner, "conversation_file_delivery_instructions", lambda: "Updated registered delivery contract.")
    second = _payload(workspace)
    second["metadata"].update(message_id="message-empty-authority-refresh", idempotency_key="idem-empty-authority-refresh")
    second["messages"] = [{"role": "user", "content": "Continue."}]
    response = client.post("/v1/chat/completions", headers=AUTH, json=second)
    assert response.status_code == 200, response.text
    current = store.list_provider_sessions(owner_id="owner-a")[0]
    persisted = json.loads(store.get_worker(current["worker_id"])["bootstrap_bundle_json"])
    assert current["worker_id"] == session["worker_id"]
    assert persisted["application_developer_instructions"] == ""
    assert persisted["developer_instructions"] == "Updated registered delivery contract."


@pytest.mark.parametrize('next_tail,rebind', [
    ('Memory B\nFact guard\n<viventium_feeling_state>Feeling B.</viventium_feeling_state>', False),
    ('Memory A\nFact guard', True),
    ('', True),
])
def test_codex_declared_authority_removal_reuses_existing_rebind_and_history_seed(tmp_path, monkeypatch, next_tail, rebind):
    workspace = tmp_path / 'Life'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    provider = client.app.state.conversation_provider
    first = _payload(workspace)
    tail = 'Memory A\nFact guard\n<viventium_feeling_state>Feeling A.</viventium_feeling_state>'
    first['messages'][0]['content'] = 'Stable authority.\n\n' + tail
    first['metadata'].update(developer_instruction_tail=tail,
        main_context_protocol='main_context_v1', main_context_owner='core', stable_authority_sha256='a' * 64)
    parsed = ChatCompletionRequest.model_validate(first)
    model = GLASSHIVE_MODELS[first['model']]
    original, seeded = provider._session(parsed, model, workspace, 'high', tenant_id='local')
    assert seeded
    second = _payload(workspace)
    second['messages'] = [
        {'role': 'system', 'content': 'Stable authority.' + ('\n\n' + next_tail if next_tail else '')},
        {'role': 'user', 'content': 'Earlier user goal.'},
        {'role': 'assistant', 'content': 'Earlier useful answer.'},
        {'role': 'tool', 'content': 'Earlier authorized tool result.'},
        {'role': 'user', 'content': 'Continue with the current state.'},
    ]
    second['metadata'].update(developer_instruction_tail=next_tail, message_id='message-b',
        idempotency_key='idem-b', main_context_protocol='main_context_v1', main_context_owner='core',
        stable_authority_sha256='a' * 64)
    current, seeded = provider._session(ChatCompletionRequest.model_validate(second), model, workspace, 'high', tenant_id='local')
    assert (current['worker_id'] != original['worker_id']) is rebind
    assert seeded is rebind
    if rebind:
        assert client.app.state.store.get_worker(original['worker_id'])['state'] == 'terminated'
        # This is the unchanged admitted-source replay owner used after any binding change.
        replay = _history_instruction(ChatCompletionRequest.model_validate(second).messages)
        assert 'Earlier user goal.' in replay and 'Earlier useful answer.' in replay
        assert 'Continue with the current state.' in replay
        assert 'Earlier authorized tool result.' in replay
    else:
        bundle = json.loads(client.app.state.store.get_worker(current['worker_id'])['bootstrap_bundle_json'])
        assert bundle['declared_developer_instruction_tail'] == next_tail
        assert bundle['developer_instructions'].endswith(next_tail)


class AccountSelectionRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.logs = {}
    def provider_activity_log(self, worker, run_id):
        return 'grok-build', self.logs.get(run_id, '')


def _selection_log(actual='grok-4.7', requested='grok-4.7-build-fast', *, terminal=True):
    output = json.dumps({'type': 'assistant_response', 'tool_name': None,
                         'voice': 'eligible', 'content': 'Useful answer.'})
    events = [{'type': 'grok.session.started', 'session_id': 'native-account-session',
               'model': actual, 'requested_model': requested},
              {'type': 'grok.session.update', 'session_id': 'native-account-session',
               'update': {'sessionUpdate': 'agent_message_chunk',
                          'content': {'type': 'text', 'text': output}}}]
    if terminal:
        events.append({'type': 'grok.result', 'session_id': 'native-account-session',
                       'stop_reason': 'end_turn', 'output': output})
    return '\n'.join(map(json.dumps, events))


def test_actual_account_selection_preserves_binding_across_turns_and_replay(tmp_path, monkeypatch):
    runtime = AccountSelectionRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    provider, store, service = client.app.state.conversation_provider, client.app.state.store, client.app.state.service
    monkeypatch.setenv('GLASSHIVE_ALLOWED_WORKER_PROFILES', 'codex-cli,claude-code,grok-build')
    monkeypatch.setattr(service, 'start_assigned_run', lambda _: None)
    monkeypatch.setattr(service, '_ensure_worker_processor', lambda _: None)
    payload = _payload(tmp_path, model='grok-build:grok-4.7-build-fast', stream=True)
    payload['reasoning_effort'] = 'high'
    payload['metadata']['audio_eligible'] = True
    session_ids, worker_ids = set(), set()
    for number, actual in enumerate(['grok-4.7', 'grok-4.7', 'grok-4.7-build-fast'], 1):
        payload['metadata'].update(message_id=f'message-{number}', stream_id=f'stream-{number}',
                                   idempotency_key=f'idem-{number}')
        if number > 1:
            payload['messages'].extend([{'role': 'assistant', 'content': 'Useful answer.'},
                                        {'role': 'user', 'content': f'Continue goal {number}.'}])
        parsed = ChatCompletionRequest.model_validate(payload)
        request = provider.start(parsed)
        authority = json.loads(request['replay_decision_json'])['request_authority_sha256']
        runtime.logs[request['run_id']] = _selection_log(actual)
        run = store.update_run(request['run_id'], state='completed', native_session_id='native-account-session',
                               output_text='Useful answer.')
        final = provider._sync(request)
        canonical = provider.response_payload(final, run, parsed)
        assert canonical['model'] == 'grok-build:' + actual
        assert canonical['glasshive'].get('requested_model') == (payload['model'] if actual == 'grok-4.7' else None)
        assert provider.response_payload(final, run, parsed) == canonical
        # Completed replay uses the existing sealed response even after log retention.
        runtime.logs.pop(request['run_id'])
        current = store.get_provider_request(request['request_id'])
        decision = json.loads(current['replay_decision_json'])
        assert decision['completion_contract_v1']['model'] == payload['model']
        assert decision['request_authority_sha256'] == authority
        session = store.get_provider_session_by_id(current['session_id'])
        worker = store.get_worker(run['worker_id'])
        assert session['model_id'] == payload['model'] and worker['model'] == 'grok-4.7-build-fast'
        assert json.loads(worker['bootstrap_bundle_json'])['env']['WPR_GROK_REASONING_EFFORT'] == 'high'
        session_ids.add(session['session_id']); worker_ids.add(worker['worker_id'])
        duplicate = provider.start(parsed)
        assert duplicate['run_id'] == request['run_id']
        assert '_authority_recovery_source' not in (provider._request_local_bundle(
            request['request_id'], expected_run_id=request['run_id']) or {})
        class Connected:
            async def is_disconnected(self): return False
        async def collect():
            return [x async for x in provider._stream_chunks(final, parsed, Connected())]
        events = [json.loads(x.removeprefix('data: ').strip()) for x in asyncio.run(collect()) if x.startswith('data: {')]
        assert all(x['model'] == canonical['model'] for x in events)
        public = [x for x in events if any(c.get('delta', {}).get('content') for c in x.get('choices', []))]
        assert public and all(x['model'] == canonical['model'] for x in public)
        if actual == 'grok-4.7':
            assert all(x.get('glasshive', {}).get('requested_model') == payload['model'] for x in public)
        from workers_projects_runtime.conversation_provider import _responses_from_chat, _responses_stream, ResponsesRequest
        responses = _responses_from_chat(canonical, ResponsesRequest(model=payload['model'], input='Current goal.'))
        assert responses['model'] == canonical['model']
        assert responses['glasshive'].get('requested_model') == canonical['glasshive'].get('requested_model')
        async def collect_responses():
            return [x async for x in _responses_stream(provider, final,
                ResponsesRequest(model=payload['model'], input='Current goal.'), parsed, Connected())]
        frames = [json.loads(line[6:]) for frame in asyncio.run(collect_responses())
                  for line in frame.splitlines() if line.startswith('data: {')]
        completed = next(frame['response'] for frame in frames if frame['type'] == 'response.completed')
        assert completed['model'] == canonical['model']
        assert completed['glasshive'].get('requested_model') == canonical['glasshive'].get('requested_model')
        altered = {**payload, 'messages': [*payload['messages'], {'role': 'user', 'content': 'Changed goal.'}]}
        from fastapi import HTTPException
        with pytest.raises(HTTPException, match='authority changed'):
            provider.start(ChatCompletionRequest.model_validate(altered))
    assert len(session_ids) == len(worker_ids) == 1
    assert not any(x['event_type'] == 'context-recovery' for x in store.list_provider_activity(request['request_id']))


@pytest.mark.parametrize('invalid', ['wrong_requested', 'wrong_effective', 'multiple', 'wrong_session', 'foreign_owner', 'wrong_run'])
def test_effective_model_projection_rejects_foreign_or_unapproved_native_identity(tmp_path, monkeypatch, invalid):
    runtime = AccountSelectionRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    provider, store, service = client.app.state.conversation_provider, client.app.state.store, client.app.state.service
    monkeypatch.setenv('GLASSHIVE_ALLOWED_WORKER_PROFILES', 'codex-cli,claude-code,grok-build')
    monkeypatch.setattr(service, 'start_assigned_run', lambda _: None)
    monkeypatch.setattr(service, '_ensure_worker_processor', lambda _: None)
    request = provider.start(ChatCompletionRequest.model_validate(_payload(tmp_path, model='grok-build:grok-4.7-build-fast')))
    run = store.update_run(request['run_id'], native_session_id='native-account-session')
    stdout = _selection_log(requested='wrong-model' if invalid == 'wrong_requested' else 'grok-4.7-build-fast',
                            actual='wrong-model' if invalid == 'wrong_effective' else 'grok-4.7')
    if invalid == 'multiple': stdout += '\n' + stdout.splitlines()[0]
    if invalid == 'wrong_session': stdout = stdout.replace('native-account-session', 'foreign-session')
    if invalid == 'foreign_owner': request = {**request, 'owner_id': 'foreign-owner'}
    if invalid == 'wrong_run': run = {**run, 'run_id': 'foreign-run'}
    runtime.logs[run['run_id']] = stdout
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        provider._native_effective_model(request, run, provider._saved_completion_contract(request))


def _authority_recovery_fixture(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    provider, store = client.app.state.conversation_provider, client.app.state.store
    monkeypatch.setattr(client.app.state.service, 'start_assigned_run', lambda worker_id: None)
    # Hold the native boundary: these cases supply a typed preflight failure, not a
    # competing successful StubRuntime run from the restart-backlog dispatcher.
    monkeypatch.setattr(client.app.state.service, '_ensure_worker_processor', lambda worker_id: None)
    payload = _payload(tmp_path, model='codex-cli:gpt-6.1-sol')
    payload['messages'] = [
        {'role': 'system', 'content': 'Exact current developer authority.'},
        {'role': 'user', 'content': 'Earlier accepted goal.'},
        {'role': 'assistant', 'content': 'Earlier useful answer.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'tool-a', 'type': 'function',
            'function': {'name': 'file_read', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'tool-a', 'content': 'Earlier verified tool evidence.'},
        {'role': 'user', 'content': 'Current accepted goal.'},
    ]
    request = provider.start(ChatCompletionRequest.model_validate(payload))
    run = store.update_run(request['run_id'], state='failed',
        failure_class='authority_update_unconfirmed', failure_structured=1, failure_retryable=1)
    return client, provider, store, request, run












def test_authority_failure_rebinds_once_with_canonical_source_not_native_delta(tmp_path, monkeypatch):
    client, provider, store, request, run = _authority_recovery_fixture(tmp_path, monkeypatch)
    transient = provider._request_local_bundle(request['request_id'], expected_run_id=run['run_id'])
    assert transient['_authority_recovery_source']['messages'][1]['content'] == 'Earlier accepted goal.'
    worker = store.get_worker(run['worker_id'])
    assert '_authority_recovery_source' not in json.loads(worker['bootstrap_bundle_json'])
    # Even when this native attempt carries only a delta, its fresh replacement needs all source.
    request = store.update_provider_request(request['request_id'], admitted_instruction='Current accepted goal.')
    assert provider._context_recovery_eligible(request, run, set())
    replacement = provider._start_context_recovery(request, run)
    replacement_run = store.get_run(replacement['run_id'])
    assert replacement_run['worker_id'] != run['worker_id']
    assert all(text in replacement_run['instruction'] for text in [
        'Earlier accepted goal.', 'Earlier useful answer.', 'Earlier verified tool evidence.', 'Current accepted goal.'])
    assert replacement['fallback_from_run_id'] == 'context_recovery:' + run['run_id']
    assert '_authority_recovery_source' not in (provider._request_local_bundle(
        request['request_id'], expected_run_id=replacement_run['run_id']) or {})
    store.update_run(replacement_run['run_id'], state='failed',
        failure_class='authority_update_unconfirmed', failure_structured=1)
    assert not provider._context_recovery_eligible(replacement, store.get_run(replacement_run['run_id']), set())
    final = provider._sync(replacement)
    assert final['state'] == 'failed'
    assert final['run_id'] == replacement_run['run_id']


@pytest.mark.parametrize('invalid', ['source_lost', 'wrong_run', 'output', 'activity'])
def test_authority_recovery_requires_current_complete_source_and_no_effect(tmp_path, monkeypatch, invalid):
    client, provider, store, request, run = _authority_recovery_fixture(tmp_path, monkeypatch)
    activities = set()
    if invalid == 'source_lost':
        provider._forget_request_local_bundle(request['request_id'])
    elif invalid == 'wrong_run':
        run = {**run, 'run_id': 'foreign-attempt'}
    elif invalid == 'output':
        run = {**run, 'output_text': 'Authored useful output.'}
    else:
        activities = {'tool'}
    assert not provider._context_recovery_eligible(request, run, activities)
    if invalid == 'source_lost':
        from fastapi import HTTPException
        final = provider._sync(request)
        with pytest.raises(HTTPException) as failure:
            provider.response_payload(final, run, ChatCompletionRequest.model_validate(_payload(tmp_path)))
        assert failure.value.detail['code'] == 'authority_update_unconfirmed'


@pytest.mark.parametrize('order', ['before_failure', 'after_failure', 'during_rebind'])
def test_stop_wins_authority_failure_recovery_in_both_orders(tmp_path, monkeypatch, order):
    client, provider, store, request, run = _authority_recovery_fixture(tmp_path, monkeypatch)
    if order != 'during_rebind':
        if order == 'before_failure':
            store.update_run(run['run_id'], state='running', failure_class='')
        provider.cancel_by_idempotency('idem-a', 'owner-a')
        if order == 'before_failure':
            store.update_run(run['run_id'], failure_class='authority_update_unconfirmed', failure_structured=1)
        current = store.get_provider_request(request['request_id'])
        assert current['state'] == 'cancelled'
        assert not provider._context_recovery_eligible(current, store.get_run(run['run_id']), set())
        assert provider._start_context_recovery(current, store.get_run(run['run_id']))['run_id'] == run['run_id']
    else:
        activate = client.app.state.service.activate_prepared_conversation_worker
        def stop_then_activate(worker_id):
            provider.cancel_by_idempotency('idem-a', 'owner-a')
            return activate(worker_id)
        monkeypatch.setattr(client.app.state.service, 'activate_prepared_conversation_worker', stop_then_activate)
        current = provider._start_context_recovery(request, run)
        assert current['state'] == 'cancelled'
        assert current['run_id'] == run['run_id']
    assert not any(item['event_type'] == 'context-recovery'
                   for item in store.list_provider_activity(request['request_id']))


def test_authority_rebind_failure_remains_typed_and_never_starts_a_second_recovery(tmp_path, monkeypatch):
    client, provider, store, request, run = _authority_recovery_fixture(tmp_path, monkeypatch)
    def reject(*args, **kwargs):
        raise RuntimeError('Synthetic replacement readiness failure')
    monkeypatch.setattr(client.app.state.service, 'activate_prepared_conversation_worker', reject)
    result = provider._start_context_recovery(request, run)
    assert result['state'] == 'failed'
    assert result['fallback_state'] == 'context_recovery_failed'
    assert result['run_id'] == run['run_id']
    assert not provider._context_recovery_eligible(result, run, set())
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:
        provider.response_payload(result, run, ChatCompletionRequest.model_validate(_payload(tmp_path)))
    assert failure.value.detail['code'] == 'authority_update_unconfirmed'


@pytest.mark.parametrize('guard', ['_native_source_coverage_proven', '_native_tool_evidence_coverage_proven'])
def test_authority_recovery_does_not_capture_unproven_full_source(tmp_path, monkeypatch, guard):
    client = _client(tmp_path, monkeypatch)
    provider = client.app.state.conversation_provider
    monkeypatch.setattr(client.app.state.service, 'start_assigned_run', lambda worker_id: None)
    monkeypatch.setattr(client.app.state.service, '_ensure_worker_processor', lambda worker_id: None)
    monkeypatch.setattr(provider, guard, lambda *args, **kwargs: False)
    request = provider.start(ChatCompletionRequest.model_validate(_payload(tmp_path, model='codex-cli:gpt-6.1-sol')))
    transient = provider._request_local_bundle(request['request_id'], expected_run_id=request['run_id'])
    assert '_authority_recovery_source' not in transient


class ElectedFallbackSelectionRuntime(AccountSelectionRuntime):
    def provider_activity_log(self, worker, run_id):
        return str(worker['profile']), self.logs.get(run_id, '')


def _elected_fast_fallback(tmp_path, monkeypatch, fallback_model):
    runtime = ElectedFallbackSelectionRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    provider, store, service = client.app.state.conversation_provider, client.app.state.store, client.app.state.service
    monkeypatch.setenv('GLASSHIVE_ALLOWED_WORKER_PROFILES', 'codex-cli,claude-code,grok-build')
    monkeypatch.setattr(service, 'start_assigned_run', lambda _: None)
    monkeypatch.setattr(service, '_ensure_worker_processor', lambda _: None)
    payload = _payload(tmp_path, model='grok-build:grok-4.7-build-fast', stream=True)
    payload['reasoning_effort'] = 'high'
    payload['metadata'].update(fallback_model=fallback_model, fallback_reasoning_effort='high',
                               audio_eligible=True, native_invocation_id='native-fallback-invocation')
    parsed = ChatCompletionRequest.model_validate(payload)
    primary = provider.start(parsed)
    authority = json.loads(primary['replay_decision_json'])['request_authority_sha256']
    store.update_run(primary['run_id'], state='failed', failure_class='provider_quota_exhausted',
                     failure_retryable=1, failure_structured=1, retry_attempts=0, output_text='')
    elected = provider._sync(primary)
    assert elected['fallback_state'] == 'started' and elected['run_id'] != primary['run_id']
    assert elected['fallback_from_run_id'] == primary['run_id']
    assert json.loads(elected['replay_decision_json'])['request_authority_sha256'] == authority
    assert provider._saved_completion_contract(elected)['model'] == payload['model']
    session = store.get_provider_session_by_id(elected['session_id'])
    run = store.get_run(elected['run_id'])
    worker = store.get_worker(run['worker_id'])
    assert session['model_id'] == fallback_model and session['worker_id'] == worker['worker_id']
    assert json.loads(worker['bootstrap_bundle_json'])['env'].get(
        'WPR_GROK_REASONING_EFFORT' if worker['profile'] == 'grok-build' else 'WPR_CLAUDE_CODE_EFFORT') == 'high'
    output = json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': 'Useful answer.'})
    runtime.logs[run['run_id']] = (_selection_log(actual='grok-4.7', requested='grok-4.7')
        if worker['profile'] == 'grok-build' else '\n'.join(map(json.dumps, [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': output}]}},
            {'type': 'result', 'result': output}])))
    run = store.update_run(run['run_id'], native_session_id='native-account-session')
    return provider, store, runtime, parsed, elected, run


@pytest.mark.parametrize('fallback_model', ['grok-build:grok-4.7', 'claude-code:opus'])
def test_elected_fallback_preserves_actual_model_in_live_completion_and_replay(tmp_path, monkeypatch, fallback_model):
    provider, store, runtime, parsed, elected, run = _elected_fast_fallback(tmp_path, monkeypatch, fallback_model)
    assert provider._native_effective_model(elected, run, provider._saved_completion_contract(elected)) == fallback_model
    class Connected:
        async def is_disconnected(self): return False
    async def live():
        stream = provider._stream_chunks(elected, parsed, Connected())
        rows = []
        try:
            async for frame in stream:
                if not frame.startswith('data: {'): continue
                row = json.loads(frame.removeprefix('data: ').strip()); rows.append(row)
                if any(choice.get('delta', {}).get('content') or choice.get('delta', {}).get(
                    'provider_specific_fields', {}).get('viventium', {}).get('assistant_preview', {}).get('text')
                    for choice in row.get('choices', [])): break
        finally:
            await stream.aclose()
        return rows
    live_rows = asyncio.run(live())
    useful = [row for row in live_rows if any(choice.get('delta', {}).get('content') or choice.get('delta', {}).get(
        'provider_specific_fields', {}).get('viventium', {}).get('assistant_preview', {}).get('text')
        for choice in row.get('choices', []))]
    assert all(row['model'] == fallback_model for row in live_rows)
    assert useful and all(row['model'] == fallback_model and row['glasshive']['requested_model'] == parsed.model for row in useful)
    assert store.get_provider_request(elected['request_id'])['state'] == 'queued'
    run = store.update_run(run['run_id'], state='completed', output_text='Useful answer.')
    final = provider._sync(store.get_provider_request(elected['request_id']))
    assert final['state'] == 'completed'
    canonical = provider.response_payload(final, run, parsed)
    assert canonical['model'] == fallback_model and canonical['glasshive']['requested_model'] == parsed.model
    # The shared session row follows a later primary turn; sealed fallback replay owns
    # its immutable elected run/worker, not the later turn's mutable worker pointer.
    next_payload = parsed.model_dump(mode='json')
    next_payload['messages'].extend([{'role': 'assistant', 'content': 'Useful answer.'},
                                     {'role': 'user', 'content': 'Next current goal.'}])
    next_payload['metadata'].update(message_id='message-next', stream_id='stream-next',
        idempotency_key='idem-next', native_invocation_id='native-next-invocation')
    next_request = provider.start(ChatCompletionRequest.model_validate(next_payload))
    next_session = store.get_provider_session_by_id(next_request['session_id'])
    assert next_session['model_id'] == parsed.model and next_session['worker_id'] != run['worker_id']
    runtime.logs.clear()
    assert provider.response_payload(final, run, parsed) == canonical
    assert provider._native_effective_model(final, run, provider._saved_completion_contract(final)) == fallback_model
    async def replay(): return [row async for row in provider._stream_chunks(final, parsed, Connected())]
    rows = [json.loads(frame.removeprefix('data: ').strip()) for frame in asyncio.run(replay()) if frame.startswith('data: {')]
    assert rows and all(row['model'] == fallback_model for row in rows)
    from workers_projects_runtime.conversation_provider import _responses_from_chat, _responses_stream, ResponsesRequest
    responses_request = ResponsesRequest(model=parsed.model, input='Current goal.')
    assert _responses_from_chat(canonical, responses_request)['model'] == fallback_model
    async def responses(): return [row async for row in _responses_stream(provider, final, responses_request, parsed, Connected())]
    frames = [json.loads(line[6:]) for frame in asyncio.run(responses()) for line in frame.splitlines() if line.startswith('data: {')]
    lifecycle = [frame['response'] for frame in frames if frame['type'] in {
        'response.created', 'response.in_progress', 'response.completed'}]
    assert len(lifecycle) == 3 and all(response['model'] == fallback_model
        and response['glasshive']['requested_model'] == parsed.model for response in lifecycle)
    completed = next(frame['response'] for frame in frames if frame['type'] == 'response.completed')
    assert completed['model'] == fallback_model and completed['glasshive']['requested_model'] == parsed.model


@pytest.mark.parametrize('invalid', ['unelected', 'wrong_model', 'foreign_owner', 'foreign_worker', 'wrong_run', 'wrong_native_model'])
def test_elected_fallback_model_projection_keeps_current_authority_fences(tmp_path, monkeypatch, invalid):
    provider, store, runtime, parsed, elected, run = _elected_fast_fallback(tmp_path, monkeypatch, 'grok-build:grok-4.7')
    if invalid == 'unelected': elected = {**elected, 'fallback_state': ''}
    if invalid == 'wrong_model': elected = {**elected, 'fallback_model_id': 'grok-build:unapproved'}
    if invalid == 'foreign_owner': elected = {**elected, 'owner_id': 'foreign-owner'}
    if invalid == 'foreign_worker': run = {**run, 'worker_id': 'foreign-worker'}
    if invalid == 'wrong_run': run = {**run, 'run_id': 'foreign-run'}
    if invalid == 'wrong_native_model': runtime.logs[run['run_id']] = _selection_log(actual='unapproved', requested='grok-4.7')
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        provider._native_effective_model(elected, run, provider._saved_completion_contract(elected))


@pytest.mark.parametrize('fallback_model', ['grok-build:grok-4.7', 'claude-code:opus'])
def test_stop_keeps_elected_fallback_identity_and_cancels_exact_current_run(tmp_path, monkeypatch, fallback_model):
    provider, store, runtime, parsed, elected, run = _elected_fast_fallback(tmp_path, monkeypatch, fallback_model)
    cancelled_runs = []
    original_cancel = provider.service.cancel_run
    def cancel_exact(worker_id, run_id):
        cancelled_runs.append((worker_id, run_id))
        return original_cancel(worker_id, run_id)
    monkeypatch.setattr(provider.service, 'cancel_run', cancel_exact)
    stopped = provider.cancel(elected['request_id'])
    current_run = store.get_run(run['run_id'])
    assert stopped['state'] == 'cancelled' and stopped['fallback_state'] == 'started'
    assert cancelled_runs == [(run['worker_id'], run['run_id'])]
    assert current_run['state'] == 'cancelled' and stopped['run_id'] == elected['run_id']
    assert provider._native_effective_model(stopped, current_run, provider._saved_completion_contract(stopped)) == fallback_model
    assert provider._sync(stopped)['state'] == 'cancelled'
    assert store.get_provider_request(elected['request_id'])['run_id'] == elected['run_id']
    from workers_projects_runtime.conversation_provider import _responses_stream, ResponsesRequest
    class Connected:
        async def is_disconnected(self): return False
    async def failed_responses():
        return [row async for row in _responses_stream(provider, stopped,
            ResponsesRequest(model=parsed.model, input='Current goal.'), parsed, Connected())]
    frames = [json.loads(line[6:]) for frame in asyncio.run(failed_responses())
              for line in frame.splitlines() if line.startswith('data: {')]
    failed = next(frame['response'] for frame in frames if frame['type'] == 'response.failed')
    assert failed['model'] == fallback_model and failed['glasshive']['requested_model'] == parsed.model


@pytest.mark.parametrize('fallback_model', ['grok-build:grok-4.7', 'claude-code:opus'])
def test_stop_before_native_selection_keeps_elected_lifecycle_model_without_public_bytes(tmp_path, monkeypatch, fallback_model):
    provider, store, runtime, parsed, elected, run = _elected_fast_fallback(tmp_path, monkeypatch, fallback_model)
    runtime.logs.clear()  # The elected worker has not emitted session.started or any authored output.
    stopped = provider.cancel(elected['request_id'])
    current_run = store.get_run(run['run_id'])
    assert stopped['state'] == current_run['state'] == 'cancelled'
    assert stopped['run_id'] == run['run_id'] and stopped['fallback_state'] == 'started'
    selected = provider._native_effective_model(stopped, current_run, provider._saved_completion_contract(stopped))
    assert selected == (None if fallback_model.startswith('grok-build:') else fallback_model)
    class Connected:
        async def is_disconnected(self): return False
    async def stream(): return [row async for row in provider._stream_chunks(stopped, parsed, Connected())]
    rows = [json.loads(frame.removeprefix('data: ').strip()) for frame in asyncio.run(stream()) if frame.startswith('data: {')]
    assert rows and all(row['model'] == fallback_model and row['glasshive']['requested_model'] == parsed.model for row in rows)
    assert any(row.get('error') for row in rows)
    assert not any(choice.get('delta', {}).get('content') or choice.get('delta', {}).get(
        'provider_specific_fields', {}).get('viventium', {}).get('assistant_preview')
        for row in rows for choice in row.get('choices', []))
    from workers_projects_runtime.conversation_provider import _responses_stream, ResponsesRequest
    async def responses(): return [row async for row in _responses_stream(provider, stopped,
        ResponsesRequest(model=parsed.model, input='Current goal.'), parsed, Connected())]
    frames = [json.loads(line[6:]) for frame in asyncio.run(responses()) for line in frame.splitlines() if line.startswith('data: {')]
    lifecycle = [frame['response'] for frame in frames if frame['type'] in {
        'response.created', 'response.in_progress', 'response.failed'}]
    assert len(lifecycle) == 3 and all(response['model'] == fallback_model
        and response['glasshive']['requested_model'] == parsed.model for response in lifecycle)
    assert not any(frame['type'] == 'response.output_text.delta' for frame in frames)


@pytest.mark.parametrize('model', [
    'codex-cli:gpt-6.1-sol', 'claude-code:claude-opus-5-5',
    'grok-build:grok-4.7-build-fast',
])
@pytest.mark.parametrize('requested_mode,native_tools,effective_mode', [
    ('stateless', True, 'stateless'),
    ('persistent', True, 'persistent'),
    ('persistent', False, 'stateless'),
    (None, False, 'stateless'),
])
def test_reused_stateless_context_replays_prior_visible_source(
    tmp_path, monkeypatch, model, requested_mode, native_tools, effective_mode,
):
    class ReplayRuntime(StubRuntime):
        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            _publish_in_process_test_start(worker)
            self.calls = getattr(self, 'calls', 0) + 1
            return f'Independent assistant answer {self.calls}.'

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    runtime = ReplayRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code,grok-build")
    headers = {
        **AUTH,
        **_signed_bundle_headers({'provider_capabilities': {'native_tools': native_tools}}),
    }
    if requested_mode is not None:
        headers['X-GlassHive-Provider-Session-Mode'] = requested_mode
    source = 'Original source request.'
    first_payload = _payload(workspace, model=model)
    first_payload['messages'][-1]['content'] = source
    first_payload['metadata'].update({
        'message_id': 'response-1', 'idempotency_key': 'request-1',
        'main_context_protocol': 'main_context_v1', 'main_context_owner': 'core',
        'stable_authority_sha256': 'a' * 64, 'main_context_snapshot_sha256': 'b' * 64,
        'main_context_epoch': 'c' * 64, 'continuity_domain_id': 'd' * 64,
        'continuity_agent_id': 'agent-main', 'logical_turn_id': 'turn-1',
        'visible_message_chain': [{
            'id': 'source-1', 'role': 'user', 'accepted_source': False,
            'sha256': hashlib.sha256(source.encode()).hexdigest(),
        }],
    })
    first = client.post('/v1/chat/completions', headers=headers, json=first_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    first_row = store.get_provider_request(first.json()['id'])
    first_run = store.get_run(first_row['run_id'])
    # Core can add delivery annotations to the visible assistant source.
    answer = first.json()['choices'][0]['message']['content'] + ' Public delivery annotation.'
    current = 'Continue with the current question.'
    second_payload = _payload(workspace, model=model)
    second_payload['messages'] = [
        *first_payload['messages'],
        {'role': 'assistant', 'content': answer},
        {'role': 'user', 'content': current},
    ]
    second_payload['metadata'].update({
        **first_payload['metadata'],
        'message_id': 'response-2', 'idempotency_key': 'request-2',
        'logical_turn_id': 'turn-2',
        'visible_message_chain': [
            first_payload['metadata']['visible_message_chain'][0],
            {'id': 'response-1', 'role': 'assistant', 'accepted_source': False,
             'sha256': hashlib.sha256(answer.encode()).hexdigest()},
            {'id': 'source-2', 'role': 'user', 'accepted_source': False,
             'sha256': hashlib.sha256(current.encode()).hexdigest()},
        ],
    })
    second = client.post('/v1/chat/completions', headers=headers, json=second_payload)
    assert second.status_code == 200, second.text
    row = store.get_provider_request(second.json()['id'])
    run = store.get_run(row['run_id'])
    assert row['session_id'] == first_row['session_id']
    assert run['worker_id'] == first_run['worker_id']
    decision = json.loads(row['replay_decision_json'])
    assert decision['provider_session_mode'] == effective_mode
    instruction = row['admitted_instruction']
    historical, actionable = instruction.split('<viventium_current_accepted_turn_v1>', 1)
    assert answer in historical
    assert current not in historical
    assert current in actionable and instruction.count(current) == 1
    assert source not in actionable
    if effective_mode == 'stateless':
        assert decision['mode'] == 'bootstrap'
        assert decision['admitted_message_indices'] == [1, 2, 3]
        assert source in historical
        assert historical.index(source) < historical.index(answer)
    else:
        assert decision['mode'] == 'delta'
        assert decision['admitted_message_indices'] == [2, 3]
        assert source not in instruction
    assert decision['instruction_bytes'] <= decision['budget_bytes']
    assert not decision['input_accepted_sources']
    assert runtime.calls == 2


def _pre_admission_context_payload(workspace, number, history):
    value = _payload(workspace)
    value['messages'] = [value['messages'][0], *history]
    value['metadata'].update({
        'message_id': f'answer-{number}', 'stream_id': f'stream-{number}',
        'idempotency_key': f'ordering-{number}', 'main_context_protocol': 'main_context_v1',
        'main_context_owner': 'core', 'stable_authority_sha256': 'a' * 64,
        'main_context_snapshot_sha256': 'b' * 64, 'main_context_epoch': 'c' * 64,
        'continuity_domain_id': 'd' * 64, 'continuity_agent_id': 'agent-main',
        'logical_turn_id': f'turn-{number}',
        'visible_message_chain': [{
            'id': f'source-{index}', 'role': message['role'], 'accepted_source': True,
            'sha256': hashlib.sha256(message['content'].encode()).hexdigest(),
        } for index, message in enumerate(history)],
    })
    return value


def _pre_admission_saved_authority(store):
    sessions = store.list_provider_sessions(owner_id='owner-a')
    assert len(sessions) == 1
    session = sessions[0]
    worker = store.get_worker(session['worker_id'])
    return {
        'session_id': session['session_id'], 'worker_id': session['worker_id'],
        'history_count': session['history_count'],
        'context_manifest': json.loads(session['context_manifest_json']),
        'bootstrap_bundle': json.loads(worker['bootstrap_bundle_json']),
        'workspace_dir': worker['workspace_dir'], 'model': worker['model'],
    }


@pytest.mark.parametrize('failure', ['changed_source_413', 'unsigned_bundle_403'])
def test_pre_admission_failure_preserves_accepted_session(tmp_path, monkeypatch, failure):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    question = {'role': 'user', 'content': 'Keep the accepted original goal.'}
    first_payload = _pre_admission_context_payload(workspace, 1, [question])
    first = client.post('/v1/chat/completions', headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    before = _pre_admission_saved_authority(store)
    manifest = before['context_manifest']
    assert manifest['accepted_visible_message_keys']
    assert manifest['last_accepted_replay_decision_v1']
    successor = _pre_admission_context_payload(workspace, 2, [question, {'role': 'user', 'content': 'Continue safely.'}])
    headers = dict(AUTH)
    if failure == 'changed_source_413':
        successor['messages'][1]['content'] = 'Changed original goal.'
        expected_status = 413
    else:
        headers['X-GlassHive-Bootstrap-Bundle-B64'] = 'eyJlbnYiOnsic3ludGhldGljIjoidmFsdWUifX0='
        expected_status = 403
    response = client.post('/v1/chat/completions', headers=headers, json=successor)
    assert response.status_code == expected_status, response.text
    assert _pre_admission_saved_authority(store) == before


@pytest.mark.parametrize('branch', [False, True])
def test_valid_pre_admission_keeps_existing_replay_result(tmp_path, monkeypatch, branch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    question = {'role': 'user', 'content': 'Keep the accepted original goal.'}
    first = client.post('/v1/chat/completions', headers=AUTH,
                        json=_pre_admission_context_payload(workspace, 1, [question]))
    assert first.status_code == 200, first.text
    first_answer = first.json()['choices'][0]['message']['content']
    history = [question]
    if not branch:
        history.append({'role': 'assistant', 'content': first_answer})
    history.append({'role': 'user', 'content': 'Continue safely.'})
    successor = _pre_admission_context_payload(workspace, 2, history)
    if not branch:
        successor['metadata']['visible_message_chain'][1]['id'] = 'answer-1'
    second = client.post('/v1/chat/completions', headers=AUTH, json=successor)
    assert second.status_code == 200, second.text
    store = client.app.state.store
    first_record = store.get_provider_request(first.json()['id'])
    second_record = store.get_provider_request(second.json()['id'])
    assert second_record['session_id'] == first_record['session_id']
    decision = json.loads(second_record['replay_decision_json'])
    assert decision['mode'] == ('bootstrap' if branch else 'delta')
    assert second.json()['choices'][0]['message']['content'] == first_answer
    duplicate = client.post('/v1/chat/completions', headers=AUTH, json=successor)
    assert duplicate.status_code == 200
    assert duplicate.json()['id'] == second.json()['id']

def _public_completion_stdout(parts):
    events = [{'type': 'grok.session.started', 'session_id': 'native-a'}]
    for index, part in enumerate(parts):
        if index:
            events.append({'type': 'grok.session.update', 'session_id': 'native-a',
                           'update': {'sessionUpdate': 'tool_call'}})
        events.append({'type': 'grok.session.update', 'session_id': 'native-a', 'update': {
            'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': part}}})
    return 'grok-build', '\n'.join(map(json.dumps, events))


def test_public_part_completion_projects_each_full_assistant_part_without_terminal():
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    bodies = [json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible',
                         'content': text}) for text in ['<emphasis>First part.</emphasis>', 'Later complete part.']]
    profile, stdout = _public_completion_stdout(bodies)
    result = _native_spoken_content(profile, stdout, None, {'version': 1, 'audio_eligible': True})
    assert result['completed_parts'] == [
        {'part_index': 1, 'text_end': len('<emphasis>First part.</emphasis>')},
        {'part_index': 2, 'text_end': len(result['text'])}]


def test_public_part_completion_seals_declared_eligible_graph_narration():
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    name = 'lc_transfer_to_specialist'
    body = json.dumps({'type': 'tool_call', 'tool_name': name, 'voice': 'eligible',
                       'content': 'I am checking the evidence.'})
    profile, stdout = _public_completion_stdout([body])
    graph = {'version': 1, 'tools': [{'name': name, 'description': 'Consult.'}]}
    result = _native_spoken_content(profile, stdout, graph, {'version': 1, 'audio_eligible': True})
    assert result['text'] == 'I am checking the evidence.'
    assert result['completed_parts'] == [{'part_index': 1, 'text_end': len(result['text'])}]
    assert result['delivery_disposition'] == {
        'version': 1, 'audio': 'eligible', 'required': True, 'valid': True, 'source': 'model'}


@pytest.mark.parametrize('case', ['skip', 'undeclared', 'invalid_voice', 'extra_field',
                                  'incomplete', 'file', 'citation'])
def test_public_graph_completion_preserves_control_and_mutable_projection_fences(case):
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    name = 'lc_transfer_to_specialist'
    envelope = {'type': 'tool_call', 'tool_name': name, 'voice': 'eligible', 'content': 'Checking.'}
    if case == 'skip':
        envelope['voice'] = 'skip'
    elif case == 'undeclared':
        envelope['tool_name'] = 'lc_transfer_to_foreign'
    elif case == 'invalid_voice':
        envelope['voice'] = 'unknown'
    elif case == 'extra_field':
        envelope['extra'] = True
    elif case == 'file':
        envelope['content'] = 'Checking. [File](file.txt)'
    elif case == 'citation':
        envelope['content'] = 'Checking. \ue200cite\ue202synthetic-source\ue201'
    body = json.dumps(envelope)
    if case == 'incomplete':
        body = body[:-2]
    profile, stdout = _public_completion_stdout([body])
    graph = {'version': 1, 'tools': [{'name': name, 'description': 'Consult.'}]}
    result = _native_spoken_content(profile, stdout, graph, {'version': 1, 'audio_eligible': True})
    assert not result or not result.get('completed_parts')


@pytest.mark.parametrize('completion_only', [False, True])
def test_graph_narration_completion_precedes_terminal_transfer_without_waiting(monkeypatch, completion_only):
    state, provider, payload = _spoken_family_fixture()
    state['stage'] = 0
    name = 'lc_transfer_to_specialist'
    text = 'I am checking the evidence.'
    envelope = {'type': 'tool_call', 'tool_name': name, 'voice': 'eligible',
                'content': text + (' ' if completion_only else '')}
    body = json.dumps(envelope)
    partial = body[:-2]
    graph = {'version': 1, 'tools': [{'name': name, 'description': 'Consult.'}]}
    provider._graph_transfer_control = lambda *_: graph
    provider.service.runtime.provider_activity_log = lambda *_: _public_completion_stdout([
        partial if completion_only and state['stage'] == 1 else body])
    final = {'model': payload.model, 'choices': [{'message': {
        'role': 'assistant', 'content': text,
        'tool_calls': [{'id': 'call-transfer', 'type': 'function',
                        'function': {'name': name, 'arguments': '{}'}}],
        'provider_specific_fields': {'viventium': {'delivery_disposition': {
            'version': 1, 'audio': 'eligible', 'required': True, 'valid': True, 'source': 'model'}}}},
        'finish_reason': 'tool_calls'}], 'usage': {}, 'glasshive': {'usage_source': 'native'}}
    def sync(record):
        state['stage'] += 1
        if state['stage'] == 4:
            record['state'] = 'completed'
            record['response_json'] = json.dumps(final)
        return record
    provider._sync = sync
    provider.response_payload = lambda *_: final
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    async def collect():
        return [(state['stage'], row) async for row in provider._stream_chunks(state['record'], payload, Connected())]
    rows = asyncio.run(collect())
    deltas = [(stage, json.loads(row[6:])['choices'][0]) for stage, row in rows
              if row.startswith('data: {') and json.loads(row[6:]).get('choices')]
    marked = [(stage, choice) for stage, choice in deltas if choice['delta'].get(
        'provider_specific_fields', {}).get('viventium', {}).get('public_part_complete')]
    assert len(marked) == 1
    stage, choice = marked[0]
    assert stage == (2 if completion_only else 1)
    assert choice['finish_reason'] is None and 'tool_calls' not in choice['delta']
    assert choice['delta']['content'] == ('' if completion_only else text)
    marker = choice['delta']['provider_specific_fields']['viventium']['public_part_complete']
    assert marker == {'version': 1, 'complete': True, 'request_id': 'request-a', 'run_id': 'run-a',
                      'part_id': 'run-a:1', 'message_id': 'message-a', 'stream_id': 'stream-a'}
    assert ''.join(choice['delta'].get('content', '') for _, choice in deltas) == text
    assert [(stage, choice['delta']['tool_calls']) for stage, choice in deltas
            if choice['delta'].get('tool_calls')] == [(4, [{'index': 0,
                **final['choices'][0]['message']['tool_calls'][0]}])]
    assert len(state['public_receipts']) == 1
    assert rows[-1][1] == 'data: [DONE]\n\n'


@pytest.mark.parametrize('body', [
    '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Still incomplete',
    '{"type":"assistant_response","tool_name":null,"voice":"skip","content":"Unspoken."}',
    '{"type":"tool_call","tool_name":"transfer","voice":"eligible","content":"Handoff."}',
    '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Here [File](file.txt)"}',
    '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Here [File](artifact_sha256:abc)"}',
])
def test_public_part_completion_withholds_incomplete_skip_undeclared_tool_or_mutable_file(body):
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    profile, stdout = _public_completion_stdout([body])
    graph = {'version': 1, 'tools': [{'name': 'transfer', 'description': 'Synthetic', 'parameters': {'type': 'object', 'properties': {}}}]}
    result = _native_spoken_content(profile, stdout, graph, {'version': 1, 'audio_eligible': True})
    assert not result or not result.get('completed_parts')


def test_public_part_completion_only_delta_survives_poll_and_replay(monkeypatch):
    state, provider, payload = _spoken_family_fixture()
    state['stage'] = 0
    text = '<emphasis>First part.</emphasis>'
    prefix = '{"type":"assistant_response","tool_name":null,"voice":"eligible","content":' + json.dumps(text + ' ')
    provider.service.runtime.provider_activity_log = lambda *_: _public_completion_stdout([
        prefix[:-1] if state['stage'] == 1 else prefix + '}'])
    # Partial content ends with a safe space. Completion adds no text.
    prefix = prefix[:-1] + ' '
    provider.service.runtime.provider_activity_log = lambda *_: _public_completion_stdout([
        prefix if state['stage'] == 1 else prefix.rstrip() + '"}'])
    final = {'model': payload.model, 'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}], 'usage': {}, 'glasshive': {'usage_source': 'native'}}
    def sync(record):
        state['stage'] += 1
        if state['stage'] == 4:
            record['state'] = 'completed'
            record['response_json'] = json.dumps(final)
        return record
    provider._sync = sync
    provider._graph_transfer_control = lambda *_: None
    provider.response_payload = lambda *_: final
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    async def collect():
        return [(state['stage'], row) async for row in provider._stream_chunks(state['record'], payload, Connected())]
    rows = asyncio.run(collect())
    chunks = [(stage, json.loads(row[6:])) for stage, row in rows if row.startswith('data: {') and json.loads(row[6:]).get('choices')]
    completed = [(stage, chunk['choices'][0]['delta']) for stage, chunk in chunks if chunk['choices'][0]['delta'].get('provider_specific_fields', {}).get('viventium', {}).get('public_part_complete')]
    assert len(completed) == 1
    assert completed[0][0] == 2
    assert completed[0][1]['content'] == ''
    marker = completed[0][1]['provider_specific_fields']['viventium']['public_part_complete']
    assert marker == {'version': 1, 'complete': True, 'request_id': 'request-a', 'run_id': 'run-a',
                      'part_id': 'run-a:1', 'message_id': 'message-a', 'stream_id': 'stream-a'}
    assert ''.join(chunk['choices'][0]['delta'].get('content', '') for _, chunk in chunks) == text
    assert all(chunk['choices'][0]['finish_reason'] is None for stage, chunk in chunks if stage < 4)


def test_public_part_completion_splits_multiple_observed_parts_in_order(monkeypatch):
    state, provider, payload = _spoken_family_fixture()
    state['stage'] = 0
    texts = ['First complete.', 'Second complete.']
    bodies = [json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': text}) for text in texts]
    provider.service.runtime.provider_activity_log = lambda *_: _public_completion_stdout(bodies)
    full = '\n\n'.join(texts)
    final = {'model': payload.model, 'choices': [{'message': {'content': full}, 'finish_reason': 'stop'}], 'usage': {}, 'glasshive': {'usage_source': 'native'}}
    def sync(record):
        state['stage'] += 1
        if state['stage'] == 3:
            record['state'] = 'completed'; record['response_json'] = json.dumps(final)
        return record
    provider._sync = sync; provider._graph_transfer_control = lambda *_: None; provider.response_payload = lambda *_: final
    class Connected:
        async def is_disconnected(self): return False
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    async def collect(): return [row async for row in provider._stream_chunks(state['record'], payload, Connected())]
    deltas = [json.loads(row[6:])['choices'][0]['delta'] for row in asyncio.run(collect()) if row.startswith('data: {') and json.loads(row[6:]).get('choices')]
    marked = [delta for delta in deltas if delta.get('provider_specific_fields', {}).get('viventium', {}).get('public_part_complete')]
    assert [delta['content'] for delta in marked] == ['First complete.', '\n\nSecond complete.']
    assert [delta['provider_specific_fields']['viventium']['public_part_complete']['part_id'] for delta in marked] == ['run-a:1', 'run-a:2']
    assert ''.join(delta.get('content', '') for delta in deltas) == full


@pytest.mark.parametrize('active', [False, True])
def test_declared_dynamic_tail_refresh_reuses_separate_followup_lane(tmp_path, monkeypatch, active):
    workspace = tmp_path / 'synthetic-workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    provider, store = client.app.state.conversation_provider, client.app.state.store
    first = _payload(workspace)
    tail_a = '<viventium_feeling_state>State A.</viventium_feeling_state>'
    tail_b = '<viventium_feeling_state>State B.</viventium_feeling_state>'
    first['metadata'].update(agent_id='agent-a:phase_b:followup', developer_instruction_tail=tail_a)
    first['messages'][0]['content'] = 'Stable registered callback authority.\n\n' + tail_a
    parsed = ChatCompletionRequest.model_validate(first)
    model = GLASSHIVE_MODELS[first['model']]
    original, _ = provider._session(parsed, model, workspace, 'high', tenant_id='local')
    # Exact admission already creates an invocation-local bundle before dispatch. A later
    # refresh changes future worker defaults, never this already prepared invocation.
    first_invocation = provider._run_local_native_bundle(parsed, model, 'high')
    if active:
        monkeypatch.setattr(store, 'list_nonterminal_runs_for_worker',
                            lambda _: [{'run_id': 'synthetic-active-callback'}])
    second = ChatCompletionRequest.model_validate(first)
    second.metadata.message_id = 'message-b'
    second.metadata.idempotency_key = 'idem-b'
    second.metadata.developer_instruction_tail = tail_b
    second.messages[0].content = 'Stable registered callback authority.\n\n' + tail_b
    current, seeded = provider._session(second, model, workspace, 'high', tenant_id='local')
    assert current['session_id'] == original['session_id']
    assert current['worker_id'] == original['worker_id']
    assert seeded is False
    refreshed = json.loads(store.get_worker(current['worker_id'])['bootstrap_bundle_json'])
    assert refreshed['developer_instructions'].endswith(tail_b)
    assert refreshed['developer_instructions'].count(tail_b) == 1
    assert tail_a not in refreshed['developer_instructions']
    assert first_invocation['developer_instructions'].endswith(tail_a)
    assert tail_b not in first_invocation['developer_instructions']


@pytest.mark.parametrize('forged_digest', [False, True])
def test_declared_dynamic_tail_never_hides_changed_stable_callback_authority(tmp_path, monkeypatch, forged_digest):
    from fastapi import HTTPException
    workspace = tmp_path / 'synthetic-workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    provider, store = client.app.state.conversation_provider, client.app.state.store
    first = _payload(workspace)
    tail = '<viventium_feeling_state>State A.</viventium_feeling_state>'
    first['metadata'].update(agent_id='agent-a:phase_b:followup', developer_instruction_tail=tail)
    if forged_digest:
        first['metadata']['stable_authority_sha256'] = 'a' * 64
    first['messages'][0]['content'] = 'Original stable callback authority.\n\n' + tail
    model = GLASSHIVE_MODELS[first['model']]
    original, _ = provider._session(ChatCompletionRequest.model_validate(first), model, workspace, 'high', tenant_id='local')
    monkeypatch.setattr(store, 'list_nonterminal_runs_for_worker', lambda _: [{'run_id': 'synthetic-active'}])
    second = ChatCompletionRequest.model_validate(first)
    second.messages[0].content = 'Changed stable callback authority.\n\n' + tail
    with pytest.raises(HTTPException) as rejected:
        provider._session(second, model, workspace, 'high', tenant_id='local')
    assert rejected.value.status_code == 409
    assert rejected.value.detail['code'] == 'conversation_session_authority_conflict'
    assert store.get_provider_session_by_id(original['session_id'])['worker_id'] == original['worker_id']


@pytest.mark.parametrize('changed', ['account', 'workspace', 'access', 'policy', 'origin'])
def test_declared_dynamic_tail_preserves_existing_active_binding_guards(tmp_path, monkeypatch, changed):
    from fastapi import HTTPException
    import workers_projects_runtime.conversation_provider as module
    workspace = tmp_path / 'synthetic-workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    provider, store = client.app.state.conversation_provider, client.app.state.store
    first = _payload(workspace)
    tail = '<viventium_feeling_state>State A.</viventium_feeling_state>'
    first['metadata'].update(agent_id='agent-a:phase_b:followup', developer_instruction_tail=tail)
    first['messages'][0]['content'] = 'Stable callback authority.\n\n' + tail
    model = GLASSHIVE_MODELS[first['model']]
    original, _ = provider._session(ChatCompletionRequest.model_validate(first), model, workspace, 'high', tenant_id='local')
    monkeypatch.setattr(store, 'list_nonterminal_runs_for_worker', lambda _: [{'run_id': 'synthetic-active'}])
    second = ChatCompletionRequest.model_validate(first)
    if changed == 'account':
        second.metadata.bootstrap_bundle = {'provider_account': {'account_id': 'synthetic-other-account'}}
    elif changed == 'workspace':
        workspace = tmp_path / 'other-workspace'
        workspace.mkdir()
    elif changed == 'access':
        second.metadata.glasshive_options.access = 'full'
    elif changed == 'policy':
        monkeypatch.setattr(module, '_native_policy_sha256', lambda _: 'b' * 64)
    else:
        second.metadata.allowed_ai_origin_scope = {'project_id': 'synthetic-other-project'}
    with pytest.raises(HTTPException) as rejected:
        provider._session(second, model, workspace, 'high', tenant_id='local')
    assert rejected.value.status_code == 409
    assert store.get_provider_session_by_id(original['session_id'])['worker_id'] == original['worker_id']


@pytest.mark.parametrize("messages", MISSING_DECLARED_AUTHORITY_MESSAGES)
def test_declared_dynamic_tail_missing_authority_is_rejected_before_reuse(
    tmp_path, monkeypatch, messages,
):
    from fastapi import HTTPException
    workspace = tmp_path / 'synthetic-workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    provider = client.app.state.conversation_provider
    payload = ChatCompletionRequest.model_validate(_payload(workspace))
    payload.messages = [ChatMessage.model_validate(message) for message in messages]
    payload.metadata.developer_instruction_tail = 'Undeclared hidden authority.'
    starts = []
    monkeypatch.setattr(provider.service.runtime, 'run_task', lambda *args, **kwargs: starts.append(True))
    with pytest.raises(HTTPException) as rejected:
        provider._session(payload, GLASSHIVE_MODELS[payload.model], workspace, 'high', tenant_id='local')
    assert rejected.value.status_code == 400
    assert 'tail is absent' in rejected.value.detail
    response = client.post('/v1/chat/completions', headers=AUTH, json=payload.model_dump(mode='json'))
    assert response.status_code == 400, response.text
    assert 'tail is absent' in response.json()['error']['message']
    assert response.json()['error']['type'] == 'invalid_request_error'
    assert response.json()['error']['code'] == 'invalid_request'
    assert payload.metadata.developer_instruction_tail not in response.text
    assert starts == []
    assert client.app.state.store.list_provider_sessions(owner_id='owner-a') == []
    assert client.app.state.store.list_all_workers() == []
    assert client.app.state.store.get_provider_request(
        tenant_id='local', owner_id='owner-a',
        idempotency_key=_versioned_idempotency_key(
            payload.metadata.idempotency_key, audio_eligible=payload.metadata.audio_eligible,
        ),
    ) is None


# Private machine-state transport shares the signed zero-tool admission, never public redaction.
_PRIVATE_STATE_CONTRACT = {"version": 1, "visibility": "private_machine_state", "format": "json_object"}
_PRIVATE_STATE_PATH = "/Users/synthetic-owner/Documents/Native_Worker_Check.md"
_PRIVATE_STATE_TEXT = json.dumps({"version": 1, "summary": "Keep evidence at " + _PRIVATE_STATE_PATH,
    "pendingAsks": ["Await permission before sending"], "commitments": [], "corrections": [],
    "decisions": [], "durableIdentifiers": [_PRIVATE_STATE_PATH], "recurrenceOutcomes": [], "toolPairs": []})

class PrivateMachineStateRuntime(StubRuntime):
    def __init__(self, output=_PRIVATE_STATE_TEXT):
        super().__init__()
        self.output = output
    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        from workers_projects_runtime.profile_runtime import _redact_text
        _publish_in_process_test_start(worker)
        return _redact_text(self.output)
    def provider_activity_log(self, worker, run_id):
        return "codex-cli", "\n".join(map(json.dumps, [
            {"type": "item.completed", "item": {"type": "agent_message", "text": self.output}},
            {"type": "turn.completed"}]))


def _private_state_headers():
    return {**AUTH, "X-GlassHive-Access": "read_only", "X-Viventium-Actor-Kind": "system",
        "X-Viventium-Origin": "system", "X-GlassHive-Provider-Session-Mode": "stateless",
        **_signed_bundle_headers({"provider_capabilities": {"native_tools": False,
            "response_contract": _PRIVATE_STATE_CONTRACT}})}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("envelope", ["bare", "json_fence", "plain_fence", "uppercase_crlf"])
def test_private_machine_state_exact_terminal_survives_review_reload_and_public_run_redaction(tmp_path, monkeypatch, stream, envelope):
    output = _PRIVATE_STATE_TEXT
    if envelope == "json_fence": output = "```json\n" + output + "\n```"
    elif envelope == "plain_fence": output = "```\n" + output + "\n```"
    elif envelope == "uppercase_crlf": output = "```JSON\r\n" + output + "\r\n```"
    client = _client(tmp_path, monkeypatch, PrivateMachineStateRuntime(output))
    response = client.post("/v1/chat/completions", headers=_private_state_headers(), json=_payload(tmp_path, stream=stream))
    assert response.status_code == 200, response.text
    if stream:
        rows = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        chunks = [row["choices"][0]["delta"].get("content", "") for row in rows
            if row.get("choices") and "delta" in row["choices"][0]]
        chunks = [text for text in chunks if text]
        assert chunks == [output]  # no partial/reasoning/public redactor path
        request_id = rows[0]["id"]
    else:
        assert response.json()["choices"][0]["message"]["content"] == output
        request_id = response.json()["id"]
    record = client.app.state.store.get_provider_request(request_id)
    contract = json.loads(record["replay_decision_json"])["completion_contract_v1"]
    assert contract["response_contract"] == _PRIVATE_STATE_CONTRACT
    native_run = client.app.state.store.get_run(record["run_id"])
    native_worker = client.app.state.store.get_worker(native_run["worker_id"])
    native_bundle = json.loads(native_worker["bootstrap_bundle_json"])
    assert native_bundle["provider_capabilities"]["response_contract"] == _PRIVATE_STATE_CONTRACT
    saved = json.loads(record["response_json"])
    saved_output = saved["choices"][0]["message"]["content"]
    assert saved_output == output
    candidate = json.loads(_PRIVATE_STATE_TEXT)
    assert candidate["durableIdentifiers"] == [_PRIVATE_STATE_PATH]
    assert _PRIVATE_STATE_PATH in candidate["summary"]
    # Independent review receives the exact source reference; no model judgment is mocked.
    review_evidence = json.dumps({"sourceReference": _PRIVATE_STATE_PATH, "candidate": candidate})
    assert review_evidence.count(_PRIVATE_STATE_PATH) == 3
    run = client.app.state.store.get_run(record["run_id"])
    assert _PRIVATE_STATE_PATH not in run["output_text"]
    assert "[REDACTED_LOCAL_PATH]" in run["output_text"]
    # Reopening the provider response owner consumes the persisted contract and cached terminal,
    # without another native dispatch or altered request authority.
    reopened = ConversationProvider(client.app.state.store, client.app.state.service)
    again = reopened.response_payload(record, run, ChatCompletionRequest.model_validate(_payload(tmp_path)))
    assert again == saved
    assert _PRIVATE_STATE_PATH in json.dumps(again)
    other = client.get(f"/v1/requests/{request_id}/activity", headers={**AUTH, "X-Viventium-User-Id": "owner-b"})
    assert other.status_code == 403
    public_activity = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH)
    assert _PRIVATE_STATE_PATH not in public_activity.text


@pytest.mark.parametrize("stream", [False, True])
def test_private_user_output_and_unsigned_machine_flag_still_use_public_redaction(tmp_path, monkeypatch, stream):
    client = _client(tmp_path, monkeypatch, PrivateMachineStateRuntime())
    payload = _payload(tmp_path, stream=stream)
    payload["metadata"]["bootstrap_bundle"] = {"provider_capabilities": {"native_tools": False,
        "response_contract": _PRIVATE_STATE_CONTRACT}}
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    assert _PRIVATE_STATE_PATH not in response.text
    assert "REDACTED_LOCAL_PATH" in response.text


@pytest.mark.parametrize("change", ["interactive", "external_user", "audio", "tools", "access", "native_tools", "version", "extra"])
def test_private_machine_state_rejects_public_or_widened_authority_before_run(tmp_path, monkeypatch, change):
    client = _client(tmp_path, monkeypatch, PrivateMachineStateRuntime())
    payload, headers = _payload(tmp_path), _private_state_headers()
    caps = {"native_tools": False, "response_contract": dict(_PRIVATE_STATE_CONTRACT)}
    if change == "interactive": headers["X-Viventium-Origin"] = "interactive"
    elif change == "external_user": headers["X-Viventium-Actor-Kind"] = "external_user"
    elif change == "audio": headers["X-Viventium-Audio-Eligible"] = "true"
    elif change == "tools": payload["tools"] = [{"type": "function", "function": {"name": "ordinary_tool"}}]
    elif change == "access": headers["X-GlassHive-Access"] = "workspace"
    elif change == "native_tools": caps["native_tools"] = True
    elif change == "version": caps["response_contract"]["version"] = True
    elif change == "extra": caps["response_contract"]["extra"] = "public"
    headers.update(_signed_bundle_headers({"provider_capabilities": caps}))
    response = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert response.status_code == 403, response.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


@pytest.mark.parametrize("failure", ["untrusted", "unsigned", "signature", "expired"])
def test_private_machine_state_requires_trusted_fresh_signed_host(tmp_path, monkeypatch, failure):
    client = _scoped_client(tmp_path, monkeypatch, PrivateMachineStateRuntime(), trust_identity_headers=failure != "untrusted")
    headers = _private_state_headers()
    if failure == "unsigned": headers.pop("X-GlassHive-Bootstrap-Signature")
    elif failure == "signature": headers["X-GlassHive-Bootstrap-Signature"] = "sha256=" + "0" * 64
    elif failure == "expired": headers.update(_signed_bundle_headers({"provider_capabilities": {
        "native_tools": False, "response_contract": _PRIVATE_STATE_CONTRACT}}, timestamp=int(time.time()) - 1000))
    response = client.post("/v1/chat/completions", headers=headers, json=_payload(tmp_path))
    assert response.status_code == 403, response.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


@pytest.mark.parametrize("output", [
    "{", "[]", '{"x":1,"x":2}', "plain private prose",
    '```json\n{"x":1}\n```\n```json\n{"y":2}\n```',
    '```json\n{"x":1,"x":2}\n```',
    '```json\n{"nested":{"x":1,"x":2}}\n```',
    '```json\n[]\n```', '```json\n{"x":1}\n',
    'prefix\n```json\n{"x":1}\n```', '```json\n{"x":1}\n```\nsuffix',
    '```python\n{"x":1}\n```',
])
def test_private_machine_state_invalid_or_duplicate_object_does_not_fall_back_to_public_text(tmp_path, monkeypatch, output):
    client = _client(tmp_path, monkeypatch, PrivateMachineStateRuntime(output))
    response = client.post("/v1/chat/completions", headers=_private_state_headers(), json=_payload(tmp_path))
    assert response.status_code == 502, response.text
    assert "choices" not in response.json()
    assert "private prose" not in response.text


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code", "grok-build"])
def test_private_machine_state_native_terminal_parsers_preserve_complete_json(profile):
    if profile == "codex-cli":
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": _PRIVATE_STATE_TEXT}}, {"type": "turn.completed"}]
    elif profile == "claude-code":
        events = [{"type": "result", "result": _PRIVATE_STATE_TEXT}]
    else:
        events = [{"type": "grok.session.started", "session_id": "native-session"},
            {"type": "grok.result", "session_id": "native-session", "stop_reason": "end_turn", "output": _PRIVATE_STATE_TEXT}]
    assert _native_terminal_text(profile, "\n".join(map(json.dumps, events))) == _PRIVATE_STATE_TEXT


@pytest.mark.parametrize("mismatch", ["worker_owner", "worker_tenant", "session_owner", "run", "cancelled", "missing_terminal"])
def test_private_machine_state_canonical_owner_and_terminal_fences(mismatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    provider = object.__new__(ConversationProvider)
    record = {"request_id": "request-a", "run_id": "run-a", "session_id": "session-a", "owner_id": "owner-a", "tenant_id": "local", "state": "running"}
    run = {"run_id": "run-a", "worker_id": "worker-a", "state": "completed"}
    session = {"owner_id": "owner-a", "tenant_id": "local", "actor_kind": "system", "origin": "system"}
    worker = {"owner_id": "owner-a", "tenant_id": "local"}
    if mismatch == "worker_owner": worker["owner_id"] = "owner-b"
    elif mismatch == "worker_tenant": worker["tenant_id"] = "foreign"
    elif mismatch == "session_owner": session["owner_id"] = "owner-b"
    elif mismatch == "run": run["run_id"] = "other-run"
    elif mismatch == "cancelled": record["state"] = "cancelled"
    provider.store = SimpleNamespace(get_provider_session_by_id=lambda _: session, get_worker=lambda _: worker)
    provider._native_terminal_output_snapshot = lambda *_: None if mismatch == "missing_terminal" else _PRIVATE_STATE_TEXT
    with pytest.raises(HTTPException) as rejected:
        provider._build_canonical_response(record, run, {"response_contract": _PRIVATE_STATE_CONTRACT})
    assert rejected.value.status_code == (502 if mismatch == "missing_terminal" else 403)
    assert _PRIVATE_STATE_PATH not in str(rejected.value.detail)


def test_private_machine_state_terminal_does_not_clip_reviewed_contract_sized_output(tmp_path, monkeypatch):
    state = json.loads(_PRIVATE_STATE_TEXT)
    state["summary"] = "Faithful state. " * 5000 + _PRIVATE_STATE_PATH
    output = json.dumps(state)
    class ClippedPublicRun(PrivateMachineStateRuntime):
        def run_task(self, *args, **kwargs):
            return super().run_task(*args, **kwargs)[:64000]
    client = _client(tmp_path, monkeypatch, ClippedPublicRun(output))
    response = client.post("/v1/chat/completions", headers=_private_state_headers(), json=_payload(tmp_path))
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == output
    record = client.app.state.store.get_provider_request(response.json()["id"])
    assert len(client.app.state.store.get_run(record["run_id"])["output_text"]) == 64000


@pytest.mark.parametrize("publication", ["file", "rejection"])
@pytest.mark.parametrize("stream", [False, True])
def test_terminal_publication_missing_identity_retains_typed_failure_once(tmp_path, monkeypatch, publication, stream):
    class PublicationRuntime(StubRuntime):
        calls = 0
        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            self.calls += 1
            _publish_in_process_test_start(worker)
            return "The result is ready."

    def publication_output(self, request, run, *, output_files=None, output_rejections=None):
        if publication == "file":
            output_files.append({"filename": "drawing.svg", "mime_type": "image/svg+xml",
                "bytes": 1, "sha256": "a" * 64, "download_url": "https://files.example.test/drawing.svg"})
        else:
            output_rejections.append({"name": "drawing.svg", "code": "not_deliverable"})
        return "The result is ready."

    monkeypatch.setattr(ConversationProvider, "_conversation_output", publication_output)
    runtime = PublicationRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(tmp_path, model="codex-cli:gpt-6.1-sol", stream=stream)
    payload["metadata"]["stream_id"] = ""
    for _ in range(2):
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        if stream:
            assert response.status_code == 200
            assert '"code":"native_output_files_identity_unavailable"' in response.text
        else:
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "native_output_files_identity_unavailable"
    request = client.app.state.store.list_provider_requests_by_state({"failed"})[0]
    run = client.app.state.store.get_run(request["run_id"])
    assert run["failure_class"] == "native_output_files_identity_unavailable"
    assert run["failure_retryable"] == 0
    assert not request["fallback_state"]
    assert runtime.calls == 1
    from fastapi import HTTPException
    provider = client.app.state.conversation_provider
    with pytest.raises(HTTPException) as rejected:
        provider._build_canonical_response(request, run, provider._saved_completion_contract(request))
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "native_output_files_identity_unavailable"


@pytest.mark.parametrize("publication", ["file", "rejection"])
def test_terminal_publication_preserves_exact_existing_stream_identity(tmp_path, monkeypatch, publication):
    file = {"filename": "drawing.svg", "mime_type": "image/svg+xml", "bytes": 1,
        "sha256": "a" * 64, "download_url": "https://files.example.test/drawing.svg"}
    rejection = {"name": "drawing.svg", "code": "not_deliverable"}
    def publication_output(self, request, run, *, output_files=None, output_rejections=None):
        (output_files if publication == "file" else output_rejections).append(
            file if publication == "file" else rejection)
        return "The result is ready."
    monkeypatch.setattr(ConversationProvider, "_conversation_output", publication_output)
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path, model="codex-cli:gpt-6.1-sol")
    payload["metadata"]["stream_id"] = "phase_b:agent-a:current-group"
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    envelope = response.json()["glasshive"]["output_files"]
    assert envelope["owner_id"] == "owner-a"
    assert envelope["conversation_id"] == "conv-a"
    assert envelope["agent_id"] == "agent-a"
    assert envelope["message_id"] == "message-a"
    assert envelope["stream_id"] == "phase_b:agent-a:current-group"
    assert envelope["files"] == ([file] if publication == "file" else [])
    assert envelope.get("rejected", []) == ([rejection] if publication == "rejection" else [])


def test_native_delivery_ranges_preserve_exact_model_audio_choices_and_projection():
    from types import SimpleNamespace
    source = {'type': 'assistant_response', 'tool_name': None, 'voice': 'skip',
              'content': '<emphasis>Quiet 🟢.</emphasis>\n\nSpoken.'}
    boundary = len('<emphasis>Quiet 🟢.</emphasis>'.encode('utf-16-le')) // 2
    length = len(source['content'].encode('utf-16-le')) // 2
    choices = [{'version': 1, 'audio': value, 'required': True, 'valid': True, 'source': 'model'}
               for value in ('skip', 'eligible')]
    ranges = [{'start': 0, 'end': boundary, 'disposition': choices[0]},
              {'start': boundary, 'end': length, 'disposition': choices[1]}]
    terminal = {'type': 'grok.result', 'session_id': 'same-native', 'stop_reason': 'end_turn',
                'output': json.dumps(source), 'deliveryDispositionRanges': ranges}
    events = [{'type': 'grok.session.started', 'session_id': 'same-native'}, terminal]
    runtime = SimpleNamespace(provider_activity_log=lambda *_: ('grok-build', '\n'.join(map(json.dumps, events))))
    service = SimpleNamespace(runtime=runtime,
        render_provider_native_images=lambda _request, _run, value, **_: value.replace('<emphasis>', '').replace('</emphasis>', ''))
    provider = SimpleNamespace(service=service, store=SimpleNamespace(get_worker=lambda _: {'worker_id': 'owned'}))
    visible = 'Quiet 🟢.\n\nSpoken.'
    result = ConversationProvider._native_delivery_ranges(provider, {}, {'worker_id': 'owned', 'run_id': 'current'}, visible)
    split = len('Quiet 🟢.'.encode('utf-16-le')) // 2
    assert result == [{'start': 0, 'end': split, 'disposition': choices[0]},
                      {'start': split, 'end': len(visible.encode('utf-16-le')) // 2, 'disposition': choices[1]}]
    assert ConversationProvider._native_delivery_ranges(provider, {}, {'worker_id': 'owned', 'run_id': 'current'}, 'changed') == []
    terminal['deliveryDispositionRanges'][1]['start'] += 1
    assert ConversationProvider._native_delivery_ranges(provider, {}, {'worker_id': 'owned', 'run_id': 'current'}, visible) == []
    del terminal['deliveryDispositionRanges']
    assert ConversationProvider._native_delivery_ranges(provider, {}, {'worker_id': 'owned', 'run_id': 'current'}, visible) is None


def test_native_stream_keeps_each_public_parts_audio_choice():
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    bodies = [json.dumps({'type': 'assistant_response', 'tool_name': None,
        'voice': voice, 'content': text}) for voice, text in [('skip', 'Text only.'), ('eligible', 'Spoken.')]]
    profile, stdout = _public_completion_stdout(bodies)
    result = _native_spoken_content(profile, stdout, None, {'version': 1, 'audio_eligible': True})
    assert result['text'] == 'Text only.\n\nSpoken.'
    assert result['delivery_disposition']['audio'] == 'eligible'
    assert result['completed_parts'] == [{'part_index': 2, 'text_end': len(result['text'])}]
    assert [p['delivery_disposition']['audio'] for p in result['presentation_parts']] == ['skip', 'eligible']


def test_grok_projected_native_usage_reaches_existing_current_epoch_occupancy(tmp_path, monkeypatch):
    from workers_projects_runtime.grok_acp import _native_response_usage
    workspace = tmp_path / 'Life'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post('/v1/chat/completions', headers=AUTH, json=_payload(workspace))
    assert response.status_code == 200, response.text
    store = client.app.state.store
    request = store.get_provider_request(response.json()['id'])
    session = store.get_provider_session_by_id(request['session_id'])
    decision = json.loads(request['replay_decision_json'])
    decision['mode'] = 'delta'
    store.update_provider_request(request['request_id'], replay_decision_json=json.dumps(decision))
    projected = _native_response_usage({'input_tokens': 147824, 'output_tokens': 614,
        'cache_read_input_tokens': 1152, 'cache_creation_input_tokens': 0, 'reasoning_tokens': 591})
    usage = _native_usage('grok-build', json.dumps({'type': 'grok.result',
        'session_id': 'synthetic-native-session', 'usage': projected}))
    assert usage == {'prompt_tokens': 148976, 'completion_tokens': 614, 'total_tokens': 149590}
    provider = client.app.state.conversation_provider
    provider._record_native_usage_calibration(store.get_provider_request(request['request_id']), usage)
    manifest = json.loads(store.get_provider_session_by_id(session['session_id'])['context_manifest_json'])
    assert manifest['latest_native_prompt_tokens'] == 148976
    assert manifest['native_occupancy_scope'] == 'native_prompt_tokens'
    assert manifest['native_occupancy_request_ids'] == [request['request_id']]
    model = GLASSHIVE_MODELS['grok-build:grok-4.7-build-fast']
    projection = provider._native_occupancy_projection(model, manifest, {
        'instruction_bytes': 400, 'projected_input_tokens': 100, 'output_reserve_tokens': 100})
    assert projection['previous_native_prompt_tokens'] == 148976
    assert projection['projected_occupancy_tokens'] == 149176


@pytest.mark.parametrize("stream", [False, True])
def test_native_mcp_startup_failure_keeps_typed_code_on_each_provider_protocol(
    tmp_path, monkeypatch, stream
):
    class NativeMcpUnavailableRuntime(ProviderAuthMissingRuntime):
        def collect_completed_run(self, worker, run_id=None, instruction=None):
            return {
                "state": "failed", "output_text": "",
                "error_text": "Synthetic configured tool startup failure",
                "failure_class": "native_mcp_unavailable", "failure_retryable": 1,
                "failure_user_message": "Connected tools did not start in time. Please try again.",
            }

    client = _client(tmp_path, monkeypatch, runtime=NativeMcpUnavailableRuntime())
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(tmp_path, stream=stream))
    if stream:
        assert response.status_code == 200
        chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        errors = [chunk["error"] for chunk in chunks if "error" in chunk]
        assert errors == [{"message": "Connected tools did not start in time. Please try again.",
                           "type": "glasshive_runtime_error", "code": "native_mcp_unavailable"}]
        assert '"code":"server_error"' not in response.text
    else:
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "native_mcp_unavailable"
    requests = client.app.state.store.list_provider_requests_by_state({"failed"})
    assert len(requests) == 1
    run = client.app.state.store.get_run(requests[0]["run_id"])
    assert run["failure_class"] == "native_mcp_unavailable"
    assert not run["output_text"]


def test_terminal_stream_delivers_mixed_audio_ranges_with_the_content(tmp_path, monkeypatch):
    """A speech client must not receive quiet text before its range decisions."""
    text = "Quiet 🟢.\n\nSpoken."
    boundary = len("Quiet 🟢.".encode("utf-16-le")) // 2
    decision = lambda audio: {"version": 1, "audio": audio, "required": True,
                              "valid": True, "source": "model"}
    ranges = [{"start": 0, "end": boundary, "disposition": decision("skip")},
              {"start": boundary, "end": len(text.encode("utf-16-le")) // 2,
               "disposition": decision("eligible")}]

    class MixedRuntime(StructuredDeliveryRuntime):
        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            _publish_in_process_test_start(worker)
            return json.dumps({"type": "assistant_response", "tool_name": None,
                               "voice": "eligible", "content": text})

    monkeypatch.setattr(ConversationProvider, "_native_spoken_content_snapshot", lambda *args: None)
    monkeypatch.setattr(ConversationProvider, "_native_delivery_ranges", lambda *args: ranges)
    client = _client(tmp_path, monkeypatch, runtime=MixedRuntime())
    response = client.post("/v1/chat/completions", headers={**AUTH,
        "X-Viventium-Surface": "voice", "X-Viventium-Audio-Eligible": "true"},
        json=_payload(tmp_path, stream=True))
    assert response.status_code == 200, response.text
    chunks = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
              if line.startswith("data: {")]
    content = [choice["delta"] for chunk in chunks for choice in chunk.get("choices", [])
               if choice["delta"].get("content")]
    assert "".join(delta["content"] for delta in content) == text
    assert all(delta["provider_specific_fields"]["viventium"]["deliveryDispositionRanges"] == ranges
               and delta["provider_specific_fields"]["viventium"]["delivery_range_text"] == text
               for delta in content)
    final = chunks[-1]["choices"][0]["delta"]["provider_specific_fields"]["viventium"]
    assert final["deliveryDispositionRanges"] == ranges
    assert final["delivery_range_text"] == text


def _standalone_spoken_fixture():
    state, provider, payload = _spoken_family_fixture()
    decision = {"version": 1, "main_context_owner": "provider_legacy",
                "request_authority_sha256": "a" * 64,
                "completion_contract_v1": {"version": 1, "surface": "voice",
                    "delivery_control": {"version": 1, "audio_eligible": True}}}
    state["record"]["replay_decision_json"] = json.dumps(decision)
    state["run"].update(active_attempt_id="attempt-a")
    state["durable_attempt"] = "attempt-a"
    state["lookup_current"] = True
    payload.metadata.main_context_owner = "provider_legacy"
    payload.metadata.owner_id = "owner-a"
    payload.metadata.conversation_id = "conv-a"
    payload.metadata.agent_id = "agent-a"
    payload.metadata.message_id = "message-a"
    payload.metadata.stream_id = "stream-a"
    def lookup(request_id, run_id, attempt_id):
        if (not state["lookup_current"] or attempt_id != state["durable_attempt"]
                or request_id != state["record"]["request_id"] or run_id != state["record"]["run_id"]):
            return None
        return {**state["record"], "speech_conversation_id": "conv-a", "speech_agent_id": "agent-a"}
    provider.store.current_provider_speech_request = lookup
    return state, provider, payload


def test_standalone_spoken_projection_uses_provider_admission_without_a_core_invocation():
    state, provider, payload = _standalone_spoken_fixture()
    assert _project_spoken_family(state, provider, payload)["text"] == "Useful public words."
    assert state["record"]["native_invocation_id"] == ""
    assert state["record"]["native_body_sha256"] == ""


@pytest.mark.parametrize("case", ["unmarked", "core_owner", "core_protocol", "family_claim",
    "missing_authority", "missing_audio", "wrong_contract", "wrong_surface", "wrong_owner",
    "wrong_conversation", "wrong_agent", "wrong_message", "wrong_stream", "missing_message",
    "missing_stream", "cancelled", "wrong_run", "stale_attempt", "no_attempt", "stale_owner",
    "scheduled", "internal", "unmarked_metadata", "core_metadata", "core_protocol_metadata"])
def test_standalone_spoken_projection_rejects_unproved_or_stale_scope(case):
    state, provider, payload = _standalone_spoken_fixture()
    decision = json.loads(state["record"]["replay_decision_json"])
    if case == "unmarked": del decision["main_context_owner"]
    elif case == "core_owner": decision["main_context_owner"] = "core"
    elif case == "core_protocol": decision["main_context_protocol"] = "main_context_v1"
    elif case == "family_claim": decision["family_continuation_v1"] = True
    elif case == "missing_authority": del decision["request_authority_sha256"]
    elif case == "missing_audio": payload.metadata.audio_eligible = False
    elif case == "wrong_contract": decision["completion_contract_v1"]["version"] = 2
    elif case == "wrong_surface": decision["completion_contract_v1"]["surface"] = "web"
    elif case == "wrong_owner": payload.metadata.owner_id = "other"
    elif case == "wrong_conversation": payload.metadata.conversation_id = "other"
    elif case == "wrong_agent": payload.metadata.agent_id = "other"
    elif case == "wrong_message": payload.metadata.message_id = "other"
    elif case == "wrong_stream": payload.metadata.stream_id = "other"
    elif case == "missing_message": state["record"]["message_id"] = ""
    elif case == "missing_stream": state["record"]["stream_id"] = ""
    elif case == "cancelled": state["record"]["state"] = "cancelled"
    elif case == "wrong_run": state["run"]["run_id"] = "other"
    elif case == "stale_attempt": state["durable_attempt"] = "new-attempt"
    elif case == "no_attempt": state["run"]["active_attempt_id"] = ""
    elif case == "stale_owner": state["lookup_current"] = False
    elif case == "scheduled": payload.metadata.origin = "scheduled"
    elif case == "internal": payload.metadata.actor_kind = "internal_ai"
    elif case == "unmarked_metadata": payload.metadata.main_context_owner = ""
    elif case == "core_metadata": payload.metadata.main_context_owner = "core"
    elif case == "core_protocol_metadata": payload.metadata.main_context_protocol = "main_context_v1"
    state["record"]["replay_decision_json"] = json.dumps(decision)
    assert _project_spoken_family(state, provider, payload) is None


def test_standalone_spoken_projection_rechecks_the_exact_native_attempt_after_read():
    state, provider, payload = _standalone_spoken_fixture()
    original = provider._native_spoken_content_snapshot
    def crossed(*args):
        result = original(*args)
        state["durable_attempt"] = "replacement-attempt"
        return result
    provider._native_spoken_content_snapshot = crossed
    assert _project_spoken_family(state, provider, payload) is None


@pytest.mark.parametrize("change", [None, "request_cancelled", "restore_hold", "run_completed",
    "attempt_replaced", "worker_closed", "worker_owner", "session_owner", "run_tenant",
    "session_tenant", "worker_tenant", "newer_run", "internal_session", "scheduled_session"])
def test_current_provider_speech_request_joins_actual_durable_ownership(tmp_path, monkeypatch, change):
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime(voice="eligible"))
    response = client.post("/v1/chat/completions", headers={**AUTH,
        "X-Viventium-Surface": "voice", "X-Viventium-Audio-Eligible": "true",
        "X-Viventium-Main-Context-Owner": "provider_legacy"}, json=_payload(tmp_path))
    assert response.status_code == 200, response.text
    store = client.app.state.store
    request_id = response.json()["id"]
    record = store.get_provider_request(request_id)
    run_id = record["run_id"]
    run = store.get_run(run_id)
    with store._connect() as conn:
        conn.execute("UPDATE provider_requests SET state='running' WHERE request_id=?", (request_id,))
        conn.execute("UPDATE runs SET state='running', active_attempt_id='attempt-a' WHERE run_id=?", (run_id,))
        conn.execute("UPDATE workers SET state='running' WHERE worker_id=?", (run["worker_id"],))
        table, field, value, column, identity = {
            "request_cancelled": ("provider_requests", "state", "cancelled", "request_id", request_id),
            "restore_hold": ("provider_requests", "restore_hold", 1, "request_id", request_id),
            "run_completed": ("runs", "state", "completed", "run_id", run_id),
            "attempt_replaced": ("runs", "active_attempt_id", "new-attempt", "run_id", run_id),
            "worker_closed": ("workers", "state", "terminated", "worker_id", run["worker_id"]),
            "worker_owner": ("workers", "owner_id", "other", "worker_id", run["worker_id"]),
            "session_owner": ("provider_sessions", "owner_id", "other", "session_id", record["session_id"]),
            "run_tenant": ("runs", "tenant_id", "other", "run_id", run_id),
            "session_tenant": ("provider_sessions", "tenant_id", "other", "session_id", record["session_id"]),
            "worker_tenant": ("workers", "tenant_id", "other", "worker_id", run["worker_id"]),
            "newer_run": ("workers", "last_run_id", "replacement", "worker_id", run["worker_id"]),
            "internal_session": ("provider_sessions", "actor_kind", "system", "session_id", record["session_id"]),
            "scheduled_session": ("provider_sessions", "origin", "scheduled", "session_id", record["session_id"]),
        }.get(change, ("", "", "", "", ""))
        if table: conn.execute(f"UPDATE {table} SET {field}=? WHERE {column}=?", (value, identity))
    current = store.current_provider_speech_request(request_id, run_id, "attempt-a")
    if change is None:
        assert current["request_id"] == request_id
        assert current["speech_conversation_id"] == "conv-a"
        assert json.loads(current["replay_decision_json"])["main_context_owner"] == "provider_legacy"
    else:
        assert current is None


@pytest.mark.parametrize("declared_owner", [None, "core", "provider_legacy"])
def test_native_voice_admission_requires_explicit_provider_context_owner(tmp_path, monkeypatch, declared_owner):
    """Header-stripped Core follow-ups can redeclare audio without acquiring provider speech authority."""
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime(voice="eligible"))
    payload = _payload(tmp_path)
    payload["metadata"].update(surface="voice", input_mode="voice", audio_eligible=True)
    if declared_owner is not None:
        payload["metadata"]["main_context_owner"] = declared_owner
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    record = client.app.state.store.get_provider_request(response.json()["id"])
    decision = json.loads(record["replay_decision_json"])
    assert (decision.get("main_context_owner") == "provider_legacy") is (declared_owner == "provider_legacy")
    if declared_owner != "provider_legacy":
        provider = client.app.state.conversation_provider
        parsed = ChatCompletionRequest.model_validate(payload)
        assert not provider._provider_owned_spoken_request(record, client.app.state.store.get_run(record["run_id"]), parsed)


def test_native_voice_explicit_provider_owner_cannot_replace_core_protocol_claim(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, runtime=StructuredDeliveryRuntime(voice="eligible"))
    payload = _payload(tmp_path)
    payload["metadata"].update(surface="voice", audio_eligible=True, main_context_owner="provider_legacy",
                               main_context_protocol="main_context_v1")
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 400
    assert "Core context binding is incomplete" in response.text


@pytest.mark.parametrize("enabled", [False, True])
def test_provider_admission_timing_is_passive_private_and_ordered(tmp_path, monkeypatch, enabled):
    import workers_projects_runtime.conversation_provider as module
    client = _client(tmp_path, monkeypatch)
    provider = client.app.state.conversation_provider
    events = []
    monkeypatch.setattr(module, "native_timing_enabled", lambda: enabled)
    monkeypatch.setattr(module, "native_timing_event", lambda stage, **fields: events.append((stage, fields)))
    counts = {"session": 0, "write": 0}
    original_session = provider._session
    original_write = provider.store.create_provider_request
    def session(*args, **kwargs):
        counts["session"] += 1
        return original_session(*args, **kwargs)
    def write(*args, **kwargs):
        counts["write"] += 1
        return original_write(*args, **kwargs)
    monkeypatch.setattr(provider, "_session", session)
    monkeypatch.setattr(provider.store, "create_provider_request", write)
    payload = _payload(tmp_path)
    payload["messages"][-1]["content"] = "Synthetic private admission content"
    reply = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert reply.status_code == 200
    assert counts == {"session": 1, "write": 1}
    measured = [(stage.removeprefix("provider_admission_"), fields)
                for stage, fields in events if stage.startswith("provider_admission_")]
    if not enabled:
        assert measured == []
        return
    assert [stage for stage, _fields in measured] == [
        "route_enter", "auth_hydrate_digest", "session_lock_acquired", "retention_check",
        "model_fallback_selection", "identity_source_prepared", "session_ready",
        "context_admission_prepared", "request_record_write", "start_total", "thread_dispatch_start",
    ]
    expected_turn = module.native_timing_hash("turn", payload["metadata"]["message_id"])
    for stage, fields in measured:
        assert set(fields) == {"turnHash", "invocationHash", "status", "durationMs"}
        assert fields["turnHash"] == expected_turn
        assert len(fields["invocationHash"]) == 64
        assert fields["durationMs"] >= 0
        assert fields["status"] == ("started" if stage == "route_enter" else "completed")
    serialized = json.dumps(measured)
    for private in ["Synthetic private admission content", "owner-a", "conv-a", "message-a", "provider-test-token", str(tmp_path)]:
        assert private not in serialized


@pytest.mark.parametrize("failure", ["auth", "model"])
def test_provider_admission_timing_preserves_rejected_route_and_private_error(tmp_path, monkeypatch, failure):
    import workers_projects_runtime.conversation_provider as module
    client = _client(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(module, "native_timing_enabled", lambda: True)
    monkeypatch.setattr(module, "native_timing_event", lambda stage, **fields: events.append((stage, fields)))
    payload = _payload(tmp_path)
    headers = AUTH
    if failure == "auth":
        payload["metadata"]["owner_id"] = "synthetic-other-owner"
    else:
        payload["model"] = "unsupported:synthetic-private-model"
    reply = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert reply.status_code == (403 if failure == "auth" else 400)
    measured = [(stage, fields) for stage, fields in events if stage.startswith("provider_admission_")]
    phases = dict(measured)
    rejected = "auth_hydrate_digest" if failure == "auth" else "model_fallback_selection"
    assert phases["provider_admission_" + rejected]["status"] == "failed"
    assert "provider_admission_request_record_write" not in phases
    assert not client.app.state.store.list_provider_sessions(owner_id="owner-a")
    serialized = json.dumps(measured)
    assert "synthetic-rejected-token" not in serialized
    assert "synthetic-private-model" not in serialized


def test_provider_admission_timing_disabled_has_no_clock_hash_or_event_side_effect(monkeypatch):
    import workers_projects_runtime.conversation_provider as module
    monkeypatch.setattr(module, "native_timing_enabled", lambda: False)
    def unexpected(*_args, **_kwargs):
        pytest.fail("Disabled admission observations must not read clocks or hash identifiers")
    monkeypatch.setattr(module.time, "monotonic_ns", unexpected)
    monkeypatch.setattr(module, "native_timing_hash", unexpected)
    monkeypatch.setattr(module, "native_timing_event", unexpected)
    payload = ChatCompletionRequest(model="synthetic", messages=[{"role": "user", "content": "Synthetic input"}])
    with module._provider_admission_phase(payload, "synthetic"):
        pass


def test_provider_admission_timing_preserves_exception_identity_and_failure_privacy(monkeypatch):
    import workers_projects_runtime.conversation_provider as module
    events = []
    monkeypatch.setattr(module, "native_timing_enabled", lambda: True)
    monkeypatch.setattr(module, "native_timing_event", lambda stage, **fields: events.append((stage, fields)))
    failure = RuntimeError("Synthetic private exception details")
    with pytest.raises(RuntimeError) as caught:
        with module._provider_admission_phase(ChatCompletionRequest(model="synthetic", messages=[{"role": "user", "content": "Synthetic input"}]), "synthetic"):
            raise failure
    assert caught.value is failure
    assert events[0][1]["status"] == "failed"
    assert "Synthetic private exception details" not in json.dumps(events)


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code", "direct-xai"])
def test_private_machine_state_terminal_retains_whole_object_and_never_selects_public_ack(profile):
    raw = json.dumps({"summary": "Keep the exact pending condition.", "nested": {"label": "FINAL REPORT"}})
    raw += "\n\nFINAL REPORT:\nInternal summary returned."
    if profile == "codex-cli":
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": raw}}, {"type": "turn.completed"}]
    elif profile == "claude-code":
        events = [{"type": "result", "result": raw}]
    else:
        events = [{"type": "grok.session.started", "session_id": "native-session"},
            {"type": "grok.result", "session_id": "native-session", "stop_reason": "end_turn", "output": raw}]
    stdout = "\n".join(map(json.dumps, events))
    assert _native_terminal_text(profile, stdout, response_contract=_PRIVATE_STATE_CONTRACT) == raw
    assert _native_terminal_text(profile, stdout) == "Internal summary returned."
    with pytest.raises(json.JSONDecodeError):
        json.loads(_native_terminal_text(profile, stdout, response_contract=_PRIVATE_STATE_CONTRACT))


@pytest.mark.parametrize('configured,requested,expected', [
    (None, None, None), (None, 120, 120.0), (None, 900, 900.0),
    ('300', None, 300.0), ('300', 120, 120.0), ('300', 900, 300.0),
])
def test_admitted_response_timeout_is_finite_owner_default_and_requested_minimum(monkeypatch, configured, requested, expected):
    if configured is None:
        monkeypatch.delenv('GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S', raising=False)
    else:
        monkeypatch.setenv('GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S', configured)
    provider = ConversationProvider.__new__(ConversationProvider)
    payload = ChatCompletionRequest.model_validate({'model': 'synthetic',
        'messages': [{'role': 'user', 'content': 'Synthetic'}],
        'metadata': {} if requested is None else {'response_timeout_s': requested}})
    assert provider._response_timeout_seconds(payload) == expected
    assert bool(provider._deadline_timestamp(expected)) == (expected is not None)


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan'), 0, -1])
def test_admitted_response_timeout_rejects_nonfinite_or_nonpositive_request(value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate({'model': 'synthetic',
            'messages': [{'role': 'user', 'content': 'Synthetic'}], 'metadata': {'response_timeout_s': value}})


@pytest.mark.parametrize('raw,expected', [
    ('', None), ('invalid', None), ('2026-10-15T12:00:00', None),
    ('2026-10-15T12:00:00Z', '2026-10-15T12:00:00+00:00'),
    ('2026-10-15T13:00:00+01:00', '2026-10-15T13:00:00+01:00'),
])
def test_admitted_response_header_uses_only_valid_stored_aware_deadline(raw, expected):
    from workers_projects_runtime.conversation_provider import _admitted_response_headers
    record = {'request_id': 'synthetic_request', 'response_deadline_at': raw}
    headers = _admitted_response_headers(record)
    assert headers['X-GlassHive-Request-Id'] == 'synthetic_request'
    assert headers.get('X-GlassHive-Response-Deadline-At') == expected


@pytest.mark.parametrize('endpoint', ['/v1/chat/completions', '/v1/responses'])
@pytest.mark.parametrize('stream', [False, True])
def test_successful_response_carries_exact_stored_admitted_deadline(tmp_path, monkeypatch, endpoint, stream):
    monkeypatch.setenv('GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S', '660')
    workspace = tmp_path / 'synthetic-workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = (_payload(workspace, stream=stream) if endpoint.endswith('completions') else
               {'model': 'codex-cli:gpt-5.6-sol', 'input': 'Synthetic', 'stream': stream})
    response = client.post(endpoint, headers={**AUTH,
        'X-GlassHive-Response-Deadline-At': '2099-01-01T00:00:00Z'}, json=payload)
    assert response.status_code == 200, response.text
    record = client.app.state.store.get_provider_request(response.headers['X-GlassHive-Request-Id'])
    assert record['response_timeout_s'] == 660.0
    assert response.headers['X-GlassHive-Response-Deadline-At'] == record['response_deadline_at']
    deadline = datetime.fromisoformat(record['response_deadline_at'])
    created = datetime.fromisoformat(record['created_at'])
    assert 659 <= (deadline - created).total_seconds() <= 661
    assert deadline.year != 2099  # An incoming header never authors the admission deadline.
    if stream:
        assert '[DONE]' in response.text if endpoint.endswith('completions') else 'response.completed' in response.text


def test_admission_reconnect_retains_original_absolute_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S', '300')
    workspace = tmp_path / 'synthetic-workspace'
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace, stream=True)
    payload['metadata']['response_timeout_s'] = 60
    first = client.post('/v1/chat/completions', headers=AUTH, json=payload)
    assert first.status_code == 200, first.text
    payload['metadata']['response_timeout_s'] = 120
    second = client.post('/v1/chat/completions', headers=AUTH, json=payload)
    assert second.status_code == 200, second.text
    assert first.headers['X-GlassHive-Request-Id'] == second.headers['X-GlassHive-Request-Id']
    assert first.headers['X-GlassHive-Response-Deadline-At'] == second.headers['X-GlassHive-Response-Deadline-At']


def test_default_public_successor_old_logical_start_keeps_existing_admission_semantics(tmp_path, monkeypatch):
    monkeypatch.delenv('GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S', raising=False)
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload['metadata']['response_started_at'] = (datetime.now(UTC) - timedelta(seconds=700)).isoformat()
    response = client.post('/v1/chat/completions', headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    record = client.app.state.store.get_provider_request(response.headers['X-GlassHive-Request-Id'])
    # The existing Store deadline owner starts its implicit budget at actual
    # record creation, not a newly imposed pre-admission logical-start cap.
    assert datetime.fromisoformat(record['response_deadline_at']) > datetime.now(UTC)


@pytest.mark.parametrize('stream', [False, True])
def test_signed_private_state_unset_budget_has_exact_stored_admission_header(tmp_path, monkeypatch, stream):
    monkeypatch.delenv('GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S', raising=False)
    client = _client(tmp_path, monkeypatch, PrivateMachineStateRuntime())
    response = client.post('/v1/chat/completions', headers=_private_state_headers(), json=_payload(tmp_path, stream=stream))
    assert response.status_code == 200, response.text
    record = client.app.state.store.get_provider_request(response.headers['X-GlassHive-Request-Id'])
    assert record['response_timeout_s'] == 660.0
    assert response.headers['X-GlassHive-Response-Deadline-At'] == record['response_deadline_at']
    assert datetime.fromisoformat(record['response_deadline_at']) > datetime.now(UTC)


@pytest.mark.parametrize("status, expected_code", [(401, "provider_auth_missing"), (403, "provider_access_denied")])
def test_direct_http_failure_keeps_its_class_at_public_conversation_boundary(tmp_path, status, expected_code):
    from workers_projects_runtime.direct_runtime import DirectModelRuntime
    from workers_projects_runtime.conversation_provider import _provider_failure_error
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    failure, _ = runtime._provider_failure_for_process(
        worker={"worker_id": "synthetic-http-refusal"}, run_id="synthetic-http-refusal",
        stdout=json.dumps({"type": "direct.error", "status_code": status,
            "stage": "provider_request", "error_type": "ModelHTTPError"}),
        stderr="", exit_code=1)
    assert failure.failure_class == expected_code
    assert _provider_failure_error({"failure_class": failure.failure_class}) == (
        "glasshive_runtime_error", expected_code)


@pytest.mark.parametrize("configured", [None, "32768", "65536", "524288", "99999999", "invalid"])
@pytest.mark.parametrize("window", [32768, 200000, 500000, 1050000])
def test_native_catalog_uses_model_config_minimum_calibration_floor(monkeypatch, configured, window):
    import workers_projects_runtime.conversation_provider as provider
    if configured is None:
        monkeypatch.delenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", raising=False)
    else:
        monkeypatch.setenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", configured)
    model = provider.HarnessModel(id="synthetic", display_name="Synthetic", harness_profile="synthetic",
        native_model="synthetic", effort_choices=("high",), recommended_effort="high", context_window=window)
    floor = model.api_payload(readiness={})["native_history_admission_v1"]["replay_budget_floor_bytes"]
    for observed in [1.0, 2.5, 4.0, 8.0]:
        assert floor <= provider._conversation_replay_budget_bytes(model, observed_chars_per_token=observed)
    assert floor == provider._conversation_replay_budget_bytes(model,
        observed_chars_per_token=provider.CONVERSATION_MIN_CHARS_PER_TOKEN)


def test_complete_review_source_and_discovery_share_model_headroom(monkeypatch):
    import workers_projects_runtime.conversation_provider as provider
    monkeypatch.delenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", raising=False)
    model = provider.GLASSHIVE_MODELS["codex-cli:gpt-6.1-sol"]
    # A whole source and its proposed replacement must reach the independent reviewer.
    text = json.dumps({"source": "source fact " * 11000,
                       "candidate": "retained fact " * 6500})
    instruction, decision, _ = provider._admit_conversation_history(
        [provider.ChatMessage(role="user", content=text)], start_at=0,
        turn_context="", model=model, observed_chars_per_token=1.0)
    assert text in instruction
    assert decision["omitted_message_count"] == 0
    assert decision["instruction_bytes"] > 192 * 1024
    assert decision["instruction_bytes"] <= decision["budget_bytes"]
    assert model.api_payload(readiness={})["native_history_admission_v1"]["replay_budget_floor_bytes"] == 237500


@pytest.mark.parametrize("model_id,expected", [
    ("codex-cli:gpt-6.1-sol", 237500),
    ("claude-code:claude-opus-5-5", 500000),
    ("grok-build:grok-4.7", 250000),
    ("grok-build:grok-4.7-build-fast", 250000),
])
def test_native_catalog_selected_route_floor_keeps_operator_cap(monkeypatch, model_id, expected):
    import workers_projects_runtime.conversation_provider as provider
    model = provider.GLASSHIVE_MODELS[model_id]
    monkeypatch.delenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", raising=False)
    assert model.api_payload(readiness={})["native_history_admission_v1"]["replay_budget_floor_bytes"] == expected
    monkeypatch.setenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", "65536")
    assert model.api_payload(readiness={})["native_history_admission_v1"]["replay_budget_floor_bytes"] == 65536


def test_native_catalog_allows_intact_long_carrier_but_not_a_smaller_observed_session(monkeypatch):
    import workers_projects_runtime.conversation_provider as provider
    from fastapi import HTTPException
    monkeypatch.delenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", raising=False)
    model = provider.GLASSHIVE_MODELS["codex-cli:gpt-6.1-sol"]
    # Synthetic sizes reproduce a protected summary + recent turns + required peers.
    context = "é🙂" * 25000 + "x" * 5688
    source = "source" * 8400
    assert len(context.encode()) == 155688
    message = provider.ChatMessage(role="user", content=source)
    instruction, decision, _ = provider._admit_conversation_history(
        [message], start_at=0, turn_context=context, model=model, observed_chars_per_token=1.0)
    floor = model.api_payload(readiness={})["native_history_admission_v1"]["replay_budget_floor_bytes"]
    assert 192 * 1024 < decision["instruction_bytes"] <= floor == 237500
    assert context in instruction and source in instruction
    assert message.content == source
    assert decision["omitted_message_count"] == 0
    # Discovery describes the catalog/config limit. Native session observation is final.
    with pytest.raises(HTTPException) as rejected:
        provider._admit_conversation_history([message], start_at=0, turn_context=context,
            model=model, observed_chars_per_token=1.0, context_window=200000)
    assert rejected.value.status_code == 413


@pytest.mark.parametrize("configured,window,source_bytes", [
    ("196608", 475000, 218000), (None, 32768, 20000),
    (None, 475000, 238000), ("524288", 475000, 238000),
])
def test_complete_review_source_still_honors_config_and_effective_model_limit(
    monkeypatch, configured, window, source_bytes,
):
    import workers_projects_runtime.conversation_provider as provider
    from fastapi import HTTPException
    if configured:
        monkeypatch.setenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", configured)
    else:
        monkeypatch.delenv("GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES", raising=False)
    model = provider.HarnessModel(id="synthetic", display_name="Synthetic", harness_profile="synthetic",
        native_model="synthetic", effort_choices=("high",), recommended_effort="high", context_window=window)
    with pytest.raises(HTTPException) as rejected:
        provider._admit_conversation_history(
            [provider.ChatMessage(role="user", content="x" * source_bytes)], start_at=0,
            turn_context="", model=model, observed_chars_per_token=1.0)
    assert rejected.value.status_code == 413


@pytest.mark.parametrize("window", [475000, None, 0, -1, True, "475000"])
def test_native_reported_context_window_is_structured_and_not_billing(window):
    from workers_projects_runtime.conversation_provider import _native_context_usage
    billing = {"prompt_tokens": 450706, "completion_tokens": 29, "total_tokens": 450735}
    event = {"type": "turn.completed", "context_window_tokens": window}
    context, scope = _native_context_usage("codex-cli", json.dumps(event), billing, context_window=1050000)
    assert scope == "native_prompt_tokens"
    assert "context_window_tokens" not in billing
    if type(window) is int and window > 0:
        assert context["context_window_tokens"] == window
    else:
        assert context == billing


@pytest.mark.parametrize("change", [None, "missing", "model", "epoch", "empty_epoch", "larger", "invalid"])
def test_native_pressure_uses_only_current_model_epoch_observed_window(change):
    from dataclasses import replace
    # Deliberately wider than the observation to exercise the observation fence,
    # independently of today's corrected native model catalog.
    model = replace(GLASSHIVE_MODELS["codex-cli:gpt-6.1-sol"], context_window=1050000)
    manifest = {"latest_native_prompt_tokens": 450706, "observed_chars_per_token": 1.0,
        "native_context_epoch": "epoch-a", "native_occupancy_native_context_epoch": "epoch-a",
        "native_context_window_model_id": model.id, "native_context_window_tokens": 475000}
    if change == "missing": manifest.pop("native_context_window_tokens")
    if change == "model": manifest["native_context_window_model_id"] = "different-model"
    if change == "epoch": manifest["native_occupancy_native_context_epoch"] = "older-epoch"
    if change == "empty_epoch":
        manifest["native_context_epoch"] = manifest["native_occupancy_native_context_epoch"] = ""
    if change == "larger": manifest["native_context_window_tokens"] = 2 * model.context_window
    if change == "invalid": manifest["native_context_window_tokens"] = True
    result = ConversationProvider._native_occupancy_projection(model, manifest,
        {"instruction_bytes": 152306, "output_reserve_tokens": 32768})
    assert result["projected_occupancy_tokens"] == 635780
    assert result["state"] == ("pressure" if change is None else "healthy")
    assert result["trigger_tokens"] == int((475000 if change is None else model.context_window) * .7)
    import workers_projects_runtime.conversation_provider as provider
    window, observed = provider._effective_native_context_window(model, manifest)
    assert window == result["effective_context_window_tokens"]
    assert observed == (change in (None, "larger"))
    assert provider._conversation_replay_budget_bytes(model, observed_chars_per_token=1.0,
        context_window=window) <= window * .5


def test_native_window_receipt_obeys_existing_request_order_epoch_and_model_fences(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    assert response.status_code == 200
    store = client.app.state.store
    request = store.get_provider_request(response.json()["id"])
    session = store.get_provider_session_by_id(request["session_id"])
    epoch = json.loads(session["context_manifest_json"]).get("native_context_epoch", "")
    def record(identity, timestamp, **changes):
        options = dict(request_id=identity, prompt_tokens=450706, total_tokens=450735,
            measurement_epoch=epoch, measurement_scope="native_prompt_tokens",
            measurement_created_at=timestamp, context_window_tokens=475000,
            context_window_model_id=session["model_id"])
        options.update(changes)
        store.record_provider_session_native_occupancy(session["session_id"], **options)
    record("window-current", "2990-01-01T00:00:00Z")
    record("window-current", "2991-01-01T00:00:00Z", context_window_tokens=1)
    record("window-old", "2980-01-01T00:00:00Z", context_window_tokens=2)
    record("window-wrong-epoch", "2991-01-01T00:00:00Z", measurement_epoch="old", context_window_tokens=3)
    record("window-wrong-model", "2991-01-01T00:00:00Z", context_window_model_id="other", context_window_tokens=4)
    manifest = json.loads(store.get_provider_session_by_id(session["session_id"])["context_manifest_json"])
    assert manifest["native_context_window_tokens"] == 475000
    assert manifest["native_context_window_model_id"] == session["model_id"]
    assert manifest["native_occupancy_native_context_epoch"] == epoch


@pytest.mark.parametrize("native_epoch", ["", "synthetic-current-epoch"])
def test_native_reported_window_reaches_existing_provider_occupancy_receipt(tmp_path, monkeypatch, native_epoch):
    class NativeWindowRuntime(NativeUsageRuntime):
        def provider_activity_log(self, worker, run_id):
            profile, log = super().provider_activity_log(worker, run_id)
            events = [json.loads(line) for line in log.splitlines()]
            events[-1]["context_window_tokens"] = 475000
            return profile, "\n".join(json.dumps(event) for event in events)
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=NativeWindowRuntime())
    # A native window is usable only with a matching context epoch. Initial
    # unscoped sessions still record the observation but retain the catalog fence.
    upsert = client.app.state.store.upsert_provider_session
    def scoped_session(**kwargs):
        if native_epoch:
            kwargs["context_manifest"] = {**(kwargs.get("context_manifest") or {}),
                                          "native_context_epoch": native_epoch}
        return upsert(**kwargs)
    monkeypatch.setattr(client.app.state.store, "upsert_provider_session", scoped_session)
    model = "codex-cli:gpt-6.1-sol"
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace, model=model))
    assert response.status_code == 200, response.text
    assert response.json()["usage"] == {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21}
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    manifest = json.loads(session["context_manifest_json"])
    assert manifest["native_context_window_tokens"] == 475000
    assert manifest["native_context_window_model_id"] == model
    assert manifest["native_occupancy_request_ids"] == [response.json()["id"]]
    decision = ConversationProvider._native_occupancy_projection(GLASSHIVE_MODELS[model], manifest,
        {"instruction_bytes": 400000, "projected_input_tokens": 400000, "output_reserve_tokens": 32768})
    assert decision["state"] == "pressure"
    assert manifest["native_occupancy_native_context_epoch"] == native_epoch
    assert decision["context_window_source"] == ("native_observed" if native_epoch else "model_catalog")


@pytest.mark.parametrize("boundary", ["turn.started", "turn.failed"])
def test_native_reported_context_window_never_inherits_prior_turn(boundary):
    from workers_projects_runtime.conversation_provider import _native_context_usage
    billing = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    log = "\n".join(json.dumps(event) for event in [
        {"type": "turn.completed", "context_window_tokens": 475000}, {"type": boundary}])
    context, _ = _native_context_usage("codex-cli", log, billing, context_window=1050000)
    assert context == billing


@pytest.mark.parametrize("messages", MISSING_DECLARED_AUTHORITY_MESSAGES[1:])
def test_empty_authority_without_declared_tail_keeps_only_structural_instructions(messages):
    payload = ChatCompletionRequest.model_validate({
        "model": "codex-cli:gpt-5.6-sol",
        "messages": messages,
        "metadata": {},
    })
    assert _developer_instruction_snapshot(payload) == ""
    assert _developer_instruction_snapshot(
        payload, "Signed native bootstrap.", "Current file delivery contract.",
        "Signed native bootstrap.",
    ) == "Signed native bootstrap.\n\nCurrent file delivery contract."


def test_models_readiness_query_is_explicit_typed_and_authenticated(tmp_path, monkeypatch):
    from workers_projects_runtime import conversation_provider as provider

    client = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(provider, "_harness_readiness", lambda profile: calls.append(profile) or {"status": "ready"})
    assert client.get("/v1/models?check_readiness=true").status_code == 401
    assert not calls
    default = client.get("/v1/models", headers=AUTH)
    disabled = client.get("/v1/models?check_readiness=false", headers=AUTH)
    assert default.status_code == disabled.status_code == 200
    assert default.json() == disabled.json()
    assert not calls
    checked = client.get("/v1/models?check_readiness=true", headers=AUTH)
    assert checked.status_code == 200
    profiles = {item["harness_profile"] for item in checked.json()["data"]}
    assert set(calls) == profiles
    assert len(calls) == len(profiles)
    assert all(item["readiness"]["status"] == "ready" for item in checked.json()["data"])
    calls.clear()
    assert client.get("/v1/models?check_readiness=invalid", headers=AUTH).status_code == 400
    assert not calls
