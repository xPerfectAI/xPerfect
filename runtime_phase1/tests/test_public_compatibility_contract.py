from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastapi import HTTPException
from fastmcp import Client
from starlette.requests import Request

from workers_projects_runtime import conversation_provider as provider_module
from workers_projects_runtime import mcp_server as mcp_module
from workers_projects_runtime import service as service_module
from workers_projects_runtime.api import create_app
from workers_projects_runtime.bootstrap import bootstrap_bundle_for, bootstrap_profile_for
from workers_projects_runtime.conversation_provider import (
    ChatCompletionRequest,
    ConversationProvider,
    ResponsesRequest,
)
from workers_projects_runtime.mcp_server import _apply_effort_to_bundle, create_mcp_server
from workers_projects_runtime.models import RunResponse, ScheduleResponse, WorkerResponse
from workers_projects_runtime.service import WorkersProjectsService


GOLDEN_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "public_compatibility_baseline_v1.json"
)
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")
SCHEMA_CONSTRAINT_KEYS = {
    "const",
    "default",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
    "pattern",
    "type",
    "uniqueItems",
}


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_connect_skill_skips_setup_when_connected_and_never_lists_the_catalog():
    skill = (
        Path(__file__).parents[2] / "skills" / "connect-glasshive" / "SKILL.md"
    ).read_text(encoding="utf-8")
    compact_skill = " ".join(skill.split())

    assert "If xPerfect is already connected" in compact_skill
    assert "call only the one tool needed for the user's request" in compact_skill
    assert "Never enumerate or summarize the tool catalog" in compact_skill
    assert "Do not inspect config files, run shell checks" in compact_skill
    assert "only during first setup or reconnect verification" in compact_skill
    assert "persistent `scopes` value" in compact_skill
    assert "inside a xPerfect workspace" in compact_skill
    assert "Do not install that capability in the controlling AI client" in compact_skill
    assert "Set `favorite=true`" in compact_skill
    assert "repeat only `workspace_wait`" in compact_skill
    assert "pass its returned `run_id` and `worker_id`" in compact_skill
    assert "same personal AI account selected for that worker" in compact_skill
    assert "do not substitute the controlling client's own apps" in compact_skill
    assert "start a new task" not in compact_skill


