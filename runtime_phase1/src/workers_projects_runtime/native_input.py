"""Durable native CLI input, bound to one existing supervised run."""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import tomllib
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid


class NativeInputError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_object(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise NativeInputError("native_input_invalid")
    if info.st_mode & 0o077 or info.st_size > 1048576:
        raise NativeInputError("native_input_invalid")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise NativeInputError("native_input_invalid")
    return value


def read_response(path: Path) -> dict:
    # The atomic link may briefly have two names until its temporary name is removed.
    # Wait only for that publication window; permanent extra links remain invalid.
    deadline = time.monotonic() + 0.1
    while True:
        before = path.lstat()
        try:
            return read_object(path)
        except NativeInputError:
            info = path.lstat()
            if (
                before.st_dev == info.st_dev
                and before.st_ino == info.st_ino
                and before.st_nlink == 2
                and stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
            ):
                # The winning writer removed its temporary hard link between
                # the first validation and this metadata refresh.
                continue
            if info.st_nlink != 2 or info.st_uid != os.getuid() or time.monotonic() >= deadline:
                raise
            # Recover a writer lost after link publication but before temp cleanup.
            for temporary in path.parent.glob(".native-input-response-*"):
                try:
                    temporary_info = temporary.lstat()
                    if (temporary_info.st_dev, temporary_info.st_ino) == (info.st_dev, info.st_ino):
                        temporary.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
            time.sleep(0.001)


def publish(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".native-input-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def request_path(root: Path, request_id: str) -> Path:
    return root / ("native-input-" + hashlib.sha256(request_id.encode()).hexdigest() + ".json")


def pending(root: Path, *, worker_id: str, run_id: str) -> dict | None:
    context = read_object(root / "native-input-context.json")
    if context.get("workerId") != worker_id or context.get("runId") != run_id:
        raise NativeInputError("native_input_stale")
    requests = []
    for path in root.glob("native-input-*.json"):
        if path.name == "native-input-context.json" or path.name.endswith(".response.json"):
            continue
        item = read_object(path)
        if item.get("contextToken") != context.get("contextToken") or item.get("state") != "pending":
            continue
        requests.append(item)
    if not requests:
        return None
    result = min(requests, key=lambda value: value.get("createdAt", 0))
    return {k: v for k, v in result.items() if k not in {"contextToken", "nativeRequest", "createdAt"}}


def respond(root: Path, *, worker_id: str, run_id: str, request_id: str,
            request_fingerprint: str, action: str, content: dict | None = None,
            allow_new: bool = True) -> dict:
    if action not in {"accept", "decline", "cancel"} or (content is not None and not isinstance(content, dict)):
        raise NativeInputError("native_input_invalid")
    context = read_object(root / "native-input-context.json")
    if context.get("workerId") != worker_id or context.get("runId") != run_id:
        raise NativeInputError("native_input_stale")
    path = request_path(root, request_id)
    try:
        item = read_object(path)
    except FileNotFoundError as exc:
        raise NativeInputError("native_input_stale") from exc
    if item.get("contextToken") != context.get("contextToken") or item.get("requestFingerprint") != request_fingerprint:
        raise NativeInputError("native_input_stale")
    response = {"action": action}
    if content is not None:
        response["content"] = content
    accepted = {"requestId": request_id, "requestFingerprint": request_fingerprint, "contextToken": context["contextToken"], "response": response}
    response_path = path.with_suffix(".response.json")
    if response_path.exists():
        if read_response(response_path) == accepted:
            return {"status": "already_accepted"}
        raise NativeInputError("native_input_conflict")
    if not allow_new or item.get("state") != "pending":
        raise NativeInputError("native_input_stale")
    if action == "accept" and item.get("mode") == "form":
        from jsonschema import Draft202012Validator
        from referencing import Registry
        try:
            from jsonschema import FormatChecker
            checker = FormatChecker(formats=("date", "date-time", "email", "uri"))
            schema = item.get("requestedSchema") or {}
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema, registry=Registry(), format_checker=checker).validate(content or {})
        except Exception as exc:
            raise NativeInputError("native_input_invalid") from exc
    # Publish complete bytes with first-writer semantics. No reader sees partial JSON.
    fd, temp_name = tempfile.mkstemp(prefix=".native-input-response-", dir=root)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(accepted) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_path, response_path)
        except FileExistsError:
            if read_response(response_path) == accepted:
                return {"status": "already_accepted"}
            raise NativeInputError("native_input_conflict")
    finally:
        temp_path.unlink(missing_ok=True)
    return {"status": "accepted"}


