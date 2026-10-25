"""Official single-agent SDK executor; the owning runtime supervises this process.

No scheduler, teams, authority store or retry loop lives here. Input is the exact
attempt projection on stdin; output uses the retained session event vocabulary.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import asdict

STARTED_AT = time.monotonic()
CURRENT_STAGE = "sdk_dependency"
CURRENT_OUTPUT_MODE = "plain"
LAST_RESPONSE_SHAPE = {}
OUTPUT_TOOL_NAME = "xperfect_conversation_result"


def emit(value):
    value.setdefault("executor_elapsed_ms", (time.monotonic() - STARTED_AT) * 1000)
    value.setdefault("observed_at_ms", time.time() * 1000)
    from pydantic_core import to_jsonable_python
    print(json.dumps(to_jsonable_python(value), ensure_ascii=False, separators=(",", ":")), flush=True)


def private_write(path, value):
    temporary = path.with_suffix(".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
    os.replace(temporary, path)


def complete_tool_history(messages):
    """Retain complete SDK protocol messages; never infer tool relevance from prose."""
    from pydantic_ai.messages import BaseToolCallPart, BaseToolReturnPart, ToolCallPart, ToolReturnPart, RetryPromptPart

    calls, results, retained = {}, set(), []
    for message in messages:
        contains_tool = False
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                if part.tool_name == OUTPUT_TOOL_NAME:
                    continue
                call_id = part.tool_call_id
                if not call_id or call_id in calls:
                    raise ValueError("Direct history has ambiguous tool call identity")
                calls[call_id] = part.tool_name
                contains_tool = True
            elif isinstance(part, (ToolReturnPart, RetryPromptPart)):
                if part.tool_name == OUTPUT_TOOL_NAME or not part.tool_call_id:
                    continue
                call_id = part.tool_call_id
                if call_id not in calls or call_id in results or calls[call_id] != part.tool_name:
                    raise ValueError("Direct history has an unmatched tool result")
                results.add(call_id)
                contains_tool = True
            elif isinstance(part, (BaseToolCallPart, BaseToolReturnPart)):
                raise ValueError("Direct history contains an unsupported native tool protocol")
        if contains_tool:
            retained.append(message)
    if set(calls) != results:
        raise ValueError("Direct history contains an incomplete tool call")
    return retained, sorted(calls)


def read_context_replay(descriptor):
    """Read the exact immutable, scope-bound journal proven at pressure admission."""
    import hashlib
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    raw = Path(descriptor["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
        raise ValueError("Direct history changed after pressure admission")
    record = json.loads(raw)
    if record.get("scope") != descriptor["scope"]:
        raise ValueError("Direct history replay does not match its provider/account scope")
    return complete_tool_history(ModelMessagesTypeAdapter.validate_json(record["messages"]))


def _image_identity(image):
    import hashlib
    from pydantic_core import to_jsonable_python
    identity = {"sha256": hashlib.sha256(image.data).hexdigest(),
                "media_type": image.media_type,
                "vendor_metadata": to_jsonable_python(image.vendor_metadata)}
    return json.dumps(identity, sort_keys=True, separators=(",", ":")), identity


def _user_images(messages):
    from pydantic_ai.messages import ModelRequest, UserPromptPart, BinaryContent
    return [item for message in messages if isinstance(message, ModelRequest)
            for part in message.parts if isinstance(part, UserPromptPart) and isinstance(part.content, list)
            for item in part.content if isinstance(item, BinaryContent) and item.is_image]


def _image_label(image, *, reference):
    _, identity = _image_identity(image)
    return json.dumps({"xperfect_image_reference" if reference else "xperfect_image":
                      {**identity, "identifier": image.identifier}}, sort_keys=True, separators=(",", ":"))


def direct_image_prompt(instruction, images, history):
    """Append each exact asset once in this already verified SDK history scope."""
    if not images:
        return instruction
    seen = {_image_identity(item)[0] for item in _user_images(history)}
    content = [instruction]
    for image in images:
        key, _ = _image_identity(image)
        if key in seen:
            content.append(_image_label(image, reference=True))
        else:
            content.append(image)
            seen.add(key)
    return content


def direct_image_request_projection(messages, available_images, *, current_run_id=None, metrics=None):
    """Keep exact image occurrences, one full payload per authorized identity, and all native parts."""
    from dataclasses import replace
    from pydantic_ai.messages import ModelRequest, UserPromptPart, BinaryContent
    available = {_image_identity(image)[0]: image for image in available_images}
    latest = {}
    for mi, message in enumerate(messages):
        if not isinstance(message, ModelRequest):
            continue
        for pi, part in enumerate(message.parts):
            if isinstance(part, UserPromptPart) and isinstance(part.content, list):
                for ci, item in enumerate(part.content):
                    if isinstance(item, BinaryContent) and item.is_image:
                        key, _ = _image_identity(item)
                        latest[key] = (mi, pi, ci)
                        available.setdefault(key, item)
    result = list(messages)
    duplicate_bytes = 0
    for mi, message in enumerate(messages):
        if not isinstance(message, ModelRequest):
            continue
        parts = list(message.parts)
        for pi, part in enumerate(message.parts):
            if not isinstance(part, UserPromptPart) or not isinstance(part.content, list):
                continue
            content = []
            changed = False
            for ci, item in enumerate(part.content):
                if not isinstance(item, BinaryContent) or not item.is_image:
                    content.append(item)
                    continue
                key, _ = _image_identity(item)
                reference = latest[key] != (mi, pi, ci)
                content.append(_image_label(item, reference=reference))
                if reference:
                    duplicate_bytes += len(item.data)
                else:
                    content.append(item)
                changed = True
            if changed:
                parts[pi] = replace(part, content=content)
        if parts != message.parts:
            result[mi] = replace(message, parts=parts)
    missing = [image for key, image in available.items() if key not in latest]
    if missing:
        # A projection/checkpoint may omit a prior full copy. Restore only actual
        # bytes already authorized in this exact SDK scope/current input. Textual
        # hash labels are never parsed as authority or file access.
        target = next(((mi, pi) for mi in range(len(result) - 1, -1, -1)
                       if isinstance(result[mi], ModelRequest)
                       and (current_run_id is None or result[mi].run_id == current_run_id)
                       for pi in range(len(result[mi].parts) - 1, -1, -1)
                       if isinstance(result[mi].parts[pi], UserPromptPart)), None)
        if target is None:
            raise ValueError("Authorized Direct images have no current user input carrier")
        mi, pi = target
        message = result[mi]
        part = message.parts[pi]
        content = [part.content] if isinstance(part.content, str) else list(part.content)
        for image in missing:
            content.extend((_image_label(image, reference=False), image))
        parts = list(message.parts)
        parts[pi] = replace(part, content=content)
        result[mi] = replace(message, parts=parts)
    if metrics is not None:
        metrics.update(unique_images=len(available), duplicate_bytes=duplicate_bytes, restored_images=len(missing))
    return result


def mcp_config(servers):
    result = {}
    for server in servers:
        name = server["name"]
        if name in result:
            raise ValueError("Duplicate configured MCP identity")
        if server.get("url"):
            result[name] = {"transport": "sse" if server.get("type") == "sse" else "http",
                            "url": server["url"], "headers": {item["name"]: item["value"] for item in server.get("headers", [])}}
        else:
            result[name] = {"command": server["command"], "args": server.get("args", []),
                            "env": {item["name"]: item["value"] for item in server.get("env", [])},
                            **({"cwd": server["cwd"]} if server.get("cwd") else {})}
    return {"mcpServers": result}


def _could_confirm_public_envelope(raw, previous, graph, delivery):
    """Compare only adjacent typed-frame representations, never ordinary replies."""
    from workers_projects_runtime.agent_builder_control import public_conversation_prefix, _unique_json_object
    # A complete silent/control-only envelope may have no public prefix. It must
    # still differ when its actual controls differ. A closing brace inside an
    # unfinished JSON string is merely pending, never a decode failure.
    if raw.rstrip().endswith("}"):
        try:
            return json.loads(raw, object_pairs_hook=_unique_json_object) == previous
        except json.JSONDecodeError:
            pass
        except ValueError:
            return False
    prefix = public_conversation_prefix(raw, graph, delivery, include_skipped=True, include_control_identity=True)
    if prefix is None:
        return True  # The existing owner cannot release this undecided header either.
    identity = {key:previous[key] for key in ("type", "tool_name", "voice")}
    return prefix["control_identity"] == identity and previous["content"].startswith(prefix["text"])


def sdk_output_schema(value):
    """Equivalent JSON Schema unions accepted by each official SDK transformer."""
    if isinstance(value, list):
        return [sdk_output_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: sdk_output_schema(item) for key, item in value.items()}
    if isinstance(result.get("type"), list):
        # JSON Schema type arrays and anyOf are equivalent here. Anthropic's
        # native SDK transformer requires the latter for nullable fields.
        members = [{"type": item} for item in result.pop("type")]
        if "anyOf" in result:
            result.setdefault("allOf", []).append({"anyOf": members})
        else:
            result["anyOf"] = members
    return result


def _without_exact_archive(text, archive):
    """Remove only an exact archive span in the retained contextual JSON carrier."""
    try:
        carrier = json.loads(text)
    except (ValueError, TypeError):
        return None
    if (not isinstance(carrier, dict) or set(carrier) != {"instruction", "context", "context_manifest"}
            or not isinstance(carrier["instruction"], str)
            or carrier["instruction"].count(archive) != 1):
        return None
    for ascii_only in (False, True):
        encoded_instruction = json.dumps(carrier["instruction"], ensure_ascii=ascii_only)
        encoded_archive = json.dumps(archive, ensure_ascii=ascii_only)[1:-1]
        if text.count(encoded_instruction) == 1 and encoded_instruction.count(encoded_archive) == 1:
            replacement = encoded_instruction.replace(encoded_archive, "", 1)
            projected = text.replace(encoded_instruction, replacement, 1)
            expected = {**carrier, "instruction": carrier["instruction"].replace(archive, "", 1)}
            return projected if json.loads(projected) == expected else None
    return None


def _core_archive_input(request):
    import hashlib
    value = request.get("core_archive")
    if not (isinstance(value, dict) and value.get("protocol") == "main_context_v1"
            and value.get("owner") == "core" and value.get("snapshot_sha256")
            and isinstance(value.get("archive"), str) and value["archive"]
            and hashlib.sha256(value["archive"].encode()).hexdigest() == value.get("archive_sha256")
            and _without_exact_archive(request["instruction"], value["archive"]) is not None):
        return None
    return value


def _core_state_input(request):
    import hashlib
    value = request.get("core_state")
    if not (isinstance(value, dict) and value.get("protocol") == "main_context_v1"
            and value.get("owner") == "core" and value.get("snapshot_sha256")
            and isinstance(value.get("context_epoch"), str)
            and len(value["context_epoch"]) == 64
            and all(c in "0123456789abcdef" for c in value["context_epoch"])
            and isinstance(value.get("blocks"), list) and len(value["blocks"]) == 1):
        return None
    blocks = []
    for block in value["blocks"]:
        if not (isinstance(block, dict) and block.get("kind") == "active_work"
                and isinstance(block.get("text"), str) and block["text"]
                and hashlib.sha256(block["text"].encode()).hexdigest() == block.get("sha256")
                and _without_exact_archive(request["instruction"], block["text"]) is not None):
            return None
        instruction = json.loads(request["instruction"])["instruction"].encode()
        data = block["text"].encode()
        blocks.append({"kind": block["kind"], "sha256": block["sha256"],
            "start_utf8": instruction.index(data), "length_utf8": len(data)})
    return {key: value[key] for key in ("protocol", "owner", "snapshot_sha256", "context_epoch")} | {"blocks": blocks}


def _core_state_request_projection(original, projected, state, metrics):
    """Keep the newest state in each consecutive, positively bound identical run."""
    from dataclasses import replace
    import hashlib
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    latest = None
    result = list(projected)
    for message_index in range(len(original) - 1, -1, -1):
        message = original[message_index]
        if not isinstance(message, ModelRequest):
            continue
        marker = (message.metadata or {}).get("xperfect_core_input")
        for index in range(len(message.parts) - 1, -1, -1):
            part = message.parts[index]
            if not isinstance(part, UserPromptPart):
                continue
            text = part.content if isinstance(part.content, str) else part.content[0] if part.content else None
            blocks = marker.get("state_blocks") if isinstance(marker, dict) else None
            if not (isinstance(marker, dict) and marker.get("protocol") == state["protocol"]
                    and marker.get("owner") == state["owner"]
                    and bool(marker.get("snapshot_sha256"))
                    and marker.get("context_epoch") == state["context_epoch"]
                    and type(marker.get("part_index")) is int
                    and marker["part_index"] == index and isinstance(text, str)
                    and hashlib.sha256(text.encode()).hexdigest() == marker.get("input_sha256")
                    and isinstance(blocks, list) and len(blocks) == 1):
                latest = None
                continue
            block = blocks[0]
            try:
                instruction = json.loads(text)["instruction"].encode()
                start, length = block["start_utf8"], block["length_utf8"]
                if (block.get("kind") != "active_work" or type(start) is not int or type(length) is not int
                        or start < 0 or length <= 0):
                    raise ValueError("Invalid state range")
                data = instruction[start:start + length]
                if len(data) != length or hashlib.sha256(data).hexdigest() != block.get("sha256"):
                    raise ValueError("State range changed")
                authored = data.decode("utf-8")
            except (TypeError, ValueError, KeyError, AttributeError):
                latest = None
                continue
            key = (marker["context_epoch"], block["kind"], block["sha256"], data)
            if latest == key:
                current = result[message_index]
                current_part = current.parts[index]
                current_text = current_part.content if isinstance(current_part.content, str) else current_part.content[0]
                replacement = _without_exact_archive(current_text, authored)
                if replacement is not None:
                    content = replacement if isinstance(current_part.content, str) else [replacement, *current_part.content[1:]]
                    parts = list(current.parts)
                    parts[index] = replace(current_part, content=content)
                    result[message_index] = replace(current, parts=parts)
                    metrics["removed_blocks"] = metrics.get("removed_blocks", 0) + 1
                    metrics["removed_data_utf8_bytes"] = metrics.get("removed_data_utf8_bytes", 0) + len(data)
                    metrics["removed_utf8_bytes"] = metrics.get("removed_utf8_bytes", 0) + len(current_text.encode()) - len(replacement.encode())
            latest = key
    return result


def _core_inline_source_projection(original, projected, state, metrics):
    """Reuse identical authorized source prefixes; retain their exact raw carriers."""
    from dataclasses import replace
    import hashlib
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    previous = {}
    result = list(projected)
    for message_index, message in enumerate(original):
        if not isinstance(message, ModelRequest):
            continue
        marker = (message.metadata or {}).get("xperfect_core_input")
        for index, part in enumerate(message.parts):
            if not isinstance(part, UserPromptPart):
                continue
            text = part.content if isinstance(part.content, str) else part.content[0] if part.content else None
            if not (isinstance(marker, dict) and marker.get("protocol") == state["protocol"]
                    and marker.get("owner") == state["owner"] and marker.get("snapshot_sha256")
                    and marker.get("context_epoch") == state["context_epoch"]
                    and type(marker.get("part_index")) is int and marker["part_index"] == index
                    and isinstance(text, str)
                    and hashlib.sha256(text.encode()).hexdigest() == marker.get("input_sha256")):
                previous = {}
                continue
            try:
                carrier = json.loads(text)
                manifest = carrier["context_manifest"]
                sources = manifest["sources"]
                if (set(carrier) != {"instruction", "context", "context_manifest"}
                        or not isinstance(carrier["context"], list) or not isinstance(sources, list)
                        or not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]
                        or type(manifest.get("inline_chars")) is not int
                        or type(manifest.get("retrievable_chars")) is not int):
                    raise ValueError("Unknown source carrier")
                by_id = {source["id"]: source for source in sources}
                if len(by_id) != len(sources):
                    raise ValueError("Ambiguous source identity")
                current = result[message_index]
                current_part = current.parts[index]
                current_text = current_part.content if isinstance(current_part.content, str) else current_part.content[0]
                outgoing = json.loads(current_text)
                inline, accepted, removed = [], {}, {}
                for source in carrier["context"]:
                    identity, data = source["id"], source["text"]
                    declared = by_id[identity]
                    digest = declared["sha256"]
                    if (not isinstance(identity, str) or not isinstance(data, str)
                            or not isinstance(digest, str) or len(digest) != 64
                            or any(c not in "0123456789abcdef" for c in digest)
                            or declared.get("status") != "available"
                            or type(declared.get("chars")) is not int
                            or declared["chars"] < len(data)
                            or type(declared.get("inline_chars")) is not int
                            or declared["inline_chars"] != len(data)
                            or declared.get("delivery") != ("inline" if len(data) == declared["chars"]
                                                          else "mixed" if data else "retrieval")):
                        raise ValueError("Unconfirmed source prefix")
                    key = (digest, data)
                    if identity in accepted:
                        raise ValueError("Ambiguous inline identity")
                    earlier = previous.get(identity)
                    duplicate = bool(data) and earlier is not None and earlier[:2] == key
                    reference = earlier[2] if duplicate else {
                        "run_id": manifest["run_id"], "source_id": identity, "sha256": digest,
                        "offset": 0, "chars": len(data),
                    }
                    accepted[identity] = (*key, reference)
                    inline.append({**source, "text": ""} if duplicate else source)
                    if duplicate:
                        removed[identity] = (data, reference)
                inline_chars = sum(len(source["text"]) for source in carrier["context"])
                if (set(accepted) != set(by_id) or manifest["inline_chars"] != inline_chars
                        or manifest["retrievable_chars"] != sum(source["chars"] for source in sources) - inline_chars):
                    raise ValueError("Unconfirmed source totals")
                if removed:
                    removed_chars = sum(len(data) for data, _ in removed.values())
                    projected_manifest = {**outgoing["context_manifest"],
                        "inline_chars": manifest["inline_chars"] - removed_chars,
                        "retrievable_chars": manifest["retrievable_chars"] + removed_chars,
                        "sources": [{**source, "inline_chars": 0, "delivery": "retrieval",
                                     "retained_inline": removed[source["id"]][1]}
                                    if source["id"] in removed else source for source in sources]}
                    replacement = json.dumps({**outgoing, "context": inline,
                                              "context_manifest": projected_manifest}, ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":"))
                    content = replacement if isinstance(current_part.content, str) else [replacement, *current_part.content[1:]]
                    parts = list(current.parts)
                    parts[index] = replace(current_part, content=content)
                    result[message_index] = replace(current, parts=parts)
                    metrics["removed_context_sources"] = metrics.get("removed_context_sources", 0) + len(removed)
                    metrics["removed_context_data_utf8_bytes"] = metrics.get("removed_context_data_utf8_bytes", 0) + sum(len(data.encode()) for data, _ in removed.values())
                previous = accepted
            except (TypeError, ValueError, KeyError, IndexError, AttributeError):
                previous = {}
    return result


def core_archive_request_projection(messages, archive, state=None, metrics=None):
    """Request-only copy: keep the raw SDK journal and all unique protocol content."""
    from dataclasses import replace
    import hashlib
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    projected = []
    retained_archive = False
    for message in messages:
        if archive is None:
            projected.append(message)
            continue
        marker = (message.metadata or {}).get("xperfect_core_input") if isinstance(message, ModelRequest) else None
        if not (isinstance(marker, dict) and marker.get("protocol") == "main_context_v1"
                and marker.get("owner") == "core" and marker.get("archive_sha256") == archive["archive_sha256"]):
            projected.append(message)
            continue
        parts = list(message.parts)
        index = marker.get("part_index")
        if not isinstance(index, int) or not 0 <= index < len(parts) or not isinstance(parts[index], UserPromptPart):
            projected.append(message)
            continue
        part = parts[index]
        text = part.content if isinstance(part.content, str) else part.content[0] if part.content else None
        if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != marker.get("input_sha256"):
            projected.append(message)
            continue
        replacement = _without_exact_archive(text, archive["archive"])
        if replacement is None:
            projected.append(message)
            continue
        if not retained_archive:
            # Keep the first trusted provider prefix byte-exact. Only subsequent
            # copies may omit this already-retained, identical archive.
            retained_archive = True
            projected.append(message)
            continue
        content = replacement if isinstance(part.content, str) else [replacement, *part.content[1:]]
        parts[index] = replace(part, content=content)
        projected.append(replace(message, parts=parts))
    if state is None:
        return projected
    metrics = metrics if metrics is not None else {}
    projected = _core_state_request_projection(messages, projected, state, metrics)
    return _core_inline_source_projection(messages, projected, state, metrics)


def core_archive_mark_input(messages, current, request, archive, state=None):
    """Copy only the exact current authored input's trusted origin metadata."""
    from dataclasses import replace
    import hashlib
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    if (archive is None and state is None) or not isinstance(current, ModelRequest):
        return messages
    for index, part in enumerate(current.parts):
        if not isinstance(part, UserPromptPart):
            continue
        text = part.content if isinstance(part.content, str) else part.content[0] if part.content else None
        if text != request["instruction"]:
            continue
        source = state or archive
        marker = {key: source[key] for key in ("protocol", "owner", "snapshot_sha256")}
        if archive is not None:
            marker["archive_sha256"] = archive["archive_sha256"]
        if state is not None:
            marker.update(context_epoch=state["context_epoch"], state_blocks=state["blocks"])
        marker.update(part_index=index, input_sha256=hashlib.sha256(text.encode()).hexdigest())
        tagged = replace(current, metadata={**(current.metadata or {}), "xperfect_core_input": marker})
        return [tagged if message is current else message for message in messages]
    return messages


