"""Read a retained run's operator-visible records. Never execute or repair work."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from contextlib import ExitStack

MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_PAGE_CHARS = 131072
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_SENSITIVE_COMMAND = re.compile(
    r"(?i)(?:\.codex|\.env\b|auth\.json|credentials|private[-_ ]?key|"
    r"\b(?:SKILL|AGENTS|CLAUDE|CODEX)\.md|\.agents/skills/[^\n]*references|"
    r"(?:instruction|prompt|project-definition|processing-background|operator-policy-context|constraint-ledger)|"
    r"\b(?:env|printenv|export|declare|set)\b|/proc/(?:self|thread-self|[0-9]+)/environ\b)"
)
_SECRETS = (
    re.compile(r"(?i)([?&](?:amp;)?tempauth=)[^&#\s'\"<>]+"),
    re.compile(r"(?i)(authorization\s*:\s*)[^\r\n]+"),
    re.compile(r'''(?i)((?:['"])?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret)(?:['"])?\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''),
    re.compile(r"(?i)((?:['\"])?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret)(?:['\"])?\s*[=:]\s*(?:['\"])?)[^'\"\s,;]+"),
)
_HOST_PATH = re.compile(r"(?:/(?:Users|home|srv|opt|var)/[^\s\"'<>`]+|[A-Za-z]:\\[^\s\"'<>`]+)")
_PRIVATE_CONTENT = re.compile(
    r"(?i)(?:<system>|<developer>|<instructions>|system[_ ]prompt|developer[_ ]message|"
    r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|GLASSHIVE_WORKER_COMPLETION_CONTRACT|"
    r"# (?:AGENTS|CLAUDE|CODEX)\.md|\[\[?system\]?)"
)


class ProcessLogChanged(ValueError):
    pass


def _safe_text(value: object, redact) -> str:
    text = str(value or "").replace("\x00", "")
    if _PRIVATE_CONTENT.search(text):
        return "[Instruction or private-key content omitted]"
    # Credential authority is declared by the provider contract, regardless of
    # spelling. Inspect only its key name here so invalid startup configuration
    # cannot prevent reading an older diagnostic.
    from .native_route import protected_provider_environment_keys, NATIVE_TRANSPORT_ENV_KEYS
    protected = protected_provider_environment_keys() | set(NATIVE_TRANSPORT_ENV_KEYS)
    selected = ""
    try:
        contract = json.loads(os.environ.get("WPR_CODEX_CLI_ROUTE_CONTRACT", "{}"))
        if isinstance(contract, dict) and isinstance(contract.get("env_key"), str):
            selected = contract["env_key"]
    except (ValueError, TypeError):
        pass
    # Known service credentials are removed without exposing their names/values.
    for key, secret in os.environ.items():
        if secret and (key == selected or (len(secret) >= 6 and (
                key in protected or re.search(r"(?i)(password|secret|token|credential|api.?key)", key)))):
            text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRETS:
        text = pattern.sub(r"\1[REDACTED]", text)
    text = redact(text)
    return _HOST_PATH.sub("[PRIVATE_HOST_PATH]", text)


def _filtered_stdout(raw: str, redact) -> tuple[str, int, int]:
    blocks = []
    omitted = 0
    records = 0
    for records, line in enumerate(raw.splitlines(), 1):
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            record = None
        prefix = f"Record {records}"
        if not isinstance(record, dict):
            blocks.append(prefix + " — [non-JSON record omitted]")
            omitted += 1
            continue
        kind = record.get("type")
        if kind in {"thread.started", "turn.started", "turn.completed"}:
            blocks.append(prefix + " — " + kind)
            continue
        if kind == "turn.failed":
            error = record.get("error")
            message = error.get("message") if isinstance(error, dict) else "Turn failed"
            blocks.append(prefix + " — turn.failed\n" + _safe_text(message, redact))
            continue
        item = record.get("item")
        if kind not in {"item.started", "item.completed", "item.updated"} or not isinstance(item, dict):
            blocks.append(prefix + " — [private or unsupported event omitted]")
            omitted += 1
            continue
        item_type = item.get("type")
        heading = prefix + " — " + kind
        if item_type == "agent_message":
            blocks.append(heading + " — agent message\n" + _safe_text(item.get("text"), redact))
        elif item_type == "error":
            blocks.append(heading + " — error\n" + _safe_text(item.get("message"), redact))
        elif item_type == "command_execution":
            command = str(item.get("command") or "")
            state = str(item.get("status") or "")
            exit_code = item.get("exit_code")
            heading += " — tool activity; " + _safe_text(state, redact)
            if type(exit_code) is int:
                heading += f"; exit {exit_code}"
            if _SENSITIVE_COMMAND.search(command):
                blocks.append(heading + "\n[Instruction, credential or environment-reading command/output omitted]")
                omitted += 1
            else:
                blocks.append(heading + "\nCommand: " + _safe_text(command, redact)
                              + "\nOutput:\n" + _safe_text(item.get("aggregated_output"), redact))
        elif item_type == "file_change":
            # Do not expose patches, which can contain instructions or secrets.
            changes = item.get("changes")
            names = []
            for change in changes if isinstance(changes, list) else []:
                if isinstance(change, dict):
                    names.append(_safe_text(change.get("path"), redact))
            blocks.append(heading + " — file activity\n" + "\n".join(names)
                          + "\n[File patch content omitted]")
            omitted += 1
        else:
            # In particular, never promote reasoning, user/system prompts or
            # unknown new provider payloads into the operator report.
            blocks.append(heading + " — [private or unsupported item omitted]")
            omitted += 1
    omitted += sum("[Instruction or private-key content omitted]" in block for block in blocks)
    return "\n\n".join(blocks), records, omitted


def read_process_log(home: Path, run_id: str, *, stream: str = "stdout", cursor: int = 0,
                     limit: int = 32768, version: str = "", download: bool = False,
                     attempt_ref: str = "", redact=lambda text: text) -> dict:
    if (attempt_ref and not re.fullmatch(r"attempt-sha256-[a-f0-9]{64}", attempt_ref)) or not _ID.fullmatch(run_id) or stream not in {"stdout", "stderr"}:
        raise ValueError("Invalid run log selection")
    if cursor < 0 or not 1 <= limit <= MAX_PAGE_CHARS or (download and cursor != 0):
        raise ValueError("Process log page is out of range")
    # Each directory is opened relative to its already-open parent, without
    # following symlinks. A directory rename cannot redirect a later lookup.
    with ExitStack() as stack:
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        parts = (".glasshive-runs", run_id) + (("attempts", attempt_ref) if attempt_ref else ())
        for part in parts:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(stream + ".log", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        handle = stack.enter_context(os.fdopen(descriptor, "rb"))
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Retained run log must be a regular file without hard links")
        if before.st_size > MAX_LOG_BYTES:
            raise OverflowError("Retained log exceeds the safe read limit; no tail substituted")
        raw = handle.read(MAX_LOG_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > MAX_LOG_BYTES:
        raise OverflowError("Retained log exceeds the safe read limit")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ProcessLogChanged("Log grew during reading; reload from the start")
    fingerprint = hashlib.sha256(
        (run_id + "\0" + attempt_ref + "\0" + stream + "\0").encode() + raw
    ).hexdigest()
    if version and version != fingerprint:
        raise ProcessLogChanged("Log changed between pages; reload from the start")
    decoded = raw.decode("utf-8", errors="replace")
    if stream == "stdout":
        text, records, omitted = _filtered_stdout(decoded, redact)
    else:
        text, records = _safe_text(decoded, redact), len(decoded.splitlines())
        omitted = records if _PRIVATE_CONTENT.search(decoded) else 0
    # All filtering/redaction occurs before offsets or display limits are applied.
    if cursor > len(text):
        raise ValueError("Process log cursor is past the end")
    end = len(text) if download else min(len(text), cursor + limit)
    return {
        "run_id": run_id, "stream": stream, "version": fingerprint,
        "raw_bytes": len(raw), "record_count": records, "omitted_records": omitted,
        "cursor": cursor, "next_cursor": end if end < len(text) else None,
        "total_chars": len(text), "text": text[cursor:end],
        "scope": "Retained root process log; filtered assistant messages and tool activity. "
                 "Private instructions, reasoning, file patches and unsupported records are omitted. "
                 "Child-session transcripts are not included. Sensitive task content may be present.",
    }
