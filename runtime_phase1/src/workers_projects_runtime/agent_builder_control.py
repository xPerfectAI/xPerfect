from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

# Canonical LibreChat Agent Builder graph protocol prefix. This is a structural
# tool namespace, never a user-intent or agent-name routing heuristic.
LC_TRANSFER_TO_PREFIX = "lc_transfer_to_"
MAX_GRAPH_TRANSFER_TOOLS = 16
MAX_GRAPH_TRANSFER_NAME_LENGTH = 128
MAX_GRAPH_TRANSFER_DESCRIPTION_LENGTH = 1024
CONTROL_VERSION = 1
MESSAGING_DELIVERY_CONTROL_VERSION = 1

_TRANSFER_NAME = re.compile(r"^lc_transfer_to_[A-Za-z0-9_-]+$")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tool_choice_name(tool_choice: Any) -> str:
    choice = _mapping(tool_choice)
    function = _mapping(choice.get("function"))
    return str(function.get("name") or "").strip()


def _zero_input_object_schema(value: Any) -> bool:
    schema = _mapping(value)
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    return (
        schema.get("type") == "object"
        and isinstance(properties, Mapping)
        and len(properties) == 0
        and isinstance(required, list)
        and len(required) == 0
        and schema.get("additionalProperties") in (None, False)
    )


def graph_transfer_control(
    tools: Iterable[Any] | None,
    tool_choice: Any = None,
) -> dict[str, Any] | None:
    """Return a bounded request-scoped control spec for LC zero-input handoffs.

    Other OpenAI tools deliberately stay outside this bridge. They continue to use
    GlassHive's signed host-capability broker and cannot gain graph authority here.
    """

    if isinstance(tool_choice, str) and tool_choice.strip().lower() == "none":
        return None

    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_tool in tools or []:
        tool = _mapping(raw_tool)
        function = _mapping(tool.get("function"))
        name = str(function.get("name") or "").strip()
        if tool.get("type") != "function" or not name.startswith(
            LC_TRANSFER_TO_PREFIX
        ):
            continue
        if (
            len(name) > MAX_GRAPH_TRANSFER_NAME_LENGTH
            or not _TRANSFER_NAME.fullmatch(name)
        ):
            raise ValueError("Invalid Agent Builder graph transfer tool name")
        if not _zero_input_object_schema(function.get("parameters")):
            # Agent Builder may bind legacy or specialist transfers that require
            # caller-authored arguments alongside the zero-input graph transfers
            # supported by this bridge. Keep those outside the native control
            # envelope; LibreChat remains their only execution authority.
            continue
        if name in seen:
            raise ValueError("Duplicate Agent Builder graph transfer tool name")
        seen.add(name)
        description = " ".join(
            str(function.get("description") or "Transfer control in the current Agent Builder graph")
            .replace("\x00", "")
            .split()
        )[:MAX_GRAPH_TRANSFER_DESCRIPTION_LENGTH]
        selected.append({"name": name, "description": description})

    if len(selected) > MAX_GRAPH_TRANSFER_TOOLS:
        raise ValueError("Too many Agent Builder graph transfer tools")

    forced_name = _tool_choice_name(tool_choice)
    if forced_name:
        if forced_name.startswith(LC_TRANSFER_TO_PREFIX):
            selected = [tool for tool in selected if tool["name"] == forced_name]
            if not selected:
                raise ValueError("Forced Agent Builder graph transfer tool is not available")
        else:
            return None

    if not selected:
        return None
    return {"version": CONTROL_VERSION, "tools": selected}