def test_agent_plugins_package_the_one_canonical_connect_skill_without_a_second_mcp_contract():
    root = Path(__file__).parents[2]
    canonical_skill = root / "skills" / "connect-glasshive"
    plugin_root = root / "plugins" / "glasshive"
    packaged_skill = plugin_root / "skills" / "connect-glasshive"
    codex_manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex_marketplace = json.loads(
        (root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    claude_manifest = json.loads(
        (plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    claude_marketplace = json.loads(
        (root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )

    assert codex_manifest["name"] == "glasshive"
    assert codex_manifest["skills"] == "./skills/"
    assert "mcpServers" not in codex_manifest
    assert codex_manifest["interface"]["defaultPrompt"] == [
        "List my xPerfect workspaces",
        "Start a xPerfect workspace for this goal",
        "Show my xPerfect schedules",
    ]
    assert codex_marketplace == {
        "name": "project-glasshive",
        "interface": {"displayName": "xPerfect"},
        "plugins": [
            {
                "name": "glasshive",
                "source": {"source": "local", "path": "./plugins/glasshive"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    assert codex_marketplace["name"] != codex_manifest["name"]
    assert claude_manifest == {
        "name": "glasshive",
        "version": "0.1.1",
        "description": "Control xPerfect workspaces from Claude Code.",
        "author": {"name": "Adrien Beyk", "url": "https://github.com/AdrienBeyk"},
        "homepage": "https://github.com/xPerfectAI/xPerfect",
        "repository": "https://github.com/xPerfectAI/xPerfect",
        "license": "Apache-2.0",
        "keywords": ["xperfect", "glasshive", "workspace", "mcp", "claude"],
    }
    assert claude_marketplace == {
        "name": "glasshive",
        "owner": {"name": "Adrien Beyk"},
        "description": "The xPerfect plugin marketplace.",
        "plugins": [
            {
                "name": "glasshive",
                "source": "./plugins/glasshive",
                "description": "Control xPerfect workspaces from Claude Code.",
                "category": "productivity",
            }
        ],
    }
    assert (packaged_skill / "SKILL.md").read_bytes() == (
        canonical_skill / "SKILL.md"
    ).read_bytes()
    assert (packaged_skill / "agents" / "openai.yaml").read_bytes() == (
        canonical_skill / "agents" / "openai.yaml"
    ).read_bytes()


def _media_schemas(content: dict[str, Any] | None) -> dict[str, Any]:
    return {
        media_type: {
            key: value
            for key, value in media.items()
            if key in {"schema", "encoding"}
        }
        for media_type, media in sorted((content or {}).items())
    }


def _normalize_parameter(parameter: dict[str, Any]) -> dict[str, Any]:
    return {
        key: parameter[key]
        for key in ("name", "in", "required", "schema", "content")
        if key in parameter
    }


def _normalize_operation(operation: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if operation.get("parameters"):
        result["parameters"] = [
            _normalize_parameter(item) for item in operation["parameters"]
        ]
    if "requestBody" in operation:
        body = operation["requestBody"]
        result["requestBody"] = {
            "required": bool(body.get("required", False)),
            "content": _media_schemas(body.get("content")),
        }
    responses: dict[str, Any] = {}
    for status, response in sorted(operation.get("responses", {}).items()):
        item: dict[str, Any] = {}
        if response.get("headers"):
            item["headers"] = response["headers"]
        if response.get("content"):
            item["content"] = _media_schemas(response["content"])
        responses[status] = item
    result["responses"] = responses
    return result


def _http_contract(openapi: dict[str, Any]) -> dict[str, Any]:
    operations: dict[str, Any] = {}
    for path, path_item in sorted(openapi["paths"].items()):
        inherited = path_item.get("parameters", [])
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not operation:
                continue
            operation = dict(operation)
            operation["parameters"] = [
                *inherited,
                *operation.get("parameters", []),
            ]
            operations[f"{method.upper()} {path}"] = _normalize_operation(operation)
    return {
        "openapi": openapi.get("openapi"),
        "operations": operations,
        "schemas": openapi.get("components", {}).get("schemas", {}),
    }


def _resolve_ref(schema: Any, components: dict[str, Any]) -> Any:
    seen: set[str] = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = str(schema["$ref"])
        prefix = "#/components/schemas/"
        assert ref.startswith(prefix), f"unsupported compatibility reference: {ref}"
        assert ref not in seen, f"cyclic compatibility reference: {ref}"
        seen.add(ref)
        name = ref.removeprefix(prefix)
        assert name in components, f"missing compatibility component: {name}"
        schema = components[name]
    return schema


def _schema_matches(
    baseline: Any,
    candidate: Any,
    *,
    baseline_components: dict[str, Any],
    candidate_components: dict[str, Any],
    direction: Literal["request", "response"],
    path: str,
) -> bool:
    try:
        _assert_schema_compatible(
            baseline,
            candidate,
            baseline_components=baseline_components,
            candidate_components=candidate_components,
            direction=direction,
            path=path,
        )
    except AssertionError:
        return False
    return True


def _assert_schema_compatible(
    baseline: Any,
    candidate: Any,
    *,
    baseline_components: dict[str, Any],
    candidate_components: dict[str, Any],
    direction: Literal["request", "response"],
    path: str,
) -> None:
    baseline = _resolve_ref(baseline, baseline_components)
    candidate = _resolve_ref(candidate, candidate_components)
    assert isinstance(baseline, dict), f"{path}: invalid golden schema"
    assert isinstance(candidate, dict), f"{path}: candidate schema disappeared"

    for key in SCHEMA_CONSTRAINT_KEYS:
        if key in baseline:
            assert key in candidate, f"{path}: schema constraint {key!r} disappeared"
            assert candidate[key] == baseline[key], (
                f"{path}: schema constraint {key!r} changed from "
                f"{baseline[key]!r} to {candidate[key]!r}"
            )

    for union_key in ("anyOf", "oneOf"):
        if union_key not in baseline:
            continue
        assert union_key in candidate, f"{path}: {union_key} contract disappeared"
        baseline_options = baseline[union_key]
        candidate_options = candidate[union_key]
        if direction == "request":
            pairs = (
                (baseline_option, candidate_options)
                for baseline_option in baseline_options
            )
        else:
            pairs = (
                (candidate_option, baseline_options)
                for candidate_option in candidate_options
            )
        for index, (searched_option, available_options) in enumerate(pairs):
            if direction == "request":
                matches = (
                    _schema_matches(
                        searched_option,
                        possible,
                        baseline_components=baseline_components,
                        candidate_components=candidate_components,
                        direction=direction,
                        path=f"{path}.{union_key}[{index}]",
                    )
                    for possible in available_options
                )
            else:
                matches = (
                    _schema_matches(
                        possible,
                        searched_option,
                        baseline_components=baseline_components,
                        candidate_components=candidate_components,
                        direction=direction,
                        path=f"{path}.{union_key}[{index}]",
                    )
                    for possible in available_options
                )
            assert any(matches), (
                f"{path}: no compatible {union_key} branch for option {index}"
            )

    if "allOf" in baseline:
        assert "allOf" in candidate, f"{path}: allOf contract disappeared"
        assert len(candidate["allOf"]) >= len(baseline["allOf"]), (
            f"{path}: allOf contract lost a branch"
        )
        for index, option in enumerate(baseline["allOf"]):
            _assert_schema_compatible(
                option,
                candidate["allOf"][index],
                baseline_components=baseline_components,
                candidate_components=candidate_components,
                direction=direction,
                path=f"{path}.allOf[{index}]",
            )

    baseline_properties = baseline.get("properties", {})
    candidate_properties = candidate.get("properties", {})
    assert set(baseline_properties) <= set(candidate_properties), (
        f"{path}: fields disappeared: "
        f"{sorted(set(baseline_properties) - set(candidate_properties))}"
    )
    baseline_required = set(baseline.get("required", []))
    candidate_required = set(candidate.get("required", []))
    if direction == "request":
        assert candidate_required <= baseline_required | (
            set(candidate_properties) - set(baseline_properties)
        ), f"{path}: a legacy optional request field became required"
        added_required = candidate_required - set(baseline_properties)
        assert not added_required, (
            f"{path}: new required request fields break legacy callers: "
            f"{sorted(added_required)}"
        )
    else:
        assert baseline_required <= candidate_required, (
            f"{path}: required response fields became optional: "
            f"{sorted(baseline_required - candidate_required)}"
        )
    for name, property_schema in baseline_properties.items():
        _assert_schema_compatible(
            property_schema,
            candidate_properties[name],
            baseline_components=baseline_components,
            candidate_components=candidate_components,
            direction=direction,
            path=f"{path}.{name}",
        )

    if "items" in baseline:
        assert "items" in candidate, f"{path}: array item schema disappeared"
        _assert_schema_compatible(
            baseline["items"],
            candidate["items"],
            baseline_components=baseline_components,
            candidate_components=candidate_components,
            direction=direction,
            path=f"{path}[]",
        )

    if direction == "request" and baseline.get("additionalProperties") is not False:
        assert candidate.get("additionalProperties") is not False, (
            f"{path}: request object stopped accepting legacy additional properties"
        )


def _parameter_index(operation: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("in")), str(item.get("name"))): item
        for item in operation.get("parameters", [])
    }


def _assert_media_compatible(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    baseline_components: dict[str, Any],
    candidate_components: dict[str, Any],
    direction: Literal["request", "response"],
    path: str,
) -> None:
    assert set(baseline) <= set(candidate), (
        f"{path}: media types disappeared: {sorted(set(baseline) - set(candidate))}"
    )
    for media_type, baseline_media in baseline.items():
        candidate_media = candidate[media_type]
        if "schema" in baseline_media:
            assert "schema" in candidate_media, f"{path}: {media_type} schema disappeared"
            _assert_schema_compatible(
                baseline_media["schema"],
                candidate_media["schema"],
                baseline_components=baseline_components,
                candidate_components=candidate_components,
                direction=direction,
                path=f"{path}:{media_type}",
            )


def _assert_http_compatible(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    assert candidate["openapi"].split(".")[:2] == baseline["openapi"].split(".")[:2]
    baseline_operations = baseline["operations"]
    candidate_operations = candidate["operations"]
    assert set(baseline_operations) <= set(candidate_operations), (
        "legacy HTTP operations disappeared: "
        f"{sorted(set(baseline_operations) - set(candidate_operations))}"
    )
    baseline_components = baseline["schemas"]
    candidate_components = candidate["schemas"]
    assert set(baseline_components) <= set(candidate_components), (
        "legacy OpenAPI components disappeared: "
        f"{sorted(set(baseline_components) - set(candidate_components))}"
    )

    for operation_key, baseline_operation in baseline_operations.items():
        candidate_operation = candidate_operations[operation_key]
        baseline_parameters = _parameter_index(baseline_operation)
        candidate_parameters = _parameter_index(candidate_operation)
        assert set(baseline_parameters) <= set(candidate_parameters), (
            f"{operation_key}: parameters disappeared: "
            f"{sorted(set(baseline_parameters) - set(candidate_parameters))}"
        )
        for parameter_key, baseline_parameter in baseline_parameters.items():
            candidate_parameter = candidate_parameters[parameter_key]
            if not baseline_parameter.get("required", False):
                assert not candidate_parameter.get("required", False), (
                    f"{operation_key}: optional parameter {parameter_key} became required"
                )
            if "schema" in baseline_parameter:
                _assert_schema_compatible(
                    baseline_parameter["schema"],
                    candidate_parameter.get("schema"),
                    baseline_components=baseline_components,
                    candidate_components=candidate_components,
                    direction="request",
                    path=f"{operation_key}.parameter[{parameter_key}]",
                )
        added_required = {
            key
            for key, value in candidate_parameters.items()
            if key not in baseline_parameters and value.get("required", False)
        }
        assert not added_required, (
            f"{operation_key}: candidate added required parameters: {sorted(added_required)}"
        )

        baseline_body = baseline_operation.get("requestBody")
        candidate_body = candidate_operation.get("requestBody")
        if baseline_body:
            assert candidate_body, f"{operation_key}: request body disappeared"
            if not baseline_body.get("required", False):
                assert not candidate_body.get("required", False), (
                    f"{operation_key}: optional request body became required"
                )
            _assert_media_compatible(
                baseline_body.get("content", {}),
                candidate_body.get("content", {}),
                baseline_components=baseline_components,
                candidate_components=candidate_components,
                direction="request",
                path=f"{operation_key}.request",
            )
        elif candidate_body:
            assert not candidate_body.get("required", False), (
                f"{operation_key}: candidate added a required request body"
            )

        baseline_responses = baseline_operation["responses"]
        candidate_responses = candidate_operation["responses"]
        assert set(baseline_responses) <= set(candidate_responses), (
            f"{operation_key}: documented response statuses disappeared: "
            f"{sorted(set(baseline_responses) - set(candidate_responses))}"
        )
        for status, baseline_response in baseline_responses.items():
            candidate_response = candidate_responses[status]
            _assert_media_compatible(
                baseline_response.get("content", {}),
                candidate_response.get("content", {}),
                baseline_components=baseline_components,
                candidate_components=candidate_components,
                direction="response",
                path=f"{operation_key}.response[{status}]",
            )


def _assert_payload_additive(baseline: Any, candidate: Any, path: str) -> None:
    if isinstance(baseline, dict):
        assert isinstance(candidate, dict), f"{path}: expected object"
        assert set(baseline) <= set(candidate), (
            f"{path}: response fields disappeared: {sorted(set(baseline) - set(candidate))}"
        )
        for key, value in baseline.items():
            _assert_payload_additive(value, candidate[key], f"{path}.{key}")
        return
    if isinstance(baseline, list):
        assert isinstance(candidate, list), f"{path}: expected list"
        assert len(candidate) >= len(baseline), f"{path}: response list entries disappeared"
        for index, value in enumerate(baseline):
            _assert_payload_additive(value, candidate[index], f"{path}[{index}]")
        return
    assert candidate == baseline, f"{path}: changed from {baseline!r} to {candidate!r}"


async def _listed_mcp_tools() -> dict[str, dict[str, Any]]:
    server = create_mcp_server(api_client=object())
    async with Client(server) as client:
        return {
            tool.name: {"inputSchema": tool.inputSchema}
            for tool in await client.list_tools()
        }


class _CaptureStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None
        self.run: dict[str, Any] | None = None

    def get_run(self, run_id):
        if self.run is None or self.run.get("run_id") != run_id:
            return None
        return self.run

    def get_provider_request(self, _request_id):
        return None

    def get_provider_session_by_id(self, _session_id):
        return None

    def get_delegation_for_worker(self, *_args, **_kwargs):
        return None

    def list_run_attempts(self, _run_id):
        return []

    def upsert_callback_outbox(self, **kwargs):
        self.saved = kwargs
        return kwargs

    def update_provider_request(self, *_args, **_kwargs):
        return None

    def commit_provider_request_terminal(
        self, _request_id, *, expected_run_id, state, response_json="", **_kwargs
    ):
        assert expected_run_id == "run-synthetic"
        return {"state": state, "response_json": response_json}

    def scheduling_cortex_occurrence_for_run(self, _run_id):
        return None


class _NoopExecutor:
    def submit(self, *_args, **_kwargs):
        return None


def _candidate_callback_payload() -> tuple[dict[str, Any], WorkersProjectsService, dict[str, Any]]:
    service = WorkersProjectsService.__new__(WorkersProjectsService)
    service.store = _CaptureStore()
    service.executor = _NoopExecutor()
    service.runtime = SimpleNamespace(
        effort_projection_for_worker=lambda _worker: {
            "requested": "high",
            "effective": "high",
            "fallback_reason": "",
        }
    )
    callback_config = {
        "events_webhook_url": "https://callback.example.invalid/events",
        "hmac_secret": "synthetic-secret",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "conversation_id": "conversation-1",
        "parent_message_id": "parent-1",
        "message_id": "message-1",
        "surface": "web",
        "input_mode": "text",
        "stream_id": "stream-1",
        "voice_call_session_id": None,
        "voice_request_id": None,
        "telegram_chat_id": None,
        "telegram_user_id": None,
        "telegram_message_id": None,
    }
    service._callback_config_for = lambda _worker: callback_config
    service._signed_watch_url = (
        lambda _worker, _callbacks: "https://glasshive.example.invalid/r/ref-1"
    )
    service._callback_message_with_links = (
        lambda _worker, message, _deliverable, _callbacks, include_watch_link: message
    )
    run = {
        "run_id": "run-1",
        "project_id": "project-1",
        "worker_id": "worker-1",
        "state": "failed",
        "failure_class": "provider_rate_limited",
        "failure_retryable": True,
    }
    service.store.run = run
    service._emit_callback(
        {"project_id": "project-1", "worker_id": "worker-1"},
        "run.failed",
        run=run,
        message="Synthetic failure",
        full_message="Synthetic failure details",
        deliverable={
            "path": "reports/result.pdf",
            "signed_download_url": "https://glasshive.example.invalid/r/file-1",
        },
    )
    assert service.store.saved is not None
    payload = json.loads(service.store.saved["payload_json"])
    payload["callback_id"] = "<callback_id>"
    payload["callback_ts"] = "<timestamp>"
    return payload, service, callback_config


def _candidate_chat_completion() -> dict[str, Any]:
    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = _CaptureStore()
    provider.service = SimpleNamespace(runtime=SimpleNamespace())
    provider._conversation_output = lambda _request_record, _run: "Synthetic answer."
    provider._native_usage_snapshot = lambda _request_record, _run: {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
    request = ChatCompletionRequest(
        model="codex-cli:gpt-5.6-sol",
        messages=[{"role": "user", "content": "Synthetic question"}],
    )
    response = provider.response_payload(
        {
            "request_id": "chatcmpl-gh-synthetic",
            "session_id": "session-synthetic",
            "state": "completed",
            "response_json": "",
        },
        {"run_id": "run-synthetic"},
        request,
    )
    response["created"] = "<timestamp>"
    return response


def _candidate_responses_completion() -> dict[str, Any]:
    request = ResponsesRequest(
        model="codex-cli:gpt-5.6-sol",
        input="Synthetic question",
        instructions="Synthetic instruction",
        reasoning={"effort": "high"},
        metadata={"purpose": "compatibility"},
        max_output_tokens=100,
        temperature=0.2,
        top_p=0.9,
        service_tier="default",
    )
    response = provider_module._responses_payload(
        "chatcmpl-gh-synthetic",
        request,
        text="Synthetic answer.",
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        created_at=1700000000,
    )
    response["completed_at"] = "<timestamp>"
    return response


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


async def _candidate_chat_stream() -> list[dict[str, Any] | str]:
    class TerminalStore:
        def get_provider_request(self, request_id):
            return {
                "request_id": request_id,
                "run_id": "run-1",
                "session_id": "session-synthetic",
                "state": "completed",
                "response_json": "",
            }

        def get_run(self, _run_id):
            return {"run_id": "run-1", "state": "completed"}

        def list_provider_activity(self, _request_id):
            return []

        def get_provider_session_by_id(self, _session_id):
            return None

        def commit_provider_request_terminal(
            self, _request_id, *, expected_run_id, state, response_json="", **_kwargs
        ):
            assert expected_run_id == "run-1"
            return {"state": state, "response_json": response_json}

    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = TerminalStore()
    provider.service = SimpleNamespace(runtime=SimpleNamespace())
    provider._sync = lambda record: record
    provider._native_output_snapshot = lambda _record, _run: ""
    provider._conversation_output = lambda _record, _run: "Synthetic answer."
    provider._native_usage_snapshot = lambda _record, _run: {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
    request = ChatCompletionRequest(
        model="codex-cli:gpt-5.6-sol",
        messages=[{"role": "user", "content": "Synthetic question"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks: list[dict[str, Any] | str] = []
    async for chunk in provider._stream_chunks(
        {"request_id": "chatcmpl-gh-synthetic"},
        request,
        _ConnectedRequest(),
    ):
        data = chunk.removeprefix("data: ").strip()
        chunks.append(data if data == "[DONE]" else json.loads(data))
    return chunks


class _SyntheticResponsesProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def stream(self, *_args, **_kwargs):
        if self.fail:
            yield "data: " + json.dumps(
                {
                    "error": {
                        "code": "provider_response_failed",
                        "message": "Synthetic provider failure",
                    },
                    "choices": [],
                }
            ) + "\n\n"
        else:
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": "Synthetic answer."}}]}
            ) + "\n\n"
            yield "data: " + json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                }
            ) + "\n\n"
        yield "data: [DONE]\n\n"


async def _candidate_responses_stream(*, fail: bool = False) -> list[dict[str, Any]]:
    responses_request = ResponsesRequest(
        model="codex-cli:gpt-5.6-sol",
        input="Synthetic question",
        stream=True,
    )
    chat_request = ChatCompletionRequest(
        model="codex-cli:gpt-5.6-sol",
        messages=[{"role": "user", "content": "Synthetic question"}],
        stream=True,
    )
    events: list[dict[str, Any]] = []
    async for chunk in provider_module._responses_stream(
        _SyntheticResponsesProvider(fail=fail),
        {"request_id": "chatcmpl-gh-synthetic"},
        responses_request,
        chat_request,
        _ConnectedRequest(),
    ):
        if not chunk.startswith("event: "):
            continue
        data_line = next(
            line.removeprefix("data: ")
            for line in chunk.splitlines()
            if line.startswith("data: ")
        )
        events.append(json.loads(data_line))
    return events


def _request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (name.lower().encode("ascii"), value.encode("utf-8"))
                for name, value in headers.items()
            ],
            "server": ("glasshive.example.invalid", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_golden_is_complete_and_pinned_to_the_versioned_baseline():
    golden = _golden()

    assert golden["contract_version"] == 1
    assert golden["source"]["baseline"] == "v1"
    assert len(golden["http"]["operations"]) == 60
    assert len(golden["http"]["schemas"]) == 30
    assert len(golden["mcp"]["tools"]) == 32
    serialized = GOLDEN_PATH.read_text(encoding="utf-8").lower()
    assert "/users/" not in serialized
    assert "@" not in serialized


def test_complete_legacy_http_openapi_contract_is_additive(tmp_path):
    golden = _golden()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub")
    candidate = _http_contract(app.openapi())

    _assert_http_compatible(golden["http"], candidate)

    legacy_states = {
        "pending",
        "running",
        "queued",
        "completed",
        "failed",
        "cancelled",
    }
    occurrence_states = legacy_states | {
        "claimed",
        "skipped",
        "retryable",
        "action_required",
    }
    assert set(
        candidate["schemas"]["ScheduleResponse"]["properties"]["state"]["enum"]
    ) == legacy_states
    assert occurrence_states <= set(
        candidate["schemas"]["RecurringScheduleOccurrenceResponse"]["properties"][
            "state"
        ]["enum"]
    )


@pytest.mark.parametrize(
    ("durable_state", "public_state"),
    [
        ("claimed", "queued"),
        ("admitted", "queued"),
        ("settling", "running"),
        ("needs_input", "paused"),
    ],
)
def test_legacy_run_response_projects_internal_states(
    durable_state: str,
    public_state: str,
):
    response = RunResponse(
        run_id="run-1",
        worker_id="worker-1",
        project_id="project-1",
        instruction="Synthetic work",
        state=durable_state,
        queued_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    assert response.state == public_state


@pytest.mark.parametrize(
    ("durable_state", "public_state"),
    [("stopping", "running"), ("needs_input", "paused")],
)
def test_legacy_worker_response_projects_internal_states(
    durable_state: str,
    public_state: str,
):
    response = WorkerResponse(
        worker_id="worker-1",
        project_id="project-1",
        owner_id="owner-1",
        name="Synthetic worker",
        role="Synthetic role",
        profile="synthetic",
        backend="synthetic",
        state=durable_state,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    assert response.state == public_state


def test_legacy_schedule_response_projects_needs_input_to_failed():
    response = ScheduleResponse(
        schedule_id="schedule-1",
        worker_id="worker-1",
        project_id="project-1",
        owner_id="owner-1",
        instruction="Synthetic scheduled work",
        run_at="2026-01-01T00:00:00+00:00",
        state="needs_input",
        last_error="Synthetic authorization is required",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    assert response.state == "failed"
    assert response.last_error == "Synthetic authorization is required"


def test_http_compatibility_checker_rejects_breaking_semantic_diffs():
    baseline = _golden()["http"]

    missing_route = copy.deepcopy(baseline)
    missing_route["operations"].pop("GET /health")
    with pytest.raises(AssertionError, match="legacy HTTP operations disappeared"):
        _assert_http_compatible(baseline, missing_route)

    required_input = copy.deepcopy(baseline)
    required_input["schemas"]["CreateProjectRequest"]["required"].append(
        "default_worker_profile"
    )
    with pytest.raises(AssertionError, match="optional request field became required"):
        _assert_http_compatible(baseline, required_input)

    missing_response_field = copy.deepcopy(baseline)
    missing_response_field["schemas"]["ProjectResponse"]["properties"].pop("title")
    with pytest.raises(AssertionError, match="fields disappeared"):
        _assert_http_compatible(baseline, missing_response_field)

    expanded_legacy_enum = copy.deepcopy(baseline)
    expanded_legacy_enum["schemas"]["ScheduleResponse"]["properties"]["state"][
        "enum"
    ].append("claimed")
    with pytest.raises(AssertionError, match="schema constraint 'enum' changed"):
        _assert_http_compatible(baseline, expanded_legacy_enum)


def test_complete_legacy_mcp_tool_input_contract_is_additive():
    golden = _golden()
    baseline_tools = golden["mcp"]["tools"]
    candidate_tools = asyncio.run(_listed_mcp_tools())
    assert set(baseline_tools) <= set(candidate_tools), (
        "legacy MCP tools disappeared: "
        f"{sorted(set(baseline_tools) - set(candidate_tools))}"
    )
    for name, baseline_tool in baseline_tools.items():
        _assert_schema_compatible(
            baseline_tool["inputSchema"],
            candidate_tools[name]["inputSchema"],
            baseline_components={},
            candidate_components={},
            direction="request",
            path=f"MCP {name}",
        )

    broken_workspace_launch = copy.deepcopy(
        baseline_tools["workspace_launch"]["inputSchema"]
    )
    broken_workspace_launch["properties"].pop("description")
    with pytest.raises(AssertionError, match="fields disappeared"):
        _assert_schema_compatible(
            baseline_tools["workspace_launch"]["inputSchema"],
            broken_workspace_launch,
            baseline_components={},
            candidate_components={},
            direction="request",
            path="MCP workspace_launch",
        )


def test_bootstrap_context_callback_and_capability_broker_contracts_remain_compatible():
    golden = _golden()
    bootstrap = golden["bootstrap"]

    assert set(bootstrap["callback_required_context_fields"]) == set(
        mcp_module.CALLBACK_REQUIRED_CONTEXT_KEYS
    )
    for canonical, aliases in bootstrap["request_context_headers"].items():
        assert canonical in mcp_module.HEADER_ALIASES
        assert set(aliases) <= set(mcp_module.HEADER_ALIASES[canonical])
    broker = bootstrap["capability_broker"]
    assert mcp_module.CAPABILITY_BROKER_NAME == broker["name"]
    assert mcp_module.CAPABILITY_BROKER_CONTENT_READ_SCOPE in broker[
        "content_read_scope_aliases"
    ]

    sample = {
        field: {"synthetic": True}
        for field in bootstrap["bundle_fields"]
        if field
        not in {
            "callbacks",
            "env",
            "files",
            "claude_project_mcp",
            "codex_config_append",
            "glasshive_capability_broker",
        }
    }
    sample.update(
        {
            "callbacks": {
                "events_webhook_url": "https://callback.example.invalid/events",
                "hmac_secret": "synthetic-value",
                "user_id": "user-1",
                "conversation_id": "conversation-1",
                "parent_message_id": "parent-1",
                "message_id": "message-1",
            },
            "env": {broker["token_env"]: "synthetic-token"},
            "files": {
                "skills/example/SKILL.md": "# Synthetic example\n",
            },
            "glasshive_capability_broker": {
                "version": broker["version"],
                "name": broker["name"],
                "url": "https://broker.example.invalid/mcp",
                "scopes": {"content_read": True},
            },
            "codex_config_append": (
                "[mcp_servers.glasshive-user-capabilities]\n"
                "url = 'https://broker.example.invalid/mcp'\n"
                "bearer_token_env_var = 'GLASSHIVE_CAPABILITY_BROKER_TOKEN'\n"
            ),
        }
    )
    normalized = mcp_module._normalize_bootstrap_bundle(json.dumps(sample))
    assert normalized is not None
    assert set(sample) <= set(normalized)
    assert normalized["files"] == [
        {
            "scope": "workspace",
            "path": "skills/example/SKILL.md",
            "content": "# Synthetic example\n",
        }
    ]
    assert mcp_module._has_complete_capability_broker_bundle(normalized)
    normalized["env"] = {}
    assert not mcp_module._has_complete_capability_broker_bundle(normalized)

    worker = {
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(sample),
    }
    assert bootstrap_profile_for(worker, "claude-code") == "clean-room"
    assert bootstrap_bundle_for(worker) == sample


def test_provider_bootstrap_header_attestation_remains_wire_compatible(
    monkeypatch,
):
    attestation = _golden()["bootstrap"]["provider_header_attestation"]
    secret = "synthetic-provider-bootstrap-secret"
    bundle = {"run_mode": "conversation", "agents_md": "Synthetic instructions"}
    encoded = base64.b64encode(
        json.dumps(bundle, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    timestamp = str(int(time.time()))
    signature_message = attestation["signature_message"].replace("\\n", "\n").format(
        timestamp=timestamp,
        base64_bundle=encoded,
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        signature_message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        attestation["bundle_header"]: encoded,
        attestation["timestamp_header"]: timestamp,
        attestation["signature_header"]: attestation["signature_prefix"] + signature,
    }
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET", secret)

    assert provider_module._decode_bootstrap_bundle(_request_with_headers(headers)) == bundle
    headers[attestation["signature_header"]] = attestation["signature_prefix"] + "0" * 64
    with pytest.raises(HTTPException) as exc_info:
        provider_module._decode_bootstrap_bundle(_request_with_headers(headers))
    assert exc_info.value.status_code == 403


def test_callback_payload_and_signature_contract_are_additive():
    golden = _golden()["callbacks"]
    candidate, service, callback_config = _candidate_callback_payload()

    assert service_module.RUN_STATE_BY_EVENT == golden["event_run_states"]
    assert service_module.ACTIONABLE_CALLBACK_LINK_EVENTS == set(
        golden["actionable_link_events"]
    )
    _assert_payload_additive(golden["payload"], candidate, "callback")
    baseline_payload = golden["payload"]
    encoded = service._encode_callback_payload(baseline_payload)
    assert service._callback_headers(callback_config, baseline_payload, encoded) == golden[
        "http_headers"
    ]


def test_completion_response_fields_and_model_attestation_remain_additive(monkeypatch):
    golden = _golden()
    error_response = provider_module._openai_error(
        429,
        "Synthetic provider limit",
        "rate_limit_exceeded",
    )
    _assert_payload_additive(
        golden["completion"]["error_response"],
        json.loads(error_response.body),
        "completion_error",
    )
    _assert_payload_additive(
        golden["completion"]["chat_completion"],
        _candidate_chat_completion(),
        "chat_completion",
    )
    _assert_payload_additive(
        golden["completion"]["responses"],
        _candidate_responses_completion(),
        "responses",
    )

    chat_chunks = asyncio.run(_candidate_chat_stream())
    json_chunks = [chunk for chunk in chat_chunks if isinstance(chunk, dict)]
    assert chat_chunks[-1] == "[DONE]"
    assert {
        chunk["object"] for chunk in json_chunks
    } <= set(golden["completion"]["chat_stream_objects"])
    for chunk in json_chunks:
        assert set(golden["completion"]["chat_stream_required_fields"]["base"]) <= set(
            chunk
        )
    usage_chunk = next(chunk for chunk in json_chunks if "usage" in chunk)
    assert set(golden["completion"]["chat_stream_required_fields"]["usage"]) <= set(
        usage_chunk
    )

    response_events = [
        *asyncio.run(_candidate_responses_stream()),
        *asyncio.run(_candidate_responses_stream(fail=True)),
    ]
    by_type = {event["type"]: event for event in response_events}
    assert set(golden["completion"]["responses_stream_events"]) <= set(by_type)
    for event_type, required_fields in golden["completion"][
        "responses_stream_required_fields"
    ].items():
        assert set(required_fields) <= set(by_type[event_type]), (
            f"Responses stream event {event_type} lost required fields"
        )

    monkeypatch.setattr(
        provider_module,
        "_harness_readiness",
        lambda _profile: {
            "status": "<dynamic>",
            "binary_available": "<dynamic>",
            "authentication": "<dynamic>",
            "detail": "<dynamic>",
        },
    )
    candidate_models = {
        model_id: model.api_payload()
        for model_id, model in provider_module.GLASSHIVE_MODELS.items()
    }
    for model in candidate_models.values():
        model["created"] = "<timestamp>"
    baseline_models = golden["provider_model_attestation"]["models"]
    assert set(baseline_models) <= set(candidate_models), (
        "legacy provider model attestations disappeared: "
        f"{sorted(set(baseline_models) - set(candidate_models))}"
    )
    for model_id, baseline in baseline_models.items():
        _assert_payload_additive(
            baseline,
            candidate_models[model_id],
            f"provider_model[{model_id}]",
        )


@pytest.mark.parametrize(
    ("profile", "effort", "env_key"),
    [
        ("codex-cli", "xhigh", "WPR_CODEX_CLI_REASONING_EFFORT"),
        ("claude-code", "max", "WPR_CLAUDE_CODE_EFFORT"),
        ("claude-code", "xhigh", "WPR_CLAUDE_CODE_EFFORT"),
    ],
)
def test_supported_high_effort_values_remain_projectable(profile, effort, env_key):
    bundle = _apply_effort_to_bundle({}, profile=profile, effort=effort)

    assert bundle["env"][env_key] == effort