def core_archive_repair_history(messages):
    """Keep trusted part origins across the SDK's application-metadata-free merge."""
    from dataclasses import replace
    import hashlib
    from pydantic_ai.messages import ModelRequest, UserPromptPart, repair_messages
    origins = {}
    for message in messages:
        marker = (message.metadata or {}).get("xperfect_core_input") if isinstance(message, ModelRequest) else None
        if not (isinstance(marker, dict) and marker.get("protocol") == "main_context_v1"
                and marker.get("owner") == "core" and marker.get("snapshot_sha256")):
            continue
        index = marker.get("part_index")
        if not isinstance(index, int) or not 0 <= index < len(message.parts):
            continue
        part = message.parts[index]
        if not isinstance(part, UserPromptPart):
            continue
        text = part.content if isinstance(part.content, str) else part.content[0] if part.content else None
        if isinstance(text, str) and hashlib.sha256(text.encode()).hexdigest() == marker.get("input_sha256"):
            origins[id(part)] = marker
    repaired = []
    for message in repair_messages(messages, repair_last_response=False):
        known = [(index, origins[id(part)]) for index, part in enumerate(message.parts) if id(part) in origins]
        if isinstance(message, ModelRequest) and len(known) == 1:
            index, marker = known[0]
            message = replace(message, metadata={**(message.metadata or {}),
                "xperfect_core_input": {**marker, "part_index": index}})
        repaired.append(message)
    return repaired