def normalized_graph_transfer_control(value: Any) -> dict[str, Any] | None:
    control = _mapping(value)
    if control.get("version") != CONTROL_VERSION:
        return None
    tools = control.get("tools")
    if not isinstance(tools, list) or not tools or len(tools) > MAX_GRAPH_TRANSFER_TOOLS:
        return None
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_tool in tools:
        tool = _mapping(raw_tool)
        name = str(tool.get("name") or "").strip()
        if (
            not name
            or name in seen
            or len(name) > MAX_GRAPH_TRANSFER_NAME_LENGTH
            or not _TRANSFER_NAME.fullmatch(name)
        ):
            return None
        seen.add(name)
        description = " ".join(
            str(tool.get("description") or "Transfer control in the current Agent Builder graph")
            .replace("\x00", "")
            .split()
        )[:MAX_GRAPH_TRANSFER_DESCRIPTION_LENGTH]
        normalized.append({"name": name, "description": description})
    return {"version": CONTROL_VERSION, "tools": normalized}


def messaging_delivery_control(*, audio_eligible: bool) -> dict[str, Any] | None:
    if not audio_eligible:
        return None
    return {
        "version": MESSAGING_DELIVERY_CONTROL_VERSION,
        "audio_eligible": True,
    }


def normalized_messaging_delivery_control(value: Any) -> dict[str, Any] | None:
    control = _mapping(value)
    if (
        control.get("version") != MESSAGING_DELIVERY_CONTROL_VERSION
        or control.get("audio_eligible") is not True
    ):
        return None
    return {
        "version": MESSAGING_DELIVERY_CONTROL_VERSION,
        "audio_eligible": True,
    }


def _fail_closed_delivery_result(content: str, source: str) -> dict[str, Any]:
    return {
        "type": "assistant_response",
        "content": content,
        "delivery_disposition": {
            "version": MESSAGING_DELIVERY_CONTROL_VERSION,
            "audio": "skip",
            "required": True,
            "valid": False,
            "source": source,
        },
    }


def conversation_output_schema(
    graph_control: Any,
    delivery_control: Any,
) -> dict[str, Any] | None:
    normalized_graph = normalized_graph_transfer_control(graph_control)
    normalized_delivery = normalized_messaging_delivery_control(delivery_control)
    if not normalized_graph and not normalized_delivery:
        return None
    names = [tool["name"] for tool in (normalized_graph or {}).get("tools", [])]
    descriptions = "; ".join(
        f"{tool['name']}: {tool['description']}"
        for tool in (normalized_graph or {}).get("tools", [])
    )
    type_choices = (
        ["assistant_response", "tool_call"]
        if normalized_graph
        else ["assistant_response"]
    )
    description_parts = [
        "Return one structured result for the current conversation turn.",
        "Put only the complete user-facing answer in content.",
    ]
    if normalized_graph:
        description_parts.extend(
            [
                (
                    "Use type=tool_call only when a listed transfer is the best next action; "
                    "the host graph will execute it with shared state."
                ),
                (
                    "When starting a consult, use empty content; the shared graph already carries "
                    "the request and context."
                ),
                (
                    "Only when returning completed specialist work, put that result in content so "
                    "the receiving agent can judge it from shared state."
                ),
                f"Available transfers: {descriptions}",
            ]
        )
    if normalized_delivery:
        description_parts.extend(
            [
                (
                    "For an assistant response, set voice=skip when optional audio would reduce "
                    "usefulness or the user requires text-only delivery; otherwise set "
                    "voice=eligible."
                ),
                "A user request to hear, read aloud, speak, or receive audio takes precedence.",
                "Do not put transport-control tokens in content.",
                "For a tool call, set voice=eligible; the final speaking agent owns delivery.",
            ]
        )
    properties: dict[str, Any] = {
        "type": {
            "type": "string",
            "enum": type_choices,
        },
        "content": {"type": "string"},
        "tool_name": {
            "type": ["string", "null"],
            "enum": [None, *names],
        },
    }
    required = ["type", "content", "tool_name"]
    if normalized_delivery:
        properties["voice"] = {
            "type": "string",
            "enum": ["eligible", "skip"],
        }
        required.append("voice")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "description": " ".join(description_parts),
        "properties": properties,
        "required": required,
    }


