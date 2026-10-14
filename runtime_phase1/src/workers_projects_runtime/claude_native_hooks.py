"""Claude native safe-boundary context over the existing exact-attempt mailbox.

The hook returns unmodified input as native additionalContext. Its receipt means
queued at that hook boundary, never model consumption or public presentation.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys

try:
    from .native_input import read_object, publish, NativeInputError
    from .grok_control import control_digest, write_private
except ImportError:  # Private run-local copy, invoked directly by the native CLI.
    from native_input import read_object, publish, NativeInputError
    from grok_control import control_digest, write_private


REVIEWED_VERSION = "2.1.285 (Claude Code)"
EVENTS = ("SessionStart", "PostToolBatch", "Stop", "SessionEnd")


def handle(context_path: Path, event: dict) -> dict:
    name = event.get("hook_event_name")
    session = event.get("session_id")
    # The hooks also inherit into native subagents. Only the foreground session
    # owns this mailbox, so an inherited subagent must never drain it.
    if name not in EVENTS or not isinstance(session, str) or not session or event.get("agent_id"):
        return {}
    root = context_path.parent / "native-control"
    lock_path = root / ".hook.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            context = read_object(context_path)
            if context.get("protocol") != "claude-native-hooks":
                return {}
            expected = context.get("nativeSessionId")
            if name == "SessionStart":
                if expected and expected != session:
                    return {}
                publish(context_path, {**context, "nativeSessionId": session,
                    "nativeSessionActive": True})
                return {}
            if expected != session or context.get("nativeSessionActive") is not True:
                return {}
            if name == "SessionEnd":
                publish(context_path, {**context, "nativeSessionActive": False})
                return {}
            texts = []
            for path in sorted(root.glob("*.request"), key=lambda p: p.stat().st_mtime_ns)[:32]:
                request = read_object(path)
                receipt = path.with_suffix(".response")
                if receipt.exists():
                    continue
                valid = (request.get("run_id") == context.get("runId")
                    and request.get("attempt_id") == context.get("attemptId")
                    and request.get("session_id") == session
                    and request.get("action") == "interject"
                    and isinstance(request.get("text"), str) and bool(request["text"])
                    and isinstance(request.get("message_id"), str)
                    and request.get("message_id") == path.stem
                    and isinstance(request.get("request_fingerprint"), str))
                result = ({"status": "queued", "message_id": path.stem,
                    "native_hook_event": name, "native_session_id": session}
                    if valid else {"status": "rejected", "error": "native_input_stale"})
                write_private(receipt, {**result, "_request": request,
                    "_request_digest": control_digest(request)}, first_writer=True)
                if valid:
                    texts.append(request["text"])
                path.unlink(missing_ok=True)
            if not texts:
                return {}
            # No semantic decision, rewrite, truncation, second turn or new manager.
            return {"hookSpecificOutput": {"hookEventName": name,
                "additionalContext": "\n\n".join(texts)}}
    except (OSError, ValueError, KeyError, NativeInputError):
        return {}


def main():
    event = json.loads(sys.stdin.read(1024 * 1024))
    result = handle(Path(sys.argv[1]), event) if isinstance(event, dict) else {}
    print(json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