def relay(context_path: Path, command: list[str]) -> int:
    context = read_object(context_path)
    if context.get("protocol") == "codex-app-server":
        return codex_relay(context_path, command)
    root = context_path.parent
    instruction = sys.stdin.buffer.read().decode()
    context = {**context, "nativeTurnId": str(uuid.uuid4()), "nativeSessionActive": False}
    publish(context_path, context)
    child_env = {**os.environ, "GLASSHIVE_NATIVE_INPUT_CONTEXT": str(context_path.resolve()),
                 "GLASSHIVE_NATIVE_INPUT_TOKEN": context["contextToken"]}
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=child_env)
    assert child.stdin is not None and child.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    live: dict[str, dict] = {}
    buffer = b""

    def terminate(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, terminate)

    def send(value: dict) -> None:
        child.stdin.write(canonical(value) + b"\n")
        child.stdin.flush()

    def close_request(request_id: str, state: str) -> None:
        item = live.pop(request_id, None)
        if item is not None:
            publish(request_path(root, request_id), {**item, "state": state})

    send({"type": "control_request", "request_id": "glasshive_initialize", "request": {"subtype": "initialize", "hooks": {}}})
    send({"type": "user", "session_id": "", "parent_tool_use_id": None, "uuid": context["nativeTurnId"],
          "message": {"role": "user", "content": instruction}})
    try:
        while selector.get_map():
            for key, _ in selector.select(timeout=0.05):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    sys.stdout.buffer.write(line + b"\n")
                    sys.stdout.buffer.flush()
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "system" and event.get("subtype") == "init":
                        session_id = event.get("session_id")
                        if isinstance(session_id, str) and session_id:
                            context = {**context, "nativeSessionId": session_id, "nativeSessionActive": True}
                            publish(context_path, context)
                    if kind == "control_request":
                        request = event.get("request") or {}
                        request_id = str(event.get("request_id") or "")
                        if request.get("subtype") != "elicitation":
                            send({"type": "control_response", "response": {"subtype": "error", "request_id": request_id, "error": "Native control request requires a supported owner handler"}})
                            continue
                        raw = {"context": context, "requestId": request_id, "request": request}
                        item = {"version": 1, "requestId": request_id, "requestFingerprint": digest(raw),
                                "kind": "elicitation", "mcpServerName": request.get("mcp_server_name", ""),
                                "message": request.get("message", ""), "mode": request.get("mode", "form"),
                                "state": "pending", "contextToken": context["contextToken"], "createdAt": time.time(), "nativeRequest": request}
                        for native, public in [("requested_schema", "requestedSchema"), ("url", "url"), ("elicitation_id", "elicitationId"), ("title", "title")]:
                            if request.get(native) is not None:
                                item[public] = request[native]
                        live[request_id] = item
                        publish(request_path(root, request_id), item)
                    elif kind == "control_cancel_request":
                        close_request(str(event.get("request_id") or ""), "cancelled")
                    elif kind == "result":
                        for request_id in list(live):
                            close_request(request_id, "cancelled")
                        child.stdin.close()
            for request_id, item in list(live.items()):
                response_path = request_path(root, request_id).with_suffix(".response.json")
                try:
                    answer = read_response(response_path)
                except (FileNotFoundError, ValueError):
                    continue
                if answer.get("requestFingerprint") != item["requestFingerprint"] or answer.get("contextToken") != context["contextToken"]:
                    continue
                send({"type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": answer["response"]}})
                close_request(request_id, "answered")
        if buffer:
            sys.stdout.buffer.write(buffer)
            sys.stdout.buffer.flush()
        return child.wait()
    finally:
        selector.close()
        for request_id in list(live):
            close_request(request_id, "cancelled")
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        publish(context_path, {**context, "nativeSessionActive": False})


