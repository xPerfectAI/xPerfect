"""Durable native CLI input, bound to one existing supervised run."""
from __future__ import annotations

import hashlib
import json
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


if __name__ == "__main__":
    raise SystemExit(relay(Path(sys.argv[1]), sys.argv[2:]))
