from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from workers_projects_runtime.conversation_provider import (
    GLASSHIVE_MODELS,
    ConversationProvider,
    _native_authored_preview,
    _native_terminal_text,
    _normalized_harness_activity,
)
from workers_projects_runtime.agent_builder_control import graph_transfer_control


def _assistant(block, *, api_error=False):
    return {
        "type": "assistant",
        "is_api_error_message": api_error,
        "message": {"content": [block]},
    }


def _quota_events():
    diagnostic = "Synthetic provider diagnostic"
    return [
        _assistant({"type": "text", "text": diagnostic}, api_error=True),
        {
            "type": "result", "subtype": "success", "is_error": True,
            "api_error_status": 429, "terminal_reason": "api_error",
            "result": diagnostic,
        },
    ]


def _eligible(events):
    stdout = "\n".join(map(json.dumps, events))
    provider = object.__new__(ConversationProvider)
    provider.store = SimpleNamespace(
        get_provider_session_by_id=lambda _: {
            "model_id": "claude-code:claude-opus-5", "owner_id": "owner-a",
        },
        get_worker=lambda _: {"owner_id": "owner-a", "profile": "claude-code"},
    )
    provider.service = SimpleNamespace(runtime=SimpleNamespace(
        provider_activity_log=lambda _worker, _run: ("claude-code", stdout),
    ))
    provider._model = lambda model, **_: GLASSHIVE_MODELS[model]
    record = {
        "request_id": "request-a", "session_id": "session-a", "owner_id": "owner-a",
        "fallback_model_id": "codex-cli:gpt-6.1-sol", "fallback_instruction": "Exact original request",
    }
    run = {
        "run_id": "run-a", "worker_id": "worker-a", "state": "failed",
        "failure_class": "provider_rate_limited", "failure_retryable": 1,
        "failure_structured": 1, "retry_attempts": 0, "provider_liveness_route_locked": 0,
        "output_text": "",
    }
    activity_types = {
        activity["event_type"] for activity in _normalized_harness_activity("claude-code", stdout)
    }
    return provider._serial_fallback_eligible(record, run, activity_types)


def test_claude_api_error_is_not_terminal_authorship_and_keeps_serial_fallback_eligible():
    events = _quota_events()
    assert _native_terminal_text("claude-code", "\n".join(map(json.dumps, events))) is None
    assert _eligible(events) is True


def test_flagged_assistant_api_error_cannot_be_a_public_preview_or_tool_activity():
    envelope = json.dumps({"type": "assistant_response", "content": "Diagnostic envelope", "tool_name": None})
    events = [
        _assistant({"type": "text", "text": envelope}, api_error=True),
        _assistant({"type": "tool_use", "name": "Bash", "input": {}}, api_error=True),
    ]
    stdout = "\n".join(map(json.dumps, events))
    control = graph_transfer_control([
        {"type": "function", "function": {"name": "lc_transfer_to_check", "parameters": {"type": "object"}}},
    ], "auto")
    assert _native_authored_preview("claude-code", stdout, control, None) is None
    assert _normalized_harness_activity("claude-code", stdout) == []


@pytest.mark.parametrize("block,event_type", [
    ({"type": "text", "text": "A genuine partial response about quota."}, "authored-response"),
    ({"type": "thinking", "thinking": "Synthetic private reasoning"}, "reasoning-summary"),
    ({"type": "tool_use", "name": "Bash", "input": {}}, "tool"),
])
def test_genuine_claude_authorship_before_api_error_still_blocks_fallback(block, event_type):
    events = [_assistant(block), *_quota_events()]
    activity = _normalized_harness_activity("claude-code", "\n".join(map(json.dumps, events)))
    assert [item["event_type"] for item in activity] == [event_type]
    assert str(block.get("text") or block.get("thinking") or "Synthetic private") not in json.dumps(activity)
    assert _eligible(events) is False


def test_successful_claude_terminal_result_is_preserved_before_later_api_error():
    events = [{"type": "result", "is_error": False, "result": "Genuine answer."}, *_quota_events()]
    assert _native_terminal_text("claude-code", "\n".join(map(json.dumps, events))) == "Genuine answer."
    assert _eligible(events) is False