def codex_app_launch(command: list[str]) -> dict | None:
    """Translate only understood exec flags; unknown policy keeps the exec path."""
    if len(command) < 3 or command[1] != "exec" or command[-1] != "-":
        return None
    argv = [command[0], "app-server", "--stdio"]
    thread, turn, images = {}, {}, []
    overrides = {}
    resumed = command[2] == "resume"
    tokens = command[3:-1] if resumed else command[2:-1]
    positionals = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--json", "--skip-git-repo-check"}:
            index += 1
            continue
        if token == "--ephemeral":
            thread["ephemeral"] = True
            index += 1
            continue
        if token == "--dangerously-bypass-approvals-and-sandbox":
            overrides.update(sandbox_mode="danger-full-access", approval_policy="never")
            argv.extend(["-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="never"'])
            index += 1
            continue
        if token == "--strict-config":
            argv.append(token)
            index += 1
            continue
        if token in {"-c", "--config", "--enable", "--disable", "-m", "--model", "-C", "--cd", "-s", "--sandbox", "--add-dir", "--image", "--output-schema"}:
            if index + 1 >= len(tokens):
                return None
            value = tokens[index + 1]
            if token in {"-c", "--config"}:
                argv.extend([token, value])
                try:
                    overrides.update(tomllib.loads(value))
                except tomllib.TOMLDecodeError:
                    return None
            elif token in {"--enable", "--disable"}:
                argv.extend([token, value])
            elif token in {"-m", "--model"}:
                thread["model"] = value
            elif token in {"-C", "--cd"}:
                thread["cwd"] = value
            elif token in {"-s", "--sandbox"}:
                overrides["sandbox_mode"] = value
                argv.extend(["-c", "sandbox_mode=" + json.dumps(value)])
            elif token == "--add-dir":
                thread.setdefault("runtimeWorkspaceRoots", []).append(value)
            elif token == "--image":
                images.append({"type": "localImage", "path": value})
            else:
                turn["outputSchema"] = json.loads(Path(value).read_text())
            index += 2
            continue
        if token.startswith("-"):
            return None
        positionals.append(token)
        index += 1
    if (resumed and len(positionals) != 1) or (not resumed and positionals):
        return None
    if resumed:
        thread["threadId"] = positionals[0]
        thread["excludeTurns"] = True
    if "model" in overrides:
        thread["model"] = overrides["model"]
    if "model_provider" in overrides:
        thread["modelProvider"] = overrides["model_provider"]
    if "sandbox_mode" in overrides:
        thread["sandbox"] = overrides["sandbox_mode"]
    if "approval_policy" in overrides:
        thread["approvalPolicy"] = overrides["approval_policy"]
    if "model_reasoning_effort" in overrides:
        turn["effort"] = overrides["model_reasoning_effort"]
    additional_roots = list(thread.get("runtimeWorkspaceRoots") or [])
    if additional_roots:
        workspace_policy = overrides.setdefault("sandbox_workspace_write", {})
        if not isinstance(workspace_policy, dict):
            return None
        configured_roots = workspace_policy.get("writable_roots") or []
        if not isinstance(configured_roots, list):
            return None
        writable_roots = list(dict.fromkeys([*configured_roots, *additional_roots]))
        workspace_policy["writable_roots"] = writable_roots
        argv.extend(["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(writable_roots)])
        if thread.get("cwd"):
            thread["runtimeWorkspaceRoots"].insert(0, thread["cwd"])
    if thread.get("approvalPolicy") != "never":
        return None
    if not resumed:
        thread["experimentalRawEvents"] = True
    return {"command": argv, "thread": thread, "turn": turn, "images": images,
            "resumed": resumed, "workspace_policy": overrides.get("sandbox_workspace_write") or {}}


class CodexRpc:
    """One native stdio connection; control responses never block event reading."""
    def __init__(self, command: list[str]):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.waiters = {}
        self.closed = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line.encode()) > 8 * 1024 * 1024:
                    raise NativeInputError("native_event_oversized")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise NativeInputError("native_event_invalid")
                if "id" in value and "method" not in value:
                    with self.lock:
                        waiter = self.waiters.get(value["id"])
                    if waiter is not None:
                        waiter.put(value)
                else:
                    self.events.put(value)
        except (OSError, ValueError, NativeInputError):
            pass
        finally:
            with self.lock:
                self.closed = True
                for waiter in self.waiters.values():
                    waiter.put(None)
            self.events.put(None)

    def send(self, payload):
        with self.lock:
            if self.closed or self.process.stdin.closed:
                raise NativeInputError("native_control_closed")
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def call(self, method, params, timeout=10):
        identity = "xperfect-" + uuid.uuid4().hex
        waiter = queue.Queue()
        with self.lock:
            if self.closed:
                raise NativeInputError("native_control_closed")
            self.waiters[identity] = waiter
        try:
            self.send({"id": identity, "method": method, "params": params})
            try:
                value = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise NativeInputError("native_control_unconfirmed") from exc
            if value is None:
                raise NativeInputError("native_control_closed")
            if "error" in value:
                raise NativeInputError("native_control_rejected")
            result = value.get("result")
            if not isinstance(result, dict):
                raise NativeInputError("native_control_invalid_ack")
            return result
        finally:
            with self.lock:
                self.waiters.pop(identity, None)

    def close(self):
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.reader.join(timeout=1)


class CodexTurn:
    def __init__(self, rpc, session_id, turn_id):
        self.rpc, self.session_id, self.turn_id = rpc, session_id, turn_id
        self.active = True
        self.lock = threading.Lock()

    def interject(self, text, message_id):
        with self.lock:
            if not self.active:
                raise NativeInputError("native_turn_ended")
            params = {"threadId": self.session_id, "expectedTurnId": self.turn_id,
                      "input": [{"type": "text", "text": text}]}
        result = self.rpc.call("turn/steer", params)
        if result.get("turnId") != self.turn_id:
            raise NativeInputError("native_turn_ack_mismatch")
        return {"status": "queued", "message_id": message_id}

    def cancel(self):
        self.end()
        self.rpc.call("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id})

    def end(self):
        with self.lock:
            self.active = False


_CODEX_ITEM_TYPES = {
    "agentMessage": "agent_message", "commandExecution": "command_execution",
    "fileChange": "file_change", "mcpToolCall": "mcp_tool_call", "dynamicToolCall": "dynamic_tool_call",
    "collabAgentToolCall": "collab_agent_tool_call", "subAgentActivity": "sub_agent_activity",
    "webSearch": "web_search", "imageView": "image_view", "imageGeneration": "image_generation",
    "contextCompaction": "context_compaction", "functionCallOutput": "function_call_output",
}
_CODEX_ITEM_FIELDS = {"aggregatedOutput": "aggregated_output", "exitCode": "exit_code",
    "receiverThreadIds": "receiver_thread_ids", "senderThreadId": "sender_thread_id",
    "agentsStates": "agents_states", "agentThreadId": "agent_thread_id", "agentPath": "agent_path",
    "reasoningEffort": "reasoning_effort", "savedPath": "saved_path", "contentItems": "content_items"}


def codex_event(value, session_id, turn_id):
    """Keep the existing exec reader's typed items, native calls and team evidence."""
    params = value.get("params") or {}
    if params.get("threadId") != session_id or params.get("turnId", turn_id) != turn_id:
        return None
    method = value.get("method")
    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("type") in {"userMessage", "hookPrompt"}:
            return None
        mapped = {_CODEX_ITEM_FIELDS.get(key, key): data for key, data in item.items()}
        mapped["type"] = _CODEX_ITEM_TYPES.get(item.get("type"), item.get("type"))
        if mapped["type"] == "reasoning":
            mapped["text"] = "\n".join(item.get("summary") or item.get("content") or [])
        if mapped["type"] == "sub_agent_activity":
            return {"type": "sub_agent_activity", **{key: data for key, data in mapped.items() if key != "type"}}
        if mapped["type"] == "web_search" and "results" in mapped:
            mapped["result"] = mapped["results"]
        return {"type": "item.started" if method == "item/started" else "item.completed", "item": mapped}
    if method == "item/rawResponseItem/completed" and isinstance(params.get("item"), dict):
        return {"type": "response_item", "payload": params["item"]}
    # Raw native events preserve async call/acceptance evidence not present in an item summary.
    if str(method).startswith("codex/event/") and isinstance(params.get("msg"), dict):
        msg = params["msg"]
        if msg.get("type") == "raw_response_item" and isinstance(msg.get("item"), dict):
            return {"type": "response_item", "payload": msg["item"]}
    return None


CODEX_EXEC_REVIEWED_VERSION = "codex-cli 0.162.0-alpha.17.2"
CODEX_EXEC_REQUEST_REJECTIONS = {
    "item/commandExecution/requestApproval": ("command execution approval", "threadId"),
    "item/fileChange/requestApproval": ("file change approval", "threadId"),
    "item/tool/requestUserInput": ("request_user_input", "threadId"),
    "item/tool/call": ("dynamic tool calls", "threadId"),
    "item/permissions/requestApproval": ("permissions approval", "threadId"),
    "applyPatchApproval": ("apply_patch approval", "conversationId"),
    "execCommandApproval": ("exec command approval", "conversationId"),
    "account/chatgptAuthTokens/refresh": ("chatgpt auth token refresh", None),
    "attestation/generate": ("attestation generation", None),
    "currentTime/read": ("external current time", None),
}
CODEX_EXEC_REQUEST_METHODS = {*CODEX_EXEC_REQUEST_REJECTIONS, "mcpServer/elicitation/request"}


def codex_exec_response(event: dict) -> dict:
    """Keep the reviewed native exec's typed server-request behavior, including bees.

    Source: openai/codex rust-v0.162.0-alpha.17.2, exec/src/lib.rs,
    canceled_mcp_server_elicitation_response and handle_server_request.
    This does not advertise interactive request_user_input support.
    """
    method = event["method"]
    if method == "mcpServer/elicitation/request":
        return {"id": event["id"], "result": {"action": "cancel", "content": None, "_meta": None}}
    route = CODEX_EXEC_REQUEST_REJECTIONS.get(method)
    if route is None:
        # Preflight excludes unreviewed server-request schemas before inference.
        raise NativeInputError("native_server_request_unreviewed")
    name, thread_key = route
    message = f"{name} {'are' if name == 'dynamic tool calls' else 'is'} not supported in exec mode"
    if thread_key:
        message += f" for thread `{(event.get('params') or {}).get(thread_key, '')}`"
    return {"id": event["id"], "error": {"code": -32000, "message": message, "data": None}}


def codex_relay(context_path: Path, command: list[str]) -> int:
    try:
        from .grok_control import ControlMailbox
    except ImportError:
        from grok_control import ControlMailbox
    context = read_object(context_path)
    instruction = sys.stdin.buffer.read().decode()
    launch = context["launch"]
    rpc = CodexRpc(command)
    turn = mailbox = None
    usage = None

    def emit(value):
        sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def stopped(signum, _frame):
        raise SystemExit(128 + signum)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stopped)
    try:
        rpc.call("initialize", {"clientInfo": {"name": "xperfect-native", "version": "1"},
                                "capabilities": {"experimentalApi": True}})
        rpc.send({"method": "initialized", "params": {}})
        options = launch["thread"]
        result = rpc.call("thread/resume" if launch["resumed"] else "thread/start", options)
        session_id = (result.get("thread") or {}).get("id")
        if not isinstance(session_id, str) or not session_id or (launch["resumed"] and session_id != options["threadId"]):
            raise NativeInputError("native_session_mismatch")
        # A native server may report a substituted model or policy. Do not author on it.
        for wanted, actual in [("model", "model"), ("modelProvider", "modelProvider"), ("approvalPolicy", "approvalPolicy")]:
            if wanted in options and result.get(actual) != options[wanted]:
                raise NativeInputError("native_binding_mismatch")
        expected_sandbox = {"danger-full-access": "dangerFullAccess", "read-only": "readOnly", "workspace-write": "workspaceWrite"}.get(options.get("sandbox"))
        if expected_sandbox and (result.get("sandbox") or {}).get("type") != expected_sandbox:
            raise NativeInputError("native_sandbox_mismatch")
        if options.get("cwd") and Path(result.get("cwd") or "").resolve() != Path(options["cwd"]).resolve():
            raise NativeInputError("native_workspace_mismatch")
        workspace_policy = launch.get("workspace_policy") or {}
        if (expected_sandbox == "workspaceWrite" and "network_access" in workspace_policy
                and (result.get("sandbox") or {}).get("networkAccess") is not workspace_policy["network_access"]):
            raise NativeInputError("native_network_policy_mismatch")
        expected_roots = {str(Path(path).resolve()) for path in workspace_policy.get("writable_roots") or []}
        actual_roots = {str(Path(path).resolve()) for path in (result.get("sandbox") or {}).get("writableRoots") or []}
        if expected_sandbox == "workspaceWrite" and not expected_roots <= actual_roots:
            raise NativeInputError("native_writable_roots_mismatch")
        if "runtimeWorkspaceRoots" in options and result.get("runtimeWorkspaceRoots") != options["runtimeWorkspaceRoots"]:
            raise NativeInputError("native_workspace_roots_mismatch")
        expected_effort = launch["turn"].get("effort")
        if expected_effort and result.get("reasoningEffort") != expected_effort:
            raise NativeInputError("native_effort_mismatch")
        emit({"type": "thread.started", "thread_id": session_id})
        started = rpc.call("turn/start", {**launch["turn"], "threadId": session_id,
            "input": [{"type": "text", "text": instruction}, *launch["images"]]})
        turn_id = (started.get("turn") or {}).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise NativeInputError("native_turn_missing")
        turn = CodexTurn(rpc, session_id, turn_id)
        context = {**context, "nativeSessionId": session_id, "nativeTurnId": turn_id,
                   "nativeSessionActive": True, "nativeGraphControl": launch.get("interject_graph_control"),
                   "nativeControlCapabilities": {"interject": True, "cancel": True,
                       "interject_graph_output": bool(launch.get("interject_graph_control"))}}
        publish(context_path, context)
        mailbox = ControlMailbox(context_path.parent / "native-control", run_id=context["runId"],
            attempt_id=context["attemptId"], session=turn, event=lambda _event: None)
        mailbox.start()
        emit({"type": "turn.started"})
        while True:
            event = rpc.events.get()
            if event is None:
                raise NativeInputError("native_control_closed")
            params = event.get("params") or {}
            if "id" in event and "method" in event:
                rpc.send(codex_exec_response(event))
                continue
            if params.get("threadId") != session_id:
                continue
            if event.get("method") == "thread/tokenUsage/updated" and params.get("turnId") == turn_id:
                usage = (params.get("tokenUsage") or {}).get("last")
                continue
            if event.get("method") == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                turn.end()
                publish(context_path, {**context, "nativeSessionActive": False})
                status = params["turn"].get("status")
                if status == "completed":
                    counts = {"input_tokens": usage.get("inputTokens"), "cached_input_tokens": usage.get("cachedInputTokens", 0),
                              "output_tokens": usage.get("outputTokens")} if isinstance(usage, dict) else None
                    emit({"type": "turn.completed", **({"usage": counts} if counts else {})})
                    return 0
                error = params["turn"].get("error") or {}
                emit({"type": "turn.failed", "error": error, "native_status": status})
                return 1
            projected = codex_event(event, session_id, turn_id)
            if projected:
                emit(projected)
    except (NativeInputError, OSError, ValueError, KeyError) as exc:
        emit({"type": "turn.failed", "error": {"message": str(exc)}})
        return 1
    finally:
        if turn is not None:
            turn.end()
        publish(context_path, {**context, "nativeSessionActive": False})
        if mailbox is not None:
            mailbox.close()
        rpc.close()


if __name__ == "__main__":
    raise SystemExit(relay(Path(sys.argv[1]), sys.argv[2:]))