def core_archive_history_record(result, request, archive, state=None):
    """Tag the raw journal without changing a single authored/protocol part."""
    if archive is None and state is None:
        return result.all_messages()
    new_messages = result.new_messages()
    current = new_messages[0] if new_messages else None
    return core_archive_mark_input(result.all_messages(), current, request, archive, state)


async def execute(request):
    global CURRENT_STAGE, CURRENT_OUTPUT_MODE, LAST_RESPONSE_SHAPE
    CURRENT_OUTPUT_MODE, LAST_RESPONSE_SHAPE = "plain", {}
    CURRENT_STAGE = "sdk_dependency"
    # Imported once per supervised executor, never in the service's ordinary CLI path.
    from pydantic_ai import Agent, PartStartEvent, PartDeltaEvent, PartEndEvent, TextPart, TextPartDelta
    from pydantic_ai import FunctionToolCallEvent, FunctionToolResultEvent, AgentRunResultEvent, ToolCallPart, ToolCallPartDelta
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ToolReturnPart, BinaryContent

    CURRENT_STAGE = "configuration"
    provider, model = request["provider"], request["model"]
    credential_name = {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    credential = os.environ.get(credential_name, "").strip()
    if not credential:
        raise ValueError("The selected Direct provider requires an API-key connection; CLI subscription sign-in is not API access")
    CURRENT_STAGE = "history_read"
    path = Path(request["history_path"])
    expected_scope = request["history_scope"]
    history = []
    if request.get("history_enabled", True) and path.exists():
        record = json.loads(path.read_text())
        if record.get("scope") != expected_scope:
            raise ValueError("Direct message history does not match its provider/account scope")
        history = ModelMessagesTypeAdapter.validate_json(record["messages"])
    elif request.get("history_enabled", True) and request.get("context_replay"):
        history, tool_call_ids = read_context_replay(request["context_replay"])
        emit({"type": "direct.stage", "stage": "context_tool_replay",
              "messages": len(history), "tool_calls": len(tool_call_ids),
              "bytes": len(ModelMessagesTypeAdapter.dump_json(history))})
    current_images = [BinaryContent(base64.b64decode(image["data_base64"], validate=True),
                                    media_type=image["media_type"]) for image in request.get("input_images", [])]
    available_images = [*_user_images(history), *current_images]
    prompt = direct_image_prompt(request["instruction"], current_images, history)
    session_id = request["session_id"]
    emit({"type": "grok.session.started", "session_id": session_id, "model": model,
          "requested_model": model, "executor": "direct", "provider": provider,
          "requestedReasoningEffort": request.get("effort") if isinstance(request.get("effort"), str)
              and request.get("effort") in {"default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"} else None,
          "executor_ready_ms": (time.monotonic() - STARTED_AT) * 1000})
    CURRENT_STAGE = "tool_configuration"
    toolsets = []
    if request["mcp_servers"]:
        from pydantic_ai.mcp import MCPToolset
        from fastmcp import Client
    for server in request["mcp_servers"]:
        toolset = MCPToolset(Client(mcp_config([server])), max_retries=0,
                             tool_error_behavior="error", prefer_tasks=False)
        enabled = server.get("enabled_tools")
        disabled = server.get("disabled_tools", [])
        if enabled is not None or disabled:
            toolset = toolset.filtered(lambda _ctx, tool, enabled=enabled, disabled=disabled:
                (enabled is None or tool.name in enabled) and tool.name not in disabled)
        if request.get("conversation_tool_mode") == "coordination":
            from workers_projects_runtime.coordination_toolset import CoordinationToolset
            toolset = CoordinationToolset(toolset)
        toolsets.append(toolset if server.get("preserve_tool_names") is True
                        else toolset.prefixed(server["name"]))
    effort = request.get("effort", "default")
    settings = dict(request.get("model_settings") or {})
    if provider in {"openai", "xai"}:
        settings.update(openai_store=False, openai_send_reasoning_ids=True)
        cache_key = request.get("prompt_cache_key")
        from workers_projects_runtime.agent_builder_control import private_machine_state_output_schema
        if (request.get("history_enabled", True)
                and private_machine_state_output_schema(request.get("response_contract")) is None
                and isinstance(cache_key, str) and len(cache_key) == 64
                and all(c in "0123456789abcdef" for c in cache_key)):
            settings["openai_prompt_cache_key"] = cache_key
    async def on_request(_request):
        global CURRENT_STAGE
        CURRENT_STAGE = "provider_request"
        emit({"type": "direct.stage", "stage": "provider_request_sent"})
    async def on_response(response):
        emit({"type": "direct.stage", "stage": "provider_headers_received", "status_code": response.status_code,
              **safe_response_request_id(response.headers)})
    CURRENT_STAGE = "provider_configuration"
    # The official providers can use different HTTPX generations. Their native
    # client class owns that compatibility; a client from another SDK is invalid.
    if provider == "anthropic":
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient, DEFAULT_TIMEOUT
    else:
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient, DEFAULT_TIMEOUT
    # The existing xPerfect supervisor owns the attempt/response deadline and
    # Stop. A separate per-read cap would terminate valid long reasoning early.
    async with DefaultAsyncHttpxClient(follow_redirects=False,
                                timeout=type(DEFAULT_TIMEOUT)(None, connect=DEFAULT_TIMEOUT.connect,
                                    write=DEFAULT_TIMEOUT.write, pool=DEFAULT_TIMEOUT.pool),
                                event_hooks={"request": [on_request], "response": [on_response]}) as http:
        if provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel, _support_tool_forcing
            from pydantic_ai.providers.anthropic import AnthropicProvider
            api = AsyncAnthropic(api_key=credential, max_retries=0, http_client=http)
            selected = AnthropicModel(model, provider=AnthropicProvider(anthropic_client=api),
                                      profile={"supports_json_object_output": False})
            if effort != "default":
                settings["anthropic_effort"] = effort
                settings.setdefault("anthropic_thinking", {"type": "adaptive"})
            def output_tool_available(parameters):
                return _support_tool_forcing(model, selected.profile, settings, parameters)
        else:
            from pydantic_ai.models.openai import OpenAIResponsesModel, _support_tool_forcing, _reasoning_active
            from pydantic_ai.providers.openai import OpenAIProvider
            api = AsyncOpenAI(api_key=credential, base_url="https://api.x.ai/v1" if provider == "xai" else request.get("base_url", "https://api.openai.com/v1"), max_retries=0, http_client=http)
            from pydantic_ai.profiles.openai import OpenAIModelProfile
            profile = OpenAIModelProfile(supports_json_object_output=False)
            if provider == "xai":
                from pydantic_ai.profiles.openai import OpenAIModelProfile
                profile = OpenAIModelProfile(openai_supports_reasoning=True,
                    openai_supports_encrypted_reasoning_content=True,
                    openai_supports_reasoning_effort_none=False,
                    supports_json_schema_output=True, supports_json_object_output=False)
            selected = OpenAIResponsesModel(model, provider=OpenAIProvider(openai_client=api), profile=profile)
            if effort != "default":
                settings["openai_reasoning_effort"] = effort
            def output_tool_available(parameters):
                return _support_tool_forcing(model, selected.profile, settings,
                    thinking=_reasoning_active(selected.profile, settings, parameters))
        emit({"type":"direct.stage", "stage":"provider_client_ready"})
        # The SDK's typed final protocol carries the owning envelope. Ordinary
        # MCP functions keep their native protocol; native schema sampling is
        # used only when request capabilities rule out a thinking-safe output
        # tool. Untyped prose never becomes eligible speech.
        output_options = {}
        output_mode = "plain"
        if request.get("output_schema"):
            from pydantic_ai import NativeOutput, ToolOutput, StructuredDict
            from pydantic_ai.models import ModelRequestParameters
            # The pinned SDK resolves forcing for the actual request, including
            # thinking modes that reject forcing or would lose thinking under it.
            from workers_projects_runtime.agent_builder_control import private_machine_state_output_schema
            private_schema = private_machine_state_output_schema(request.get("response_contract"))
            if private_schema is not None and (request.get("output_schema") != private_schema
                    or request.get("mcp_servers") or request.get("graph_control") or request.get("delivery_control")):
                raise ValueError("Private JSON output requires its exact zero-tool contract")
            native_output = ((private_schema is not None and selected.profile.get("supports_json_schema_output") is True)
                             or not output_tool_available(ModelRequestParameters()))
            if native_output and selected.profile.get("supports_json_schema_output") is not True:
                raise ValueError("The selected model cannot preserve its thinking and typed output contract")
            output_mode = "native" if native_output else "tool"
            CURRENT_OUTPUT_MODE = output_mode
            typed = StructuredDict(sdk_output_schema(request["output_schema"]))
            output_options["output_type"] = NativeOutput(typed) if native_output else ToolOutput(
                typed, name=OUTPUT_TOOL_NAME, max_retries=0)
        archive_input = _core_archive_input(request)
        state_input = _core_state_input(request)
        if archive_input is not None or state_input is not None:
            history = core_archive_repair_history(history)
        from pydantic_ai.capabilities import Hooks
        def observe_response_shape(ctx, *, request_context, response):
            global LAST_RESPONSE_SHAPE
            reason = response.finish_reason
            LAST_RESPONSE_SHAPE = {
                "finish_reason": reason if reason in {"stop", "length", "content_filter", "tool_call", "error"} else "other",
                "parts": len(response.parts),
                "text_parts": sum(isinstance(part, TextPart) for part in response.parts),
                "text_chars": sum(len(part.content) for part in response.parts if isinstance(part, TextPart)),
                "output_tool_calls": sum(isinstance(part, ToolCallPart) and part.tool_name == OUTPUT_TOOL_NAME for part in response.parts),
                "other_tool_calls": sum(isinstance(part, ToolCallPart) and part.tool_name != OUTPUT_TOOL_NAME for part in response.parts),
            }
            return response
        capabilities = [Hooks(after_model_request=observe_response_shape)]
        if archive_input is not None or state_input is not None or request.get("history_enabled", True) or available_images:
            from pydantic_ai.capabilities import Hooks
            from pydantic_ai.messages import ToolReturnPart
            def prepare_history(ctx, model_request):
                global CURRENT_STAGE
                # The SDK identifies this invocation's initial input by run ID.
                # Origin metadata is applied to a copy for both projection and
                # durable checkpoints; raw SDK messages remain untouched.
                from pydantic_ai.messages import ModelRequest
                current = next((message for message in ctx.messages
                    if isinstance(message, ModelRequest) and message.run_id == ctx.run_id), None)
                marked = core_archive_mark_input(ctx.messages, current, request, archive_input, state_input)
                if (request.get("history_enabled", True) and ctx.messages
                        and any(isinstance(part, ToolReturnPart) for part in ctx.messages[-1].parts)):
                    # A completed SDK tool round is durable before later model
                    # generation can be stopped. Preserve the SDK's exact IDs,
                    # reasoning and results; never synthesize or replay an action.
                    previous_stage = CURRENT_STAGE
                    CURRENT_STAGE = "history_checkpoint"
                    private_write(path, json.dumps({"scope": expected_scope,
                        "messages": ModelMessagesTypeAdapter.dump_json(marked).decode()}))
                    CURRENT_STAGE = previous_stage
                if archive_input is not None or state_input is not None:
                    request_current = next((message for message in model_request.messages
                        if isinstance(message, ModelRequest) and message.run_id == ctx.run_id), None)
                    request_marked = core_archive_mark_input(model_request.messages, request_current, request, archive_input, state_input)
                    projection_metrics = {}
                    model_request.messages = core_archive_request_projection(request_marked, archive_input, state_input, projection_metrics)
                    if state_input is not None:
                        emit({"type": "direct.stage", "stage": "core_state_projection",
                            "removed_blocks": projection_metrics.get("removed_blocks", 0),
                              "removed_data_utf8_bytes": projection_metrics.get("removed_data_utf8_bytes", 0),
                            "removed_utf8_bytes": projection_metrics.get("removed_utf8_bytes", 0),
                            "removed_context_sources": projection_metrics.get("removed_context_sources", 0),
                            "removed_context_data_utf8_bytes": projection_metrics.get("removed_context_data_utf8_bytes", 0)})
                if available_images or _user_images(model_request.messages):
                    media_metrics = {}
                    model_request.messages = direct_image_request_projection(
                        model_request.messages, available_images, current_run_id=ctx.run_id, metrics=media_metrics)
                    emit({"type": "direct.stage", "stage": "image_projection", **media_metrics})
                return model_request
            capabilities.append(Hooks(before_model_request=prepare_history))
        agent = Agent(selected, instructions=request["instructions"], toolsets=toolsets,
                      capabilities=capabilities, end_strategy="exhaustive", retries=0, **output_options)
        public_parts = None
        validate_terminal = None
        validated_outputs = {}
        if request.get("output_schema"):
            from jsonschema import Draft202012Validator
            from workers_projects_runtime.agent_builder_control import (
                _unique_json_object, conversation_controls_from_schema, parse_conversation_output)
            validator = Draft202012Validator(request["output_schema"])
            graph, delivery = conversation_controls_from_schema(request["output_schema"])
            graph = request.get("graph_control") or graph
            delivery = request.get("delivery_control") or delivery
            if delivery:
                from workers_projects_runtime.agent_builder_control import NativeConversationParts
                public_parts = NativeConversationParts(graph, delivery)
            def validate_terminal(raw):
                global CURRENT_STAGE
                CURRENT_STAGE = "output_validation"
                parsed = json.loads(raw, object_pairs_hook=_unique_json_object)
                validator.validate(parsed)
                if graph or delivery:
                    decision = parse_conversation_output(raw, graph, delivery)
                    if delivery and decision.get("delivery_disposition", {}).get("valid") is not True:
                        raise ValueError("Direct output did not satisfy its owning delivery contract")
                return parsed
            @agent.output_validator
            def validate_output(ctx, output):
                if output_mode == "tool":
                    part = next(part for part in ctx.messages[-1].parts
                        if isinstance(part, ToolCallPart) and part.tool_name == OUTPUT_TOOL_NAME
                        and part.tool_call_id == ctx.tool_call_id)
                    raw = part.args if isinstance(part.args, str) else json.dumps(part.args)
                else:
                    raw = "".join(part.content for part in ctx.messages[-1].parts
                        if isinstance(part, TextPart) and (part.provider_details or {}).get("phase") != "commentary")
                validate_terminal(raw)
                validated_outputs[ctx.tool_call_id] = raw
                return output
        def emit_update(update, meta=None):
            if public_parts is not None:
                public_parts.update(update, meta)
            emit({"type": "grok.session.update", "session_id": session_id,
                  **({"meta": meta} if meta is not None else {}), "update": update})
        result = None
        part_phases, part_times = {}, {}
        output_parts = {}
        text_parts = {}
        public_output_call_id = None
        adjacent_public_text = None
        raw_text_observed = set()
        def observe_raw_text(value, part_kind):
            if value and part_kind not in raw_text_observed:
                raw_text_observed.add(part_kind)
                stage = "sdk_first_raw_text" if part_kind == "text" else "sdk_first_raw_output_arguments"
                emit({"type":"direct.stage", "stage":stage, "part_kind":part_kind})
        turn_start, last_stream_start = int(time.time() * 1000), 0
        CURRENT_STAGE = "tool_connection"
        async with agent:
            emit({"type":"direct.stage", "stage":"mcp_connections_ready", "connections":len(toolsets)})
            CURRENT_STAGE = "tool_catalog"
            async with agent.run_stream_events(prompt, message_history=history,
                                               model_settings=settings, retries=0) as events:
                async for event in events:
                    text = ""
                    if isinstance(event, PartStartEvent):
                        preceding_text = adjacent_public_text
                        adjacent_public_text = None
                    if request.get("output_schema"):
                        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                            text_parts[event.index] = {"raw":event.part.content, "published":False,
                                "phase":(event.part.provider_details or {}).get("phase")}
                            part_times[event.index] = max(int(time.time() * 1000), last_stream_start + 1)
                            last_stream_start = part_times[event.index]
                            text = event.part.content
                            observe_raw_text(text, "text")
                        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                            text = event.delta.content_delta
                            observe_raw_text(text, "text")
                            text_parts[event.index]["raw"] += text
                        elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                            state = text_parts[event.index]
                            if state["published"]:
                                adjacent_public_text = validate_terminal(state["raw"])
                                emit_update({"sessionUpdate":"response_completed"},
                                    {"promptId":request["run_id"], "turnStartMs":turn_start,
                                     "streamStartMs":part_times[event.index]})
                        elif output_mode == "tool" and isinstance(event, PartStartEvent) and isinstance(event.part, ToolCallPart):
                            if event.part.tool_name == OUTPUT_TOOL_NAME:
                                output_parts[event.index] = {"part":event.part, "raw":"", "confirmation":preceding_text}
                                if public_output_call_id is None:
                                    public_output_call_id = event.part.tool_call_id
                                if event.part.tool_call_id == public_output_call_id:
                                    part_times[event.index] = max(int(time.time() * 1000), last_stream_start + 1)
                                    last_stream_start = part_times[event.index]
                                    text = event.part.args if isinstance(event.part.args, str) else json.dumps(event.part.args) if event.part.args else ""
                                    observe_raw_text(text, "output_tool_arguments")
                                    output_parts[event.index]["raw"] = text
                                    confirmation = output_parts[event.index]["confirmation"]
                                    if confirmation is not None:
                                        if _could_confirm_public_envelope(text, confirmation, graph, delivery):
                                            text = ""
                                        else:
                                            output_parts[event.index]["confirmation"] = None
                        elif output_mode == "tool" and isinstance(event, PartDeltaEvent) and isinstance(event.delta, ToolCallPartDelta) and event.index in output_parts:
                            state = output_parts[event.index]
                            part = state["part"] = event.delta.apply(state["part"])
                            if part.tool_call_id == public_output_call_id:
                                if not isinstance(event.delta.args_delta, (str, type(None))):
                                    CURRENT_STAGE = "output_validation"
                                    raise ValueError("Direct public output requires append-only JSON argument streaming")
                                text = event.delta.args_delta or ""
                                observe_raw_text(text, "output_tool_arguments")
                                state["raw"] += text
                                if state["confirmation"] is not None:
                                    if _could_confirm_public_envelope(state["raw"], state["confirmation"], graph, delivery):
                                        text = ""
                                    else:
                                        # Only the matching prefix was held. A
                                        # changed envelope immediately resumes
                                        # the existing public generation path.
                                        state["confirmation"] = None
                                        text = state["raw"]
                        elif output_mode == "tool" and isinstance(event, PartEndEvent) and isinstance(event.part, ToolCallPart):
                            if (event.part.tool_name == OUTPUT_TOOL_NAME and event.part.tool_call_id == public_output_call_id
                                    and output_parts[event.index]["confirmation"] is None):
                                emit_update({"sessionUpdate": "response_completed"},
                                    {"promptId": request["run_id"], "turnStartMs": turn_start,
                                     "streamStartMs": part_times[event.index]})
                        if text and event.index in text_parts and isinstance(getattr(event,"part",None) or getattr(event,"delta",None), (TextPart, TextPartDelta)):
                            state = text_parts[event.index]
                            if state["phase"] == "commentary":
                                text = ""
                            elif not state["published"]:
                                from workers_projects_runtime.agent_builder_control import public_conversation_prefix
                                if public_conversation_prefix(state["raw"], graph, delivery, include_skipped=True) is None:
                                    text = ""
                                else:
                                    # The existing owner has identified a typed
                                    # public prefix. Release its buffered header,
                                    # then stream every subsequent text delta.
                                    text, state["published"] = state["raw"], True
                    else:
                        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                            part_phases[event.index] = (event.part.provider_details or {}).get("phase")
                            part_times[event.index] = max(int(time.time() * 1000), last_stream_start + 1)
                            last_stream_start = part_times[event.index]
                            text = event.part.content
                            observe_raw_text(text, "text")
                        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                            text = event.delta.content_delta
                            observe_raw_text(text, "text")
                        elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart) and part_phases.get(event.index) != "commentary":
                            emit_update({"sessionUpdate": "response_completed"},
                                {"promptId": request["run_id"], "turnStartMs": turn_start,
                                 "streamStartMs": part_times[event.index]})
                    if text and part_phases.get(event.index) != "commentary":
                        meta = {"promptId": request["run_id"], "turnStartMs": turn_start,
                                "streamStartMs": part_times[event.index]}
                        emit_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}, meta)
                    if isinstance(event, FunctionToolCallEvent):
                        adjacent_public_text = None
                        CURRENT_STAGE = "tool_execution"
                        emit_update({"sessionUpdate": "tool_call", "toolCallId": event.part.tool_call_id,
                                         "title": event.part.tool_name, "status": "in_progress", "rawInput": event.part.args,
                                         "_meta": {"x.ai/tool": {"name": event.part.tool_name, "input": event.part.args}}})
                    elif isinstance(event, FunctionToolResultEvent):
                        adjacent_public_text = None
                        emit_update({"sessionUpdate": "tool_call_update", "toolCallId": event.part.tool_call_id,
                                         "status": "completed", "rawOutput": event.part.content, "content": [{"type": "text", "text": str(event.part.content)}]})
                    elif isinstance(event, AgentRunResultEvent):
                        result = event.result
        if result is None:
            raise ValueError("Direct executor ended without a terminal SDK result")
        CURRENT_STAGE = "output_validation"
        last_response = result.all_messages()[-1]
        final_output = validated_outputs.get(public_output_call_id if output_mode == "tool" else None) if request.get("output_schema") else "".join(
            part.content for part in last_response.parts
            if isinstance(part, TextPart) and (part.provider_details or {}).get("phase") != "commentary")
        if request.get("output_schema") and (final_output is None or json.loads(final_output) != result.output):
            raise ValueError("Direct executor final output changed its streamed output-tool identity")
        ranges = []
        if public_parts is not None:
            final_output = public_parts.finish()
            ranges = public_parts.delivery_disposition_ranges()
        if validate_terminal is not None:
            validate_terminal(final_output)
        CURRENT_STAGE = "history_commit"
        if request.get("history_enabled", True):
            private_write(path, json.dumps({"scope": expected_scope,
                                           "messages": ModelMessagesTypeAdapter.dump_json(core_archive_history_record(result, request, archive_input, state_input)).decode()}))
        usage = asdict(result.usage)
        last_response = getattr(result, "response", None)
        response_usage = getattr(last_response, "usage", None)
        context_usage = {"source": "unavailable"}
        if response_usage is not None and response_usage.input_tokens > 0:
            context_usage = {"source": "sdk_last_response", "input_tokens": response_usage.input_tokens,
                             "output_tokens": response_usage.output_tokens}
        for message in reversed(result.all_messages() if last_response is not None else []):
            if message is last_response:
                break
            if isinstance(message, ModelRequest) and any(
                isinstance(part, ToolReturnPart) and part.tool_name != OUTPUT_TOOL_NAME
                for part in message.parts
            ):
                context_usage = {"source": "unavailable", "unmeasured_tool_results": True}
                break
        emit({"type": "grok.result", "session_id": session_id, "stop_reason": "end_turn",
              "output": final_output, "usage": {"input_tokens": usage.get("input_tokens", 0),
              "output_tokens": usage.get("output_tokens", 0), "cached_input_tokens": usage.get("cache_read_tokens", 0),
              "cache_write_input_tokens": usage.get("cache_write_tokens", 0)},
              "context_usage": context_usage,
              "executor": "direct", "provider": provider,
              **({"deliveryDispositionRanges": ranges} if ranges else {})})


