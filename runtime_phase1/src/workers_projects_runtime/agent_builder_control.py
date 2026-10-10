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
        "For type=assistant_response, put the complete user-facing answer in content.",
        "Returning type=assistant_response ends your work for this turn. First complete authorized work you can do now, or report its actual blocker; a promise is not a result. A real accepted asynchronous work receipt may be acknowledged without waiting for completion.",
    ]
    if normalized_graph:
        description_parts.extend(
            [
                (
                    "Use type=tool_call only when a listed transfer is the best next action; "
                    "the host graph will execute it with shared state."
                ),
                (
                    "When starting a consult, content may carry a concise, useful public update "
                    "about your actual next action; leave it empty when no update is useful. "
                    "The shared graph already carries the request and context."
                ),
                (
                    "Only when returning completed specialist work, put that result in content as "
                    "evidence for the receiving agent to judge from shared state, not as public speech."
                ),
            ]
        )
    if normalized_delivery:
        description_parts.extend(
            [
                (
                    "For any public content, follow the current surface and participant-role "
                    "delivery instructions. In a live call, ordinary public content from the "
                    "current speaking agent is voice=eligible unless the user explicitly requests "
                    "text-only or silent delivery. Where messaging audio is optional, set "
                    "voice=skip when audio would reduce usefulness; otherwise set voice=eligible."
                ),
                "A user request to hear, read aloud, speak, or receive audio takes precedence.",
                "Do not put transport-control tokens in content.",
                "When returning completed specialist work, set voice=skip for its receiving-agent evidence. "
                "The current speaking agent owns delivery of its public content; the receiving "
                "agent owns its later response.",
            ]
        )
    if normalized_graph:
        description_parts.append(f"Available transfers: {descriptions}")
    properties: dict[str, Any] = {
        "type": {
            "type": "string",
            "enum": type_choices,
        },
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
    # Native providers may preserve property order. Streaming still validates the
    # actual typed controls before decoding content; ordering is not authority.
    properties["content"] = {"type": "string"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "description": " ".join(description_parts),
        "properties": properties,
        "required": required,
    }



def conversation_controls_from_schema(schema: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Recover the same typed delivery/graph contract in the standalone ACP bridge."""
    if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
        return None, None
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != {"type", "tool_name", "voice", "content"}:
        return None, None
    if set(schema.get("required") or []) != set(properties):
        return None, None
    if properties.get("voice", {}).get("enum") != ["eligible", "skip"]:
        return None, None
    names = properties.get("tool_name", {}).get("enum")
    if not isinstance(names, list) or not names or names[0] is not None:
        return None, None
    graph = normalized_graph_transfer_control({"version": CONTROL_VERSION, "tools": [
        {"name": name, "description": "Transfer in the current graph."} for name in names[1:]
    ]}) if len(names) > 1 else None
    if len(names) > 1 and graph is None:
        return None, None
    return graph, messaging_delivery_control(audio_eligible=True)


def conversation_interjection_supported(schema: Any) -> bool:
    """Decline the existing strict graph-only contract this framing cannot recover."""
    if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
        return True
    properties = _mapping(schema.get("properties"))
    required = schema.get("required")
    types = _mapping(properties.get("type")).get("enum")
    return not (set(properties) == {"type", "tool_name", "content"}
                and isinstance(required, list) and all(isinstance(field, str) for field in required)
                and set(required) == set(properties)
                and isinstance(types, list) and "tool_call" in types)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate conversation control field")
        result[key] = value
    return result


def public_conversation_prefix(output: str, graph_control: Any, delivery_control: Any) -> dict[str, Any] | None:
    """Decode only a public string after every required audio/graph control.

    Content-first envelopes remain valid terminal results, but cannot stream early.
    JSON escapes and surrogate pairs stay buffered until structurally complete.
    """
    if not normalized_messaging_delivery_control(delivery_control):
        return None
    normalized_graph = normalized_graph_transfer_control(graph_control)
    allowed = {tool["name"] for tool in (normalized_graph or {}).get("tools", [])}
    source = str(output or "").lstrip()
    if not source.startswith("{"):
        return None
    decoder = json.JSONDecoder(object_pairs_hook=_unique_json_object)
    offset = 1
    controls: dict[str, Any] = {}
    while offset < len(source):
        while offset < len(source) and source[offset].isspace():
            offset += 1
        try:
            key, offset = decoder.raw_decode(source, offset)
        except (ValueError, TypeError):
            return None
        if not isinstance(key, str) or key in controls or key not in {"type", "tool_name", "voice", "content"}:
            return None
        while offset < len(source) and source[offset].isspace():
            offset += 1
        if offset >= len(source) or source[offset] != ":":
            return None
        offset += 1
        while offset < len(source) and source[offset].isspace():
            offset += 1
        if key == "content":
            if (set(controls) != {"type", "tool_name", "voice"}
                    or controls.get("voice") != "eligible"
                    or not (
                        (controls.get("type") == "assistant_response" and controls.get("tool_name") is None)
                        or (controls.get("type") == "tool_call"
                            and isinstance(controls.get("tool_name"), str)
                            and controls["tool_name"] in allowed)
                    )
                    or offset >= len(source) or source[offset] != '"'):
                return None
            start = offset
            offset += 1
            safe_end = offset
            closed = False
            while offset < len(source):
                char = source[offset]
                if char == '"':
                    closed = True
                    break
                if ord(char) < 0x20 or 0xD800 <= ord(char) <= 0xDFFF:
                    return None
                if char == "\\":
                    if offset + 1 >= len(source):
                        break
                    escape = source[offset + 1]
                    if escape == "u":
                        if offset + 6 > len(source):
                            break
                        try:
                            code = int(source[offset + 2:offset + 6], 16)
                        except ValueError:
                            return None
                        if 0xD800 <= code <= 0xDBFF:
                            if offset + 12 > len(source):
                                break
                            try:
                                low = int(source[offset + 8:offset + 12], 16)
                            except ValueError:
                                return None
                            if source[offset + 6:offset + 8] != "\\u" or not 0xDC00 <= low <= 0xDFFF:
                                return None
                            offset += 12
                        elif 0xDC00 <= code <= 0xDFFF:
                            return None
                        else:
                            offset += 6
                    elif escape in '\"\\/bfnrt':
                        offset += 2
                    else:
                        return None
                else:
                    offset += 1
                safe_end = offset
            if closed and source[offset + 1:].strip() != "}":
                return None
            try:
                content = json.loads(source[start:safe_end] + '"')
            except ValueError:
                return None
            return {"text": content, "complete": closed, "delivery_disposition": {
                "version": MESSAGING_DELIVERY_CONTROL_VERSION, "audio": "eligible",
                "required": True, "valid": True, "source": "model",
            }}
        try:
            value, offset = decoder.raw_decode(source, offset)
        except (ValueError, TypeError):
            return None
        controls[key] = value
        while offset < len(source) and source[offset].isspace():
            offset += 1
        if offset >= len(source) or source[offset] != ",":
            return None
        offset += 1
    return None


def ordered_conversation_output(parts: list[str], output: str, graph_control: Any, delivery_control: Any) -> str:
    """Keep accepted model-authored narration in the same native terminal answer."""
    if not parts:
        return output
    payload = json.loads(output, object_pairs_hook=_unique_json_object)
    decision = parse_conversation_output(output, graph_control, delivery_control)
    disposition = decision.get("delivery_disposition")
    if isinstance(disposition, dict) and disposition.get("valid") is not True:
        raise ValueError("Invalid terminal conversation delivery control")
    payload["content"] = "\n\n".join([*parts, str(payload["content"])])
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class NativeConversationParts:
    """The existing ordered public parts, framed by exact native generation metadata.

    Callers must first bind notifications to their exact active native session.
    A later generation is buffered to distinguish complete envelope duplicates;
    Silent assistant predecessors add no public narration. A declared terminal
    graph transfer remains terminal; optional context cannot replace its controls.
    """

    def __init__(self, graph_control: Any, delivery_control: Any):
        self.controls = (graph_control, delivery_control)
        self.current: list[str] = []
        self.parts: list[str] = []
        self.part_index = 1
        self.generation: tuple[str, int, int] | None = None
        self.completed = False
        self.buffered_generation = False
        self.previous_envelope: dict[str, Any] | None = None
        self.generation_base_parts = 0
        self.prefix_seen = False
        self.terminal_graph = False

    def _envelope(self) -> dict[str, Any]:
        raw = "".join(self.current)
        payload = json.loads(raw, object_pairs_hook=_unique_json_object)
        decision = parse_conversation_output(raw, *self.controls)
        if decision.get("delivery_disposition", {}).get("valid") is not True:
            raise ValueError("Invalid native generation delivery controls")
        return payload

    def graph_transfer_selected(self) -> bool:
        """A strict declared handoff closes optional current-turn interjection."""
        try:
            decision = parse_conversation_output("".join(self.current), *self.controls)
        except ValueError:
            return False
        disposition = decision.get("delivery_disposition")
        return (decision.get("type") == "tool_call"
                and (disposition is None or disposition.get("valid") is True))

    def update(self, update: Mapping[str, Any], meta: Any = None) -> None:
        kind = update.get("sessionUpdate")
        if kind == "response_completed":
            metadata = _mapping(meta)
            if self.generation and any(
                    key in metadata and metadata[key] != expected
                    for key, expected in zip(("promptId", "turnStartMs", "streamStartMs"), self.generation)):
                raise ValueError("Native completion changed its active generation identity")
            if self.buffered_generation:
                self._envelope()
            self.completed = self.generation is not None
            return
        if kind == "tool_call":
            if self.terminal_graph:
                return
            if self.graph_transfer_selected():
                self.terminal_graph = True
                return
            prefix = self.prefix()
            if prefix and prefix["text"]:
                if not prefix["complete"]:
                    raise ValueError("Native tool interrupted an incomplete public response envelope")
                if not prefix.get("duplicate"):
                    self.parts.append(prefix["text"])
            elif self.prefix_seen:
                raise ValueError("Native tool changed an accepted public response envelope")
            self.current = []
            self.prefix_seen = False
            self.buffered_generation = False
            self.part_index += 1
            return
        content = update.get("content")
        if kind != "agent_message_chunk" or not isinstance(content, Mapping) or content.get("type") != "text":
            return
        text = content.get("text")
        if not isinstance(text, str):
            raise ValueError("Malformed native message content")
        if normalized_messaging_delivery_control(self.controls[1]):
            metadata = _mapping(meta)
            keys = ("promptId", "turnStartMs", "streamStartMs")
            if any(key in metadata for key in keys):
                prompt, turn, stream = (metadata.get(key) for key in keys)
                if (not isinstance(prompt, str) or not prompt
                        or type(turn) is not int or type(stream) is not int
                        or turn < 0 or stream < turn):
                    raise ValueError("Invalid native generation identity")
                generation = (prompt, turn, stream)
                if self.generation is not None and generation != self.generation:
                    if generation[:2] != self.generation[:2] or stream <= self.generation[2]:
                        raise ValueError("Native generation changed its active prompt or turn")
                    if not self.completed:
                        raise ValueError("Native generation changed before response completion")
                    # A native tool retires its public buffer. A later completed generation
                    # keeps the same prompt fence without parsing that retired empty buffer.
                    envelope = self._envelope() if self.current else None
                    if envelope and envelope["type"] == "tool_call":
                        # The graph decision already ends this native host step.
                        # A previously queued optional interjection cannot replace
                        # its exact declared transfer or fail valid Main output.
                        self.terminal_graph = True
                        self.generation = generation
                        self.completed = False
                        return
                    if envelope and envelope["voice"] == "eligible" and envelope != self.previous_envelope:
                        self.parts.append(envelope["content"])
                    self.previous_envelope = envelope
                    self.generation_base_parts = len(self.parts)
                    self.current = []
                    self.prefix_seen = False
                    self.part_index += 1
                    self.buffered_generation = True
                    self.completed = False
                self.generation = generation
            elif self.generation is not None:
                raise ValueError("Missing native generation identity")
        if self.terminal_graph:
            return
        self.current.append(text)
        prefix = public_conversation_prefix("".join(self.current), *self.controls)
        if prefix:
            self.prefix_seen = True

    def prefix(self) -> dict[str, Any] | None:
        prefix = public_conversation_prefix("".join(self.current), *self.controls)
        if self.buffered_generation:
            if not prefix or not prefix["complete"]:
                return None
            payload = self._envelope()
            prefix = {**prefix, "duplicate": self._is_duplicate(payload)}
        return prefix

    def _is_duplicate(self, payload: dict[str, Any]) -> bool:
        return (payload == self.previous_envelope and payload["type"] == "assistant_response"
                and payload["voice"] == "eligible" and len(self.parts) == self.generation_base_parts)

    def finish(self) -> str:
        raw = "".join(self.current)
        parts = self.parts
        if self.buffered_generation:
            payload = self._envelope()
            if self._is_duplicate(payload):
                parts = parts[:-1]
        if self.prefix_seen:
            decision = parse_conversation_output(raw, *self.controls)
            if decision.get("delivery_disposition", {}).get("valid") is not True:
                raise ValueError("Invalid public response envelope")
        return ordered_conversation_output(parts, raw, *self.controls)


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
        payload = json.loads(raw_output.strip(), object_pairs_hook=_unique_json_object)
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
    if normalized_delivery and voice not in {"eligible", "skip"}:
        raise ValueError("Native harness returned an invalid graph transfer delivery control")
    result = {"type": action, "content": content, "tool_name": tool_name}
    if normalized_delivery:
        result["delivery_disposition"] = {
            "version": MESSAGING_DELIVERY_CONTROL_VERSION,
            "audio": voice, "required": True, "valid": True, "source": "model",
        }
    return result


def parse_graph_transfer_output(output: str, control: Any) -> dict[str, Any]:
    return parse_conversation_output(output, control, None)
