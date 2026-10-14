from __future__ import annotations

import json
from pathlib import Path

import pytest

from workers_projects_runtime import claude_native_hooks as hooks, native_input
from workers_projects_runtime.grok_control import submit_control
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime


def fixture(root):
    (root / "native-control").mkdir(mode=0o700)
    path = root / "claude-hook-context.json"
    native_input.publish(path, {"version": 1, "protocol": "claude-native-hooks",
        "workerId": "worker-fixture", "runId": "run-fixture", "attemptId": "attempt-fixture",
        "nativeSessionActive": False})
    hooks.handle(path, {"hook_event_name": "SessionStart", "session_id": "session-fixture"})
    return path


def send(root, **changes):
    body = {"run_id": "run-fixture", "attempt_id": "attempt-fixture",
        "session_id": "session-fixture", "action": "interject", "text": "Original synthetic bytes.\n",
        "request_fingerprint": "f" * 64, **changes}
    return submit_control(root / "native-control", body, message_id="a" * 64, timeout=0)


@pytest.mark.parametrize("event", ["PostToolBatch", "Stop"])
def test_native_boundary_returns_exact_input_and_retains_replay(tmp_path, event):
    context = fixture(tmp_path)
    assert send(tmp_path)["status"] == "pending"
    result = hooks.handle(context, {"hook_event_name": event, "session_id": "session-fixture"})
    assert result == {"hookSpecificOutput": {"hookEventName": event,
        "additionalContext": "Original synthetic bytes.\n"}}
    receipt = send(tmp_path)
    assert receipt["status"] == "queued"
    assert receipt["native_hook_event"] == event
    assert hooks.handle(context, {"hook_event_name": event, "session_id": "session-fixture"}) == {}
    assert not any(key in receipt for key in ["consumed", "presented", "nativeTurnId"])
    with pytest.raises(ValueError, match="native_input_conflict"):
        send(tmp_path, text="Changed bytes")


@pytest.mark.parametrize("change", ["foreign-run", "foreign-attempt", "foreign-session", "subagent", "ended", "symlink"])
def test_native_hook_fences(tmp_path, change):
    context = fixture(tmp_path)
    args = {}
    if change == "foreign-run": args["run_id"] = "other-run"
    if change == "foreign-attempt": args["attempt_id"] = "other-attempt"
    send(tmp_path, **args)
    event = {"hook_event_name": "PostToolBatch", "session_id": "session-fixture"}
    if change == "foreign-session": event["session_id"] = "other-session"
    if change == "subagent": event["agent_id"] = "native-subagent"
    if change == "ended": hooks.handle(context, {"hook_event_name": "SessionEnd", "session_id": "session-fixture"})
    if change == "symlink":
        target = tmp_path / "target-context.json"
        context.rename(target)
        context.symlink_to(target)
    assert hooks.handle(context, event) == {}
    if change in {"foreign-run", "foreign-attempt"}:
        assert send(tmp_path, **args)["status"] == "rejected"
    else:
        assert send(tmp_path)["status"] == "pending"


def test_prepare_keeps_existing_hooks_settings_and_native_command(tmp_path, monkeypatch):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime, "_native_context_hooks_supported", lambda: True)
    monkeypatch.setattr(runtime, "_bootstrap_bundle_for_worker", lambda worker: {"access_mode": "full"})
    root = tmp_path / "run"
    root.mkdir()
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]},
        "autoMemoryEnabled": True}
    original = ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "high",
        "--settings", json.dumps(settings)]
    worker = {"worker_id": "worker-fixture", "_active_run_id": "run-fixture", "_run_attempt_id": "attempt-fixture"}
    result = runtime._prepare_conversation_authority_command(worker, original,
        run_root=root, timeout_sec=None)
    actual = json.loads(result[-1])
    assert result[:-1] == original[:-1]
    assert actual["autoMemoryEnabled"] is True
    assert actual["hooks"]["Stop"][0] == settings["hooks"]["Stop"][0]
    assert all(name in actual["hooks"] for name in hooks.EVENTS)
    assert native_input.read_object(root / "claude-hook-context.json")["attemptId"] == "attempt-fixture"
    assert (root / "claude_native_hooks.py").is_file()


@pytest.mark.parametrize("case", ["unsupported", "workspace", "disabled-hooks", "missing-attempt"])
def test_prepare_preserves_unsupported_native_path(tmp_path, monkeypatch, case):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime, "_native_context_hooks_supported", lambda: case != "unsupported")
    monkeypatch.setattr(runtime, "_bootstrap_bundle_for_worker", lambda worker:
        {"access_mode": "workspace" if case == "workspace" else "full"})
    command = ["claude", "-p", "--settings", json.dumps({"disableAllHooks": case == "disabled-hooks"})]
    worker = {"worker_id": "worker-fixture", "_active_run_id": "run-fixture",
        "_run_attempt_id": "" if case == "missing-attempt" else "attempt-fixture"}
    assert runtime._prepare_conversation_authority_command(worker, command,
        run_root=tmp_path, timeout_sec=None) == command
    assert not (tmp_path / "claude-hook-context.json").exists()


def test_capability_requires_exact_running_host_attempt(tmp_path, monkeypatch):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "runtime"))
    root = tmp_path / "run"
    root.mkdir()
    context = fixture(root)
    monkeypatch.setattr(runtime, "_run_root", lambda worker, run: root)
    monkeypatch.setattr(runtime, "_read_active_session", lambda worker:
        {"run_id": "run-fixture", "attempt_id": "attempt-fixture"})
    monkeypatch.setattr(runtime, "host_process_identity", lambda worker, run: {"pid": 123})
    worker = {"worker_id": "worker-fixture"}
    assert runtime.supports_native_control(worker, "interject", run_id="run-fixture", attempt_id="attempt-fixture")
    assert not runtime.supports_native_control(worker, "interject", run_id="run-fixture", attempt_id="foreign-attempt")
    assert not runtime.supports_native_control(worker, "interject_graph_output", run_id="run-fixture", attempt_id="attempt-fixture")
    hooks.handle(context, {"hook_event_name": "SessionEnd", "session_id": "session-fixture"})
    assert not runtime.supports_native_control(worker, "interject", run_id="run-fixture", attempt_id="attempt-fixture")