def graph_transfer_output_schema(control: Any) -> dict[str, Any] | None:
    return conversation_output_schema(control, None)


def parse_conversation_output(
    output: str,
    graph_control: Any,
    delivery_control: Any,
) -> dict[str, Any]:
    normalized_graph = normalized_graph_transfer_control(graph_control)
    normalized_delivery = normalized_messaging_delivery_control(delivery_control)
    if not normalized_graph and not normalized_delivery:
        return {"type": "assistant_response", "content": str(output or "")}
    raw_output = str(output or "")
    try:
        payload = json.loads(raw_output.strip())
    except json.JSONDecodeError as exc:
        if normalized_delivery:
            visible_fallback = raw_output
            stripped_output = raw_output.lstrip()
            fenced_control = False
            if stripped_output.startswith("```"):
                fence_lines = stripped_output.splitlines()
                if len(fence_lines) >= 3 and fence_lines[-1].strip() == "```":
                    fence_language = fence_lines[0][3:].strip().lower()
                    fence_body = "\n".join(fence_lines[1:-1]).strip()
                    try:
                        fenced_payload = json.loads(fence_body)
                    except json.JSONDecodeError:
                        fenced_payload = None
                    fenced_control = (
                        fence_language == "json"
                        and fence_body.lstrip().startswith(("{", "["))
                    ) or bool(
                        isinstance(fenced_payload, dict)
                        and {"type", "content", "tool_name"}.issubset(
                            fenced_payload
                        )
                    )
            if stripped_output.startswith(("{", "[")) or fenced_control:
                visible_fallback = (
                    "The response completed, but its delivery metadata was malformed."
                )
            return _fail_closed_delivery_result(
                visible_fallback,
                "required_malformed",
            )
        raise ValueError(
            "Native harness returned malformed conversation control output"
        ) from exc
    expected_keys = {"type", "content", "tool_name"}
    if normalized_delivery:
        expected_keys.add("voice")
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        if normalized_delivery and isinstance(payload, str):
            return _fail_closed_delivery_result(payload, "required_malformed")
        if (
            normalized_delivery
            and isinstance(payload, dict)
            and payload.get("type") == "assistant_response"
            and isinstance(payload.get("content"), str)
            and payload.get("tool_name") is None
        ):
            return _fail_closed_delivery_result(
                payload["content"],
                "required_missing" if "voice" not in payload else "required_malformed",
            )
        raise ValueError("Native harness returned an invalid conversation control envelope")
    action = payload.get("type")
    content = payload.get("content")
    tool_name = payload.get("tool_name")
    voice = payload.get("voice") if normalized_delivery else None
    if action == "assistant_response":
        if not isinstance(content, str) or tool_name is not None:
            raise ValueError("Native harness returned an invalid assistant response envelope")
        if normalized_delivery and voice not in {"eligible", "skip"}:
            return _fail_closed_delivery_result(content, "required_malformed")
        result = {"type": action, "content": content}
        if normalized_delivery:
            result["delivery_disposition"] = {
                "version": MESSAGING_DELIVERY_CONTROL_VERSION,
                "audio": voice,
                "required": True,
                "valid": True,
                "source": "model",
            }
        return result
    allowed = {tool["name"] for tool in (normalized_graph or {}).get("tools", [])}
    if action != "tool_call" or not isinstance(content, str):
        raise ValueError("Native harness returned an invalid graph transfer envelope")
    if not isinstance(tool_name, str) or tool_name not in allowed:
        raise ValueError("Native harness selected an unavailable graph transfer tool")
    # A graph transfer never delivers audio. Ignore either schema-valid value and
    # let the eventual final speaking agent own the delivery decision.
    result = {"type": action, "content": content, "tool_name": tool_name}
    return result


def parse_graph_transfer_output(output: str, control: Any) -> dict[str, Any]:
    return parse_conversation_output(output, control, None)
