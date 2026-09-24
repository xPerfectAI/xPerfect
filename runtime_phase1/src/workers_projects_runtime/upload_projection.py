from __future__ import annotations

import json
import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .bootstrap import BOOTSTRAP_SOURCE_TOKEN_KEY, sign_bootstrap_source_path


def project_inline_image_files(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Project typed inline image bytes into the existing request file owner."""
    from .deliverables import _native_image_bytes, NATIVE_MEDIA_MAX_ITEMS, NATIVE_MEDIA_MAX_TOTAL_BYTES

    files = []
    seen = set()
    total_bytes = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {"image_url", "input_image"}:
                continue
            url = block.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if not isinstance(url, str) or not url.startswith("data:"):
                continue  # Existing uploaded-file projection owns non-inline inputs.
            header, separator, encoded = url.partition(",")
            if not separator or not header.endswith(";base64"):
                raise ValueError("Inline image requires base64 data")
            mime_type = header[5:-7]
            data, suffix = _native_image_bytes(mime_type, encoded)
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            if len(files) >= NATIVE_MEDIA_MAX_ITEMS or total_bytes + len(data) > NATIVE_MEDIA_MAX_TOTAL_BYTES:
                raise ValueError("Inline image input exceeds the native media limit")
            seen.add(digest)
            total_bytes += len(data)
            files.append({
                "scope": "workspace", "path": f"uploads/native-images/{digest}{suffix}",
                "type": mime_type, "sha256": digest, "bytes": len(data),
                "encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii"),
            })
    return files


_UPLOAD_LEDGER_PUBLIC_KEYS = (
    "file_id",
    "filename",
    "source",
    "type",
    "bytes",
    "media_group_index",
)


def _safe_upload_filename(value: object, fallback: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip() or fallback
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return safe or fallback


def _safe_virtual_ref(value: object) -> str:
    candidate = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", candidate):
        return ""
    return candidate


def _safe_source_type(value: object) -> str:
    candidate = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", candidate):
        return ""
    return candidate


def _safe_declared_type(value: object) -> str:
    candidate = str(value or "").strip().lower()
    token = r"[a-z0-9][a-z0-9.+_-]{0,63}"
    if not re.fullmatch(rf"{token}(?:/{token})?", candidate):
        return ""
    return candidate


def _upload_root_candidates() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("WPR_LIBRECHAT_UPLOADS_ROOT", "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    roots.extend(
        Path(item.strip()).expanduser()
        for item in os.environ.get("WPR_BOOTSTRAP_SOURCE_ROOTS", "").split(os.pathsep)
        if item.strip()
    )
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.fspath(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _owner_components(*values: object) -> list[str]:
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if (
            clean
            and clean not in {".", ".."}
            and "\x00" not in clean
            and "/" not in clean
            and "\\" not in clean
            and ".." not in clean
            and clean not in result
        ):
            result.append(clean)
    return result


def _trusted_virtual_source(
    value: str,
    *,
    owner_id: str | None,
    storage_owner_id: str | None,
) -> str:
    if not value.startswith("/uploads/"):
        return ""
    relative = value.split("?", 1)[0].split("/uploads/", 1)[1].strip("/")
    normalized = os.path.normpath(relative)
    if (
        not relative
        or normalized == "."
        or normalized.startswith("..")
        or os.path.isabs(normalized)
        or ".." in normalized.split(os.path.sep)
    ):
        return ""
    owners = _owner_components(owner_id, storage_owner_id)
    if owner_id or storage_owner_id:
        first = normalized.split(os.path.sep, 1)[0]
        if not owners or first not in owners:
            return ""
    for root in _upload_root_candidates():
        candidate = root / normalized
        if candidate.exists() and candidate.is_file():
            return os.fspath(candidate)
    return ""


def _owner_source_for_file_id(
    file_id: str,
    *,
    owner_id: str | None,
    storage_owner_id: str | None,
) -> str:
    """Resolve one owner-scoped upload by its durable file identity.

    Multiple Telegram album items commonly share the same display filename.  The
    file id, not that name, is the stable link to the original bytes.
    """

    clean_id = str(file_id or "").strip()
    if (
        not clean_id
        or clean_id in {".", ".."}
        or "\x00" in clean_id
        or "/" in clean_id
        or "\\" in clean_id
        or ".." in clean_id
    ):
        return ""
    matches: dict[str, Path] = {}
    for root in _upload_root_candidates():
        for owner in _owner_components(owner_id, storage_owner_id):
            owner_root = root / owner
            if not owner_root.is_dir():
                continue
            try:
                for item in owner_root.rglob("*"):
                    if not item.is_file():
                        continue
                    if item.name != clean_id and not item.name.startswith(f"{clean_id}__"):
                        continue
                    resolved = os.fspath(item.resolve())
                    matches[resolved] = item
            except OSError:
                continue
    if len(matches) != 1:
        return ""
    return next(iter(matches))


def _iter_uploads(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_uploads(item)
        return
    if not isinstance(value, dict):
        return
    if any(
        key in value
        for key in ("file_id", "filename", "filepath", "source_path", "local_path", "text")
    ):
        yield value
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_uploads(child)


def trusted_selected_files(bundle: dict[str, Any] | None) -> list[dict[str, str]] | None:
    """Read the exact owner-file authority from a trusted delegation packet.

    ``None`` means this is not a packet-governed launch. An empty list is an
    explicit instruction to project no owner files.
    """

    packet = (bundle or {}).get("viventium_delegation_packet")
    if not isinstance(packet, dict):
        return None
    raw = packet.get("selected_files", [])
    if not isinstance(raw, list) or len(raw) > 64:
        raise ValueError("viventium_delegation_packet selected_files is invalid")
    selected: list[dict[str, str]] = []
    for ordinal, item in enumerate(raw):
        if not isinstance(item, dict) or not set(item).issubset(
            {"ordinal", "name", "ref"}
        ):
            raise ValueError("viventium_delegation_packet selected_files is invalid")
        name = item.get("name")
        ref = item.get("ref")
        if (
            isinstance(item.get("ordinal"), bool)
            or item.get("ordinal") != ordinal
            or (name is None) == (ref is None)
        ):
            raise ValueError("viventium_delegation_packet selected_files is invalid")
        kind = "name" if name is not None else "ref"
        value = name if name is not None else ref
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("viventium_delegation_packet selected_files is invalid")
        if kind != "ref":
            raise ValueError(
                "viventium_delegation_packet selected_files requires a stable upload ref"
            )
        selected.append({"kind": kind, "value": value.strip()})
    return selected


def _upload_source_refs(file_obj: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in (
        "file_id",
        "id",
        "filepath",
        "path",
        "source_path",
        "local_path",
        "upload_path",
        "absolute_path",
        "url",
        "uri",
    ):
        value = str(file_obj.get(key) or "").strip()
        if value:
            refs.add(value)
            basename = value.replace("\\", "/").rsplit("/", 1)[-1]
            if "__" in basename:
                token = basename.split("__", 1)[0]
                if _safe_virtual_ref(token):
                    refs.add(token)
    return refs


def _upload_stable_key(file_obj: dict[str, Any], ordinal: int) -> tuple[str, str]:
    file_id = str(file_obj.get("file_id") or file_obj.get("id") or "").strip()
    source_ref = ""
    for key in (
        "filepath",
        "source_path",
        "local_path",
        "upload_path",
        "absolute_path",
        "url",
        "uri",
        "path",
    ):
        value = str(file_obj.get(key) or "").strip()
        if value:
            source_ref = value
            break
    if file_id or source_ref:
        return (file_id, source_ref)
    return ("ledger", str(ordinal))


def intersect_upload_records(
    value: Any,
    selected_files: list[dict[str, str]] | None,
    *,
    require_all: bool = False,
) -> list[dict[str, Any]]:
    """Intersect one upload source with packet authority before projection."""

    records = [dict(item) for item in _iter_uploads(value)]
    if selected_files is None:
        return records
    if not selected_files:
        return []
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for ordinal, record in enumerate(records):
        candidates.setdefault(_upload_stable_key(record, ordinal), record)
    selected_keys: set[tuple[str, str]] = set()
    for selector in selected_files:
        if selector.get("kind") != "ref":
            raise ValueError("Selected files require a stable upload ref")
        selected_ref = str(selector["value"])
        matches = [
            key
            for key, record in candidates.items()
            if selected_ref in _upload_source_refs(record)
        ]
        if len(matches) > 1:
            raise ValueError("Selected file identity is ambiguous")
        if not matches:
            if require_all:
                raise ValueError("Selected file identity does not match a trusted upload ref")
            continue
        selected_keys.add(matches[0])
    return [
        record
        for key, record in candidates.items()
        if key in selected_keys
    ]


def public_upload_ledger(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return public-safe metadata for already-selected upload records."""

    result: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {}
        file_id = _safe_virtual_ref(record.get("file_id") or record.get("id"))
        filename = _safe_upload_filename(
            record.get("filename") or record.get("name"), ""
        )
        source = _safe_source_type(record.get("source"))
        declared_type = _safe_declared_type(record.get("type"))
        if file_id:
            item["file_id"] = file_id
        if filename:
            item["filename"] = filename
        if source:
            item["source"] = source
        if declared_type:
            item["type"] = declared_type
        for key in ("bytes", "media_group_index"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                item[key] = value
        if item:
            result.append(item)
    return result


def _dedupe_path(filename: str, used: set[str]) -> str:
    safe = _safe_upload_filename(filename, "upload")
    stem, extension = os.path.splitext(safe)
    path = f"uploads/{safe}"
    suffix = 2
    while path in used:
        path = f"uploads/{stem}-{suffix}{extension}"
        suffix += 1
    used.add(path)
    return path


def project_upload_files(
    upload_context: dict[str, Any],
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
    storage_owner_id: str | None = None,
) -> list[dict[str, Any]]:
    """Convert one trusted request attachment ledger into ordered workspace files."""

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    used_paths: set[str] = set()
    for upload_index, file_obj in enumerate(_iter_uploads(upload_context)):
        file_id = str(file_obj.get("file_id") or file_obj.get("id") or "").strip()
        source_ref = next(
            (
                str(file_obj.get(key) or "").strip()
                for key in (
                    "filepath",
                    "path",
                    "local_path",
                    "upload_path",
                    "absolute_path",
                    "url",
                    "uri",
                )
                if str(file_obj.get(key) or "").strip()
            ),
            "",
        )
        # Legacy callers may have neither ID nor source reference. Preserve each ordered ledger
        # entry instead of collapsing an entire album into the first `(\"\", \"\")` item.
        identity = (file_id, source_ref or (f"ledger-index:{upload_index}" if not file_id else ""))
        if identity in seen:
            continue
        seen.add(identity)
        filename = _safe_upload_filename(
            file_obj.get("filename") or file_obj.get("name") or source_ref or file_id,
            f"upload-{len(result) + 1}",
        )
        path = _dedupe_path(filename, used_paths)
        metadata: dict[str, Any] = {"filename": filename}
        safe_file_id = _safe_virtual_ref(file_id)
        if safe_file_id:
            metadata["file_id"] = safe_file_id
        safe_source = _safe_source_type(file_obj.get("source"))
        if safe_source:
            metadata["source"] = safe_source
        safe_type = _safe_declared_type(file_obj.get("type"))
        if safe_type:
            metadata["type"] = safe_type
        for key in ("bytes", "media_group_index"):
            value = file_obj.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[key] = value
        source = _trusted_virtual_source(
            source_ref,
            owner_id=owner_id,
            storage_owner_id=storage_owner_id,
        )
        if not source and file_id:
            source = _owner_source_for_file_id(
                file_id,
                owner_id=owner_id,
                storage_owner_id=storage_owner_id,
            )
        if source:
            token = sign_bootstrap_source_path(source, tenant_id=tenant_id, owner_id=owner_id)
            result.append(
                {
                    "scope": "workspace",
                    "path": path,
                    "source_path": source,
                    **({BOOTSTRAP_SOURCE_TOKEN_KEY: token} if token else {}),
                    **metadata,
                }
            )
            continue
        text = file_obj.get("text")
        if isinstance(text, str) and text.strip() and filename.lower().endswith(
            (".txt", ".md", ".csv", ".json", ".jsonl", ".tsv", ".yaml", ".yml", ".xml", ".html", ".htm", ".log")
        ):
            result.append({"scope": "workspace", "path": path, "content": text, **metadata})
            continue
        if metadata:
            blocker = (
                "Original uploaded file bytes were not safely available to GlassHive. "
                "Do not substitute extracted text for this file unless the user explicitly asked for text extraction."
            )
            manifest = {
                **metadata,
                "source_status": "original_bytes_unavailable",
                "blocker": blocker,
                "extracted_text_available": bool(isinstance(text, str) and text.strip()),
            }
            result.append(
                {
                    "scope": "workspace",
                    "path": f"{path}.metadata.json",
                    "content": json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    "upload_blocker": blocker,
                    **metadata,
                }
            )
    return result


def merge_projected_upload_files(existing: Any, projected: list[dict[str, Any]]) -> list[Any]:
    files = list(existing) if isinstance(existing, list) else []
    seen_identities: set[tuple[str, str]] = set()
    seen_paths: set[str] = set()

    def identity(item: dict[str, Any]) -> tuple[str, str] | None:
        file_id = str(item.get("file_id") or item.get("id") or "").strip()
        if file_id:
            return ("file_id", file_id)
        source = str(item.get("source_path") or "").strip()
        if source:
            return ("source_path", source)
        path = str(item.get("path") or "").strip()
        return ("workspace_path", path) if path else None

    def collision_safe_path(path: str) -> str:
        if not path or path not in seen_paths:
            return path
        parent, filename = os.path.split(path)
        stem, extension = os.path.splitext(filename)
        suffix = 2
        candidate = os.path.join(parent, f"{stem}-{suffix}{extension}")
        while candidate in seen_paths:
            suffix += 1
            candidate = os.path.join(parent, f"{stem}-{suffix}{extension}")
        return candidate

    for item in files:
        if not isinstance(item, dict):
            continue
        item_identity = identity(item)
        if item_identity:
            seen_identities.add(item_identity)
        path = str(item.get("path") or "").strip()
        if path:
            seen_paths.add(path)
    for item in projected:
        item_identity = identity(item)
        if item_identity and item_identity in seen_identities:
            continue
        entry = dict(item)
        path = collision_safe_path(str(entry.get("path") or "").strip())
        if path:
            entry["path"] = path
            seen_paths.add(path)
        files.append(entry)
        if item_identity:
            seen_identities.add(item_identity)
    return files
