"""Bounded immutable identities and destination-declared path semantics."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath


class EvidenceError(ValueError):
    """A contract cannot be proved; callers must not report success."""


@dataclass(frozen=True)
class Limits:
    manifest_bytes: int = 1024 * 1024
    files: int = 1000
    file_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 256 * 1024 * 1024
    path_bytes: int = 1024
    depth: int = 20
    seconds: float = 120.0

    def __post_init__(self):
        for name in ("manifest_bytes", "files", "file_bytes", "total_bytes", "path_bytes", "depth"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise EvidenceError("Limits must be positive integers")
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, (int, float)) or not math.isfinite(self.seconds) or self.seconds <= 0:
            raise EvidenceError("Time limit must be positive and finite")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise EvidenceError("Non-finite JSON")


def loads(data: bytes, limits: Limits = Limits()):
    if len(data) > limits.manifest_bytes:
        raise EvidenceError("Manifest exceeds byte limit")
    try:
        return json.loads(data, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceError("Invalid JSON") from exc


def canonical(value) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvidenceError("Value is not canonical JSON") from exc


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise EvidenceError("Manifest fields do not match the declared schema")


def text(value, *, maximum=256):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EvidenceError("Invalid text identity")
    return value


def hash_value(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise EvidenceError("Invalid SHA-256")
    return value


def integer(value, *, maximum=2**63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise EvidenceError("Invalid bounded integer")
    return value


def relative(value: str, limits: Limits = Limits()) -> str:
    text(value, maximum=limits.path_bytes)
    parts = value.split("/")
    if len(parts) > limits.depth or any(p in {"", ".", ".."} for p in parts) or "\\" in value or ":" in value:
        raise EvidenceError("Unsafe relative path")
    if PurePosixPath(value).is_absolute():
        raise EvidenceError("Absolute path is not admitted")
    return value


@dataclass(frozen=True)
class PathPolicy:
    normalization: str = "NFC"
    case_sensitive: bool = True

    def __post_init__(self):
        if self.normalization not in {"none", "NFC"} or type(self.case_sensitive) is not bool:
            raise EvidenceError("Unsupported destination path policy")

    def key(self, path):
        value = unicodedata.normalize("NFC", path) if self.normalization == "NFC" else path
        return value if self.case_sensitive else value.casefold()


RESERVED = {"SOURCE.json", "PACKAGE.json", "VALIDATION.json", "COMMIT.json", "RESERVATION.json", "RECEIPT.json"}


def file_plan(items, policy: PathPolicy = PathPolicy(), limits: Limits = Limits()):
    if not isinstance(items, list) or not items or len(items) > limits.files:
        raise EvidenceError("File count is outside the declared bounds")
    names, identities, total = set(), set(), 0
    reserved = {policy.key(p) for p in RESERVED}
    for item in items:
        fields(item, {"logical_id", "source_id", "relative_path", "package_path", "size_bytes", "sha256"},
               {"file_id", "revision", "display_name"})
        identity = text(item["logical_id"])
        text(item["source_id"])
        for key in ("file_id", "revision", "display_name"):
            if key in item:
                text(item[key])
        if identity in identities:
            raise EvidenceError("Duplicate physical input identity")
        identities.add(identity)
        relative(item["relative_path"], limits)
        path = policy.key(relative(item["package_path"], limits))
        if path.split("/")[0] in reserved or path in names:
            raise EvidenceError("Destination path collision or reserved name")
        names.add(path)
        total += integer(item["size_bytes"], maximum=limits.file_bytes)
        hash_value(item["sha256"])
    if total > limits.total_bytes:
        raise EvidenceError("Package exceeds total byte limit")
    for path in names:
        if any("/".join(path.split("/")[:i]) in names for i in range(1, len(path.split("/")))):
            raise EvidenceError("File and directory paths overlap")
    return items


def source_event(value, policy=PathPolicy(), limits=Limits()):
    fields(value, {"schema_version", "owner_scope", "source_scope", "event_id", "revision", "inputs"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise EvidenceError("Unsupported source manifest version")
    for key in ("owner_scope", "source_scope", "event_id", "revision"):
        text(value[key])
    file_plan(value["inputs"], policy, limits)
    return value


def event_key(event):
    return digest({k: event[k] for k in ("owner_scope", "source_scope", "event_id", "revision")})


def finite_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise EvidenceError("Invalid elapsed seconds")
    return value
