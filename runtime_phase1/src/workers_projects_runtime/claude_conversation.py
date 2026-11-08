"""Retained Claude stream transport owned by the existing provider conversation.

The native CLI owns history. Each admitted turn supplies its current application
authority and MCP grants through native controls; no user brief is reconstructed.
Startup-only NativeWarmPool capacity is deliberately not used for conversations.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from .bootstrap import CLEAN_ROOM_RUNTIME_ENV_KEYS

STYLE_NAME = "xperfect-application"


def enabled(bundle):
    capabilities = bundle.get("provider_capabilities") or {}
    return (capabilities.get("conversation_process_reuse") is True
            and capabilities.get("native_tools") is False)


def native_servers(payload, environment):
    """Resolve the current admitted environment before crossing the native IPC."""
    servers = copy.deepcopy(payload.get("mcpServers") or {})
    for server in servers.values():
        for field in ("headers", "env"):
            for key, value in (server.get(field) or {}).items():
                if not isinstance(value, str):
                    raise ValueError("Native MCP environment values must be strings")
                for name, replacement in environment.items():
                    value = value.replace("${" + name + "}", replacement)
                if "${" in value:
                    raise ValueError("Native MCP environment reference is unavailable")
                server[field][key] = value
    return servers


def assert_private_style_tree(workspace):
    """Project settings stay disabled in effect; only our native style is admitted."""
    root = workspace / ".claude"
    if not root.exists():
        return
    if root.is_symlink():
        raise ValueError("Native application configuration must be private")
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or relative not in (
                "output-styles", "output-styles/" + STYLE_NAME + ".md"):
            raise ValueError("Unexpected native project configuration in restricted conversation")


def idle_seconds():
    value = float(os.environ.get("GLASSHIVE_CLAUDE_CONVERSATION_IDLE_SECONDS", "120"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Native conversation idle lifetime must be positive")
    return value


def write_authority(workspace, authority):
    from .profile_runtime import _atomic_write_private_text
    directory = workspace.resolve()
    for part in (".claude", "output-styles"):
        directory = directory / part
        if directory.is_symlink():
            raise ValueError("Native application authority directory must be private")
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    path = directory / (STYLE_NAME + ".md")
    _atomic_write_private_text(path,
        "---\nname: " + STYLE_NAME + "\ndescription: Current application context\n"
        "keep-coding-instructions: true\n---\n" + authority)
    return path


def command_projection(command, workspace):
    """Keep the existing policy/model translation; change only the native transport."""
    result = list(command)
    for flag in ("--append-system-prompt-file", "--mcp-config", "--setting-sources", "--input-format"):
        while flag in result:
            at = result.index(flag)
            del result[at:at + 2]
    for flag in ("--safe-mode", "--no-session-persistence"):
        if flag in result:
            result.remove(flag)
    settings = json.loads(result[result.index("--settings") + 1])
    settings.update(outputStyle=STYLE_NAME, autoMemoryEnabled=False, disableAllHooks=True,
                    claudeMdExcludes=["**"])
    result[result.index("--settings") + 1] = json.dumps(settings)
    result.extend(["--input-format", "stream-json", "--setting-sources", "project",
                   "--mcp-config", '{"mcpServers":{}}'])
    return result, settings


class NativeConversation:
    """Only stream handles and control correlation; cognitive state stays native."""
    def __init__(self, process, scope, stderr_path):
        self.process, self.scope, self.stderr_path = process, scope, stderr_path
        self.turn_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.condition = threading.Condition()
        self.responses = {}
        self.events = None
        self.session_id = ""
        self.idle = True
        self.interrupt_requested = False
        self.closed = False
        self.idle_expiry = None
        threading.Thread(target=self._read, daemon=True, name="claude-conversation-stream").start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    events = self.events
                    if events is not None:
                        events.put(line)
                    continue
                if event.get("type") == "control_response":
                    response = event.get("response") or {}
                    with self.condition:
                        self.responses[str(response.get("request_id") or "")] = response
                        self.condition.notify_all()
                    continue
                session = event.get("session_id")
                if isinstance(session, str) and session:
                    self.session_id = session
                events = self.events
                if events is not None:
                    events.put(line)
        finally:
            self.closed = True
            events = self.events
            if events is not None:
                events.put(None)
            with self.condition:
                self.condition.notify_all()

    def send(self, value):
        from .profile_runtime import RuntimeErrorBase
        with self.write_lock:
            try:
                self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise RuntimeErrorBase("The retained native conversation process ended") from exc

    def control(self, request, timeout=15):
        identity = str(uuid.uuid4())
        self.send({"type": "control_request", "request_id": identity, "request": request})
        deadline = time.monotonic() + timeout
        with self.condition:
            while identity not in self.responses:
                remaining = deadline - time.monotonic()
                if self.closed or remaining <= 0:
                    raise RuntimeError("Native conversation control was not acknowledged")
                self.condition.wait(remaining)
            response = self.responses.pop(identity)
        if response.get("subtype") != "success":
            raise RuntimeError("Native conversation control was rejected: " +
                               str(response.get("error") or request.get("subtype")))
        return response.get("response") or {}


def process_scope(command, environment):
    # Run grants never become startup credentials or a retained process's authority.
    static = {k: v for k, v in environment.items()
              if k not in CLEAN_ROOM_RUNTIME_ENV_KEYS and k != "GLASSHIVE_RUN_ID"}
    return hashlib.sha256(json.dumps([command, static], sort_keys=True).encode()).hexdigest(), static


def run_turn(runtime, worker, command, environment, workspace, run_root,
             instruction, timeout_sec, authority_receipt, *, native_image_scope=None):
    from .profile_runtime import (
        _atomic_write_private_text, _redact_command_arg, runtime_start_boundary,
        notify_runtime_started, RuntimeErrorBase,
    )
    if not runtime._native_context_hooks_supported():
        raise RuntimeErrorBase("This native Claude build has not passed the retained conversation protocol checks")
    worker_id, run_id = str(worker["worker_id"]), str(worker["_active_run_id"])
    attempt_id = str(worker.get("_run_attempt_id") or "")
    bundle = runtime._bootstrap_bundle_for_worker(worker)
    authority = str(bundle.get("developer_instructions") or "")
    if not authority:
        raise RuntimeErrorBase("Retained native conversations require current application authority")
    assert_private_style_tree(workspace)
    lifetime = idle_seconds()
    native_command, settings = command_projection(command, workspace)
    scope, native_env = process_scope(native_command, environment)
    deadline = time.monotonic() + timeout_sec if timeout_sec else None
    absolute = str(worker.get("_provider_response_deadline_at") or "")
    if absolute:
        from datetime import timezone
        try:
            stamp = datetime.fromisoformat(absolute.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            projected = time.monotonic() + stamp.timestamp() - time.time()
            deadline = min(deadline, projected) if deadline is not None else projected
        except (ValueError, OverflowError) as exc:
            raise RuntimeErrorBase("The native conversation response deadline is invalid") from exc
    if deadline is not None and deadline <= time.monotonic():
        raise RuntimeErrorBase("The native conversation turn reached its response deadline")
    session = process = previous = active = None
    lock_owner = None
    host_slot_token = ""
    stderr_offset = 0
    completed = False
    result_exit_code = 0

    def stop_owned(snapshot, turn=False):
        return runtime._stop_active_process(worker_id,
            worker=worker if turn else None, run_id=run_id if turn else None,
            expected_session=snapshot)

    def metadata(phase):
        return {"session_name": "conversation-" + str(process.pid),
                "run_id": run_id, "attempt_id": attempt_id,
                "process_pid": process.pid, "process_lifetime": "conversation", "phase": phase,
                "workspace_dir": str(workspace), "stdout_path": str(run_root / "stdout.log"),
                "stderr_path": str(run_root / "stderr.log"), "exit_path": str(run_root / "exit_code"),
                "model": str(runtime._host_runtime_info(worker).model or ""),
                "started_at": datetime.now().astimezone().isoformat(),
                "host_slot_token": host_slot_token, "timeout_seconds": timeout_sec,
                "run_mode": "conversation", "instruction_redacted": True,
                "native_image_scope": native_image_scope}
    try:
        with runtime._process_lock:
            process = runtime._active_processes.get(worker_id)
        session = getattr(process, "_native_conversation", None)
        if session is not None:
            # An idle expiry may already be ending this exact process. Wait for
            # that owner, then re-read its identity before deciding to relaunch.
            if not session.turn_lock.acquire(blocking=False):
                if not session.idle:
                    raise RuntimeErrorBase("The retained native conversation already has an active turn")
                session.turn_lock.acquire()
            lock_owner = session
            previous = runtime._read_active_session(worker_id)
            with runtime._process_lock:
                registered = runtime._active_processes.get(worker_id)
            if (registered is not process or process.poll() is not None
                    or session.closed or session.scope != scope):
                if registered is process and not stop_owned(previous):
                    raise RuntimeErrorBase("The previous native conversation could not be safely reconciled")
                session.turn_lock.release()
                lock_owner = None
                process = session = None
        previous = runtime._read_active_session(worker_id)
        if session is None and previous and not stop_owned(previous):
            raise RuntimeErrorBase("The retained native conversation needs process recovery")
        host_slot_token = runtime._acquire_host_slot(worker)
        if session is None:
            native_stderr = runtime._state_dir(worker_id) / "conversation-native.stderr.log"
            launch_command = list(native_command)
            native_key = (None if runtime._provider_session_starts_fresh(worker)
                          else runtime._read_provider_session_key(worker))
            if native_key:
                try:
                    uuid.UUID(native_key)
                except (ValueError, TypeError):
                    native_key = None
            if native_key:
                launch_command.extend(["--resume", native_key])
            _atomic_write_private_text(run_root / "native-conversation-command.json",
                json.dumps([_redact_command_arg(part) for part in launch_command]))
            with native_stderr.open("a") as stderr:
                native_stderr.chmod(0o600)
                process = subprocess.Popen(launch_command, cwd=workspace, env=native_env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                    text=True, bufsize=1, start_new_session=True)
            session = NativeConversation(process, scope, native_stderr)
            session.turn_lock.acquire()
            lock_owner = session
            process._native_conversation = session
            runtime._register_process(worker_id, process)
            initial = metadata("preparing")
            runtime._write_active_session(worker_id, initial, worker=worker, spawned_process=process)
            previous = runtime._read_active_session(worker_id)
            session.control({"subtype": "initialize", "hooks": {}})
        session.events = queue.SimpleQueue()
        stderr_offset = session.stderr_path.stat().st_size
        if session.idle_expiry is not None:
            session.idle_expiry.cancel()
        preparing = metadata("preparing")
        if not runtime._write_active_session(worker_id, preparing, expected_session=previous):
            raise RuntimeErrorBase("The native conversation ownership changed during preparation")
        previous = runtime._read_active_session(worker_id)
        prepared_at = time.monotonic()
        write_authority(workspace, authority)
        session.control({"subtype": "apply_flag_settings", "settings": settings})
        refreshed = session.control({"subtype": "reload_output_styles"})
        if STYLE_NAME not in refreshed.get("available_output_styles", []):
            raise RuntimeErrorBase("Native application authority refresh is unsupported")
        mcp_path = runtime._state_dir(worker_id) / "conversation-mcp.json"
        runtime._assert_host_claude_mcp_config(worker, mcp_path)
        servers = native_servers(json.loads(mcp_path.read_text()), environment)
        applied = session.control({"subtype": "mcp_set_servers", "servers": servers})
        if applied.get("errors"):
            raise RuntimeErrorBase("The current native capability connections are unavailable")
        payload = {**preparing, "phase": "running", "native_session_id": session.session_id}
        if not runtime._write_active_session(worker_id, payload, expected_session=previous,
                publish_run_start=True, worker=worker, spawned_process=process):
            raise RuntimeErrorBase("The native conversation admission changed before invocation")
        active = runtime._read_active_session(worker_id)
        receipt = {**(authority_receipt or {}), "placement": "native_output_style",
                   "authority_sha256": hashlib.sha256(authority.encode()).hexdigest(),
                   "authority_chars": len(authority), "materialized": True,
                   "native_refresh_acknowledged": True,
                   "authority_preflight_ms": round((time.monotonic()-prepared_at)*1000, 3)}
        _atomic_write_private_text(run_root / "native-provider-authority-receipt.json",
                                  json.dumps(receipt, sort_keys=True))
        raw_stdout = run_root / "stdout.log"
        session.idle = False
        session.interrupt_requested = False
        from .native_input import claude_user_frame
        content = [{"type": "text", "text": instruction}]
        for _path, mime, data in runtime._native_input_image_files(worker, runtime._host_runtime_info(worker)):
            import base64
            content.append({"type": "image", "source": {"type": "base64",
                           "media_type": mime, "data": base64.b64encode(data).decode()}})
        if deadline is not None and deadline <= time.monotonic():
            raise RuntimeErrorBase("The native conversation turn reached its response deadline")
        with runtime_start_boundary(worker):
            notify_runtime_started(worker)
            session.send(claude_user_frame(content))
        runtime._write_action_audit(worker, {"kind": "conversation.provider_invoked",
            "run_id": run_id, "process_pid": process.pid,
            "native_provider_authority_receipt": receipt})
        with raw_stdout.open("w") as stdout:
            raw_stdout.chmod(0o600)
            while True:
                remaining = deadline-time.monotonic() if deadline else None
                if remaining is not None and remaining <= 0:
                    session.control({"subtype": "interrupt"})
                    raise RuntimeErrorBase("The native conversation turn reached its response deadline")
                try:
                    line = session.events.get(timeout=remaining)
                except queue.Empty as exc:
                    session.control({"subtype": "interrupt"})
                    raise RuntimeErrorBase("The native conversation turn reached its response deadline") from exc
                if line is None:
                    exit_code = process.wait(timeout=2)
                    _atomic_write_private_text(run_root / "exit_code", str(exit_code))
                    raise RuntimeErrorBase("The retained native conversation process ended (exit " + str(exit_code) + ")")
                stdout.write(line)
                stdout.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "assistant" and event.get("error"):
                    result_exit_code = 1
                    continue  # Drain its terminal result before accepting any later user turn.
                if event.get("type") == "result":
                    if event.get("is_error") and not (
                            session.interrupt_requested
                            and event.get("terminal_reason") == "aborted_streaming"):
                        result_exit_code = 1
                    completed = True
                    break
        # A retained conversation holds no previous turn's live capability connection.
        removed = session.control({"subtype": "mcp_set_servers", "servers": {}})
        if removed.get("errors"):
            raise RuntimeErrorBase("The previous native capability connection could not be released")
        # The completed run relinquishes this process to its provider conversation.
        # A later control for that run must not see the retained PID as live work.
        idle = {**active, "phase": "idle", "run_id": "", "attempt_id": "",
                "last_run_id": run_id, "native_session_id": session.session_id,
                "host_slot_token": ""}
        if not runtime._write_active_session(worker_id, idle, expected_session=active):
            raise RuntimeErrorBase("The native conversation ownership changed at turn completion")
        session.idle = True
        def expire_idle():
            if session.idle:
                if session.turn_lock.acquire(blocking=False):
                    try:
                        if session.idle:
                            stop_owned(idle)
                    finally:
                        session.turn_lock.release()
        session.idle_expiry = threading.Timer(lifetime, expire_idle)
        session.idle_expiry.daemon = True
        session.idle_expiry.start()
        _atomic_write_private_text(run_root / "exit_code", str(result_exit_code))
        return result_exit_code
    finally:
        cleanup_confirmed = True
        try:
            if lock_owner is not None:
                with session.stderr_path.open("rb") as stderr:
                    stderr.seek(stderr_offset)
                    _atomic_write_private_text(run_root / "stderr.log", stderr.read().decode(errors="replace"))
                session.events = None
                if not completed or not session.idle:
                    cleanup_confirmed = stop_owned(active or previous, turn=True)
                    if not cleanup_confirmed:
                        raise RuntimeErrorBase("Native conversation process cleanup is unconfirmed")
        finally:
            if host_slot_token and cleanup_confirmed:
                runtime._release_host_slot(worker_id, expected_slot_token=host_slot_token)
            if lock_owner is not None:
                lock_owner.turn_lock.release()


def current_session(runtime, worker, run_id, attempt_id):
    active = runtime._read_active_session(str(worker["worker_id"])) or {}
    with runtime._process_lock:
        process = runtime._active_processes.get(str(worker["worker_id"]))
    session = getattr(process, "_native_conversation", None)
    if (not session or session.idle or session.closed or active.get("phase") != "running"
            or active.get("run_id") != run_id or active.get("attempt_id") != attempt_id):
        return None
    return session


def release_cached_conversation(runtime, worker_id):
    captured = runtime._read_active_session(worker_id) or {}
    if captured.get("process_lifetime") != "conversation":
        return
    from .profile_runtime import RuntimeErrorBase
    with runtime._process_lock:
        process = runtime._active_processes.get(worker_id)
    session = getattr(process, "_native_conversation", None)
    if session is not None and not session.turn_lock.acquire(blocking=False):
        raise RuntimeErrorBase("An active retained conversation cannot be replaced")
    try:
        captured = runtime._read_active_session(worker_id) or {}
        if captured.get("phase") != "idle":
            raise RuntimeErrorBase("An active retained conversation cannot be replaced")
        if not runtime._stop_active_process(worker_id, expected_session=captured):
            raise RuntimeErrorBase("Retained native conversation cleanup is unconfirmed")
    finally:
        if session is not None:
            session.turn_lock.release()
