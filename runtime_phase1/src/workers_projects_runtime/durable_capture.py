from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from threading import RLock
from typing import Iterable


OPERATION_TOKEN_FIELD = "_viventium_operation_token"
BROKER_TOKEN_FIELD = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
OPERATION_TOKEN_REDACTION = "[REDACTED_OPERATION_TOKEN]"
BROKER_TOKEN_REDACTION = "[REDACTED_BROKER_TOKEN]"

_STRUCTURAL_SECRET_FIELDS = {
    OPERATION_TOKEN_FIELD: OPERATION_TOKEN_REDACTION,
    BROKER_TOKEN_FIELD: BROKER_TOKEN_REDACTION,
}
_TEXT_ARTIFACT_SUFFIXES = {".json", ".jsonl", ".log", ".ndjson", ".txt"}
_JSON_ARTIFACT_MARKERS = {
    "activity",
    "active-run",
    "event",
    "evidence",
    "rollout",
    "session",
    "stderr",
    "stdout",
    "transcript",
}
_MAX_EMBEDDED_JSON_CHARS = 2 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_MIN_EXACT_SECRET_CHARS = 16
_MAX_EXACT_SECRET_CHARS = 8192


class DurableCaptureError(RuntimeError):
    pass


class DurableSecretScrubber:
    """Scrub exact invocation secrets learned from reserved transcript fields.

    This deliberately does not attempt to recognize arbitrary base64, JWTs, or
    bearer-looking strings. The operation token is learned from its reserved
    provider-tool field, and invocation-local broker bearers are supplied by the
    caller. Once learned, only exact values are removed from later transcript
    text. A shared instance is safe for concurrent stdout/stderr drain threads.
    """

    def __init__(self, *, exact_values: Iterable[str] = ()) -> None:
        self._lock = RLock()
        self._exact_values: dict[str, str] = {}
        for value in exact_values:
            self._remember(value, BROKER_TOKEN_REDACTION)

    def _remember(self, value: object, replacement: str) -> None:
        secret = str(value or "")
        if (
            len(secret) < _MIN_EXACT_SECRET_CHARS
            or len(secret) > _MAX_EXACT_SECRET_CHARS
            or secret in _STRUCTURAL_SECRET_FIELDS.values()
        ):
            return
        self._exact_values[secret] = replacement

    @staticmethod
    def _embedded_json(value: str) -> object | None:
        stripped = value.strip()
        if (
            not stripped
            or len(stripped) > _MAX_EMBEDDED_JSON_CHARS
            or stripped[0] not in "[{"
            or stripped[-1] not in "]}"
        ):
            return None
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError):
            return None
        return parsed if isinstance(parsed, (dict, list)) else None

    def _discover_value(self, value: object, *, depth: int = 0) -> None:
        if depth > 64:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                field = str(key)
                replacement = _STRUCTURAL_SECRET_FIELDS.get(field)
                if replacement is not None:
                    self._remember(child, replacement)
                self._discover_value(child, depth=depth + 1)
            return
        if isinstance(value, list):
            for child in value:
                self._discover_value(child, depth=depth + 1)
            return
        if isinstance(value, str):
            embedded = self._embedded_json(value)
            if embedded is not None:
                self._discover_value(embedded, depth=depth + 1)

    def discover_text(self, text: str) -> None:
        """Learn secrets from reserved fields without changing the input."""

        with self._lock:
            for line in str(text).splitlines():
                parsed = self._embedded_json(line)
                if parsed is not None:
                    self._discover_value(parsed)

    def _replace_exact_values(self, value: str) -> str:
        result = value
        # Longest first keeps a shorter exact value from obscuring a longer one.
        for secret, replacement in sorted(
            self._exact_values.items(), key=lambda item: len(item[0]), reverse=True
        ):
            result = result.replace(secret, replacement)
        return result

    def _scrub_value(self, value: object, *, depth: int = 0) -> object:
        if depth > 64:
            return value
        if isinstance(value, dict):
            scrubbed: dict[object, object] = {}
            for key, child in value.items():
                replacement = _STRUCTURAL_SECRET_FIELDS.get(str(key))
                if replacement is not None:
                    self._remember(child, replacement)
                    scrubbed[key] = replacement
                else:
                    scrubbed[key] = self._scrub_value(child, depth=depth + 1)
            return scrubbed
        if isinstance(value, list):
            return [self._scrub_value(child, depth=depth + 1) for child in value]
        if isinstance(value, str):
            embedded = self._embedded_json(value)
            if embedded is not None:
                scrubbed = self._scrub_value(embedded, depth=depth + 1)
                value = json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":"))
            return self._replace_exact_values(value)
        return value

    def scrub_text(self, text: str) -> str:
        """Return transcript text with structural and learned exact secrets removed."""

        original = str(text)
        if not original:
            return original
        with self._lock:
            self.discover_text(original)
            pieces = original.splitlines(keepends=True)
            scrubbed_pieces: list[str] = []
            for piece in pieces:
                line = piece.rstrip("\r\n")
                ending = piece[len(line) :]
                parsed = self._embedded_json(line)
                if parsed is not None:
                    scrubbed = self._scrub_value(parsed)
                    rendered = (
                        json.dumps(
                            scrubbed, ensure_ascii=False, separators=(",", ":")
                        )
                        if scrubbed != parsed
                        else self._replace_exact_values(line)
                    )
                else:
                    rendered = self._replace_exact_values(line)
                scrubbed_pieces.append(rendered + ending)
            return "".join(scrubbed_pieces)


def _is_harness_text_artifact(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in _TEXT_ARTIFACT_SUFFIXES:
        return False
    if suffix in {".log", ".jsonl", ".ndjson"}:
        return True
    name = path.name.lower()
    return any(marker in name for marker in _JSON_ARTIFACT_MARKERS)


def _candidate_artifacts(roots: Iterable[Path]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for raw_root in roots:
        root = Path(raw_root)
        if not root.exists() or root.is_symlink():
            continue
        paths = (root,) if root.is_file() else root.rglob("*")
        for path in paths:
            try:
                if (
                    path in seen
                    or path.is_symlink()
                    or not path.is_file()
                    or not _is_harness_text_artifact(path)
                ):
                    continue
                if path.stat().st_size > _MAX_ARTIFACT_BYTES:
                    raise DurableCaptureError(
                        f"Harness transcript exceeds the safe scrub limit: {path.name}"
                    )
            except FileNotFoundError:
                continue
            except OSError:
                raise
            seen.add(path)
            candidates.append(path)
    return sorted(candidates, key=lambda item: str(item))


def _read_text_artifact(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _replace_private_text_file(path: Path, text: str) -> None:
    current_mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".scrub", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(current_mode & 0o700 or 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def scrub_durable_text_artifacts(
    roots: Iterable[Path], *, scrubber: DurableSecretScrubber
) -> list[Path]:
    """Two-pass scrub of explicitly harness-owned transcript/state roots only."""

    candidates = _candidate_artifacts(roots)
    for path in candidates:
        text = _read_text_artifact(path)
        if text is None:
            continue
        scrubber.discover_text(text)

    changed: list[Path] = []
    for path in candidates:
        original = _read_text_artifact(path)
        if original is None:
            continue
        scrubbed = scrubber.scrub_text(original)
        if scrubbed == original:
            continue
        # A durable transcript that cannot be sanitized is a hard security
        # failure. Let the owning run fail closed instead of reporting success
        # while leaving a known capability in local state.
        _replace_private_text_file(path, scrubbed)
        changed.append(path)
    return changed