def safe_response_request_id(headers):
    value = headers.get("x-request-id") or headers.get("request-id")
    if (isinstance(value, str) and 1 <= len(value) <= 128
            and all(char.isascii() and (char.isalnum() or char in "_-.:" ) for char in value)):
        return {"provider_request_id": value}
    return {}


def safe_failure_shape(exc):
    chain = []
    current = exc
    while current is not None and len(chain) < 4:
        chain.append(type(current).__name__[:80])
        current = current.__cause__
    shape = {"output_mode": CURRENT_OUTPUT_MODE, "response_shape": dict(LAST_RESPONSE_SHAPE),
             "cause_types": chain}
    # An HTTP-200 stream can fail directly or wrap the provider's typed SDK error.
    # Keep only declared error codes. Never retain provider prose or request content.
    from typing import get_args
    from openai.types.responses.response_error import ResponseError
    declared = get_args(ResponseError.model_fields["code"].annotation)
    current = exc
    for _ in range(4):
        if current is None:
            break
        code = (str(getattr(current, "message", "")).partition(":")[0]
                if type(current).__name__ == "ModelAPIError" else getattr(current, "code", None))
        if code in declared:
            shape["provider_error_code"] = code
            break
        current = current.__cause__
    return shape


def main():
    try:
        request = json.loads(sys.stdin.read())
        asyncio.run(execute(request))
    except Exception as exc:
        # Provider exceptions may contain private tool input. Keep the user-facing
        # diagnostic structured and bounded; the exact exception type is sufficient.
        emit({"type": "direct.error", "error_type": type(exc).__name__,
              "status_code": getattr(exc, "status_code", None), "stage": CURRENT_STAGE,
              **safe_failure_shape(exc),
              "message": "Direct execution could not finish; see its typed stage and recovery"})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
