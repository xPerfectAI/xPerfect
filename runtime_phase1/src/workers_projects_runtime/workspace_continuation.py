from __future__ import annotations

import re
from typing import Any

import json

CONNECTED_ACCOUNT_NO_BROKER_NOTE = (
    "Connected-account content intent was requested, but this workspace did not receive a complete "
    "host-signed `glasshive-user-capabilities` broker grant/config in its bootstrap bundle. Do not "
    "claim brokered MCP access, brokered provider reachability, or brokered results. Use only tools "
    "that are actually available inside this worker session and label them accurately; if the needed "
    "provider, content, or auth scope is unavailable, report the blocker instead of filling gaps."
)


DEFAULT_CONTINUATION_REQUEST = (
    "Resume the original task from the current workspace state. "
    "Use available partial work, avoid repeating failed provider-heavy loops when possible, "
    "and produce the final requested deliverables."
)

def _strip_instruction_note(instruction: str, note: str) -> str:
    clean_note = str(note or "").strip()
    if not clean_note:
        return str(instruction or "").strip()
    return re.sub(r"\n{0,2}" + re.escape(clean_note), "", str(instruction or "")).strip()


def _validated_continuation_context(value: object) -> dict[str, Any] | None:
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict) or set(value) != {
        "version",
        "base_instruction",
        "guidance",
    }:
        raise ValueError("Invalid workspace continuation context")
    base_instruction = value.get("base_instruction")
    guidance = value.get("guidance")
    if (
        value.get("version") != 1
        or not isinstance(base_instruction, str)
        or not base_instruction.strip()
        or len(base_instruction.encode("utf-8")) > 128 * 1024
        or not isinstance(guidance, list)
        or len(guidance) > 128
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.encode("utf-8")) > 100 * 1024
            for item in guidance
        )
        or sum(len(item.encode("utf-8")) for item in guidance) > 512 * 1024
    ):
        raise ValueError("Invalid workspace continuation context")
    return {
        "version": 1,
        "base_instruction": base_instruction.strip(),
        "guidance": [str(item).strip() for item in guidance],
    }


def accepted_run_input(run: dict[str, Any]) -> dict[str, Any]:
    """Project the durable accepted input without rendering or parsing prose."""
    instruction = run.get("instruction")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip() or not isinstance(instruction, str):
        raise ValueError("Invalid accepted run input")
    raw_context = run.get("continuation_context_json")
    if isinstance(raw_context, str):
        try:
            raw_context = json.loads(raw_context or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid workspace continuation context") from exc
    validated = _validated_continuation_context(raw_context)
    result: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "instruction": instruction,
    }
    if validated is not None:
        # Validation shares the admission contract; preserve accepted bytes,
        # including whitespace and the ordered guidance, in the wire projection.
        result["continuation_context"] = {
            "version": 1,
            "base_instruction": raw_context["base_instruction"],
            "guidance": list(raw_context["guidance"]),
        }
    return result

def build_workspace_continuation_context(
    *,
    previous_run: dict[str, Any],
    continuation_goal: str | None = None,
) -> dict[str, Any]:
    """Build context from host-persisted structure without parsing prompt prose."""

    raw_context = previous_run.get("continuation_context_json")
    if isinstance(raw_context, str):
        try:
            decoded_context = json.loads(raw_context or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid workspace continuation context") from exc
    else:
        decoded_context = raw_context
    stored = _validated_continuation_context(decoded_context)
    if stored is None:
        base_instruction = _strip_instruction_note(
            str(previous_run.get("instruction") or ""),
            CONNECTED_ACCOUNT_NO_BROKER_NOTE,
        )
        if not base_instruction:
            raise ValueError("Workspace continuation requires prior task context")
        stored = {
            "version": 1,
            "base_instruction": base_instruction,
            "guidance": [],
        }
    clean_goal = str(continuation_goal or "").strip()
    if clean_goal:
        stored = {
            **stored,
            "guidance": [*stored["guidance"], clean_goal],
        }
    validated = _validated_continuation_context(stored)
    if validated is None:
        raise ValueError("Workspace continuation requires prior task context")
    return validated

def continuation_instruction(
    *,
    previous_run: dict[str, Any],
    continuation_goal: str | None = None,
    continuation_context: dict[str, Any] | None = None,
) -> str:
    context = _validated_continuation_context(continuation_context)
    if context is None:
        context = build_workspace_continuation_context(
            previous_run=previous_run,
            continuation_goal=continuation_goal,
        )
    elif str(continuation_goal or "").strip():
        raise ValueError(
            "Continuation goal must already be included in structured context"
        )
    prior_context = str(context["base_instruction"])
    continuation_parts = [str(item) for item in context["guidance"]]
    rendered_continuation = (
        "\n\n".join(continuation_parts)
        if continuation_parts
        else DEFAULT_CONTINUATION_REQUEST
    )
    failure_class = ""
    failure_retryable = False
    recovery = ""
    if str(previous_run.get("state") or "").strip().lower() == "failed":
        failure_class = str(previous_run.get("failure_class") or "").strip() or "unknown"
        failure_retryable = bool(previous_run.get("failure_retryable"))
        recovery = str(previous_run.get("failure_recommended_recovery") or "").strip()

    chunks = [
        "Continue this GlassHive workspace from its current files, browser state, notes, and partial outputs.",
        "Preserve trusted requirements and any files already available in the workspace.",
        "Do not replace binary source files with text extracts unless the user explicitly asked for text extraction only.",
    ]
    if prior_context:
        # Worker-readable text is a projection only. The host-persisted
        # structured continuation contract remains the authority.
        chunks.append(f"Prior run task context:\n{prior_context}")
    if failure_class:
        chunks.append(
            "Previous failure classification:\n"
            f"- class: {failure_class}\n"
            f"- retryable: {failure_retryable}\n"
            f"- recovery guidance: {recovery or 'Continue carefully.'}"
        )
    chunks.append(f"Continuation context:\n{rendered_continuation}")
    return "\n\n".join(chunks)
