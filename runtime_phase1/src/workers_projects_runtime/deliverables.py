from __future__ import annotations

import os
import base64
import binascii
import hashlib
import json
import re
import stat
import uuid
import zipfile
from pathlib import Path
from functools import lru_cache
from urllib.parse import quote


SANDBOX_WORKSPACE_MOUNT = Path(os.environ.get("WPR_SANDBOX_WORKSPACE", "/workspace/project"))
LOCALHOST_URL_PATTERN = re.compile(
    r"://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?:[:/]|$)",
    flags=re.IGNORECASE,
)
NON_DELIVERABLE_URL_HOST_PATTERN = re.compile(
    r"(^|\.)(api\.openai\.com|api\.anthropic\.com|api\.portkey\.ai|openai\.azure\.com|cognitiveservices\.azure\.com|services\.ai\.azure\.com)$",
    flags=re.IGNORECASE,
)
NON_DELIVERABLE_URL_PATH_PATTERN = re.compile(
    r"/(?:openai|anthropic|chat/completions|responses)(?:/|$)",
    flags=re.IGNORECASE,
)
NON_DELIVERABLE_FILE_NAMES = {
    ".mcp.json",
    "account web data",
    "account web data-journal",
    "agents.md",
    "claude.md",
    "codex.md",
    "chrome-default-cookies.sqlite",
    "cookies",
    "cookies-journal",
    "harness-prompt.md",
    "local state",
    "login data",
    "login data-journal",
    "project-definition.md",
    "web data",
    "web data-journal",
    "work-log.md",
}
NON_DELIVERABLE_DIR_NAMES = {
    ".codex",
    ".git",
    ".glasshive",
    ".venv",
    "__pycache__",
    "chrome-user-data",
    "browser-profile",
    "chromium-user-data",
    "glasshive-run",
    "glasshive-host-tools",
    "node_modules",
}
NON_DELIVERABLE_PATH_PREFIXES = {
    ("scheduled-prompt",),
    ("tmp",),
    ("uploads",),
}
USER_DELIVERABLE_DIR_PRIORITY = {
    "artifacts": 0,
    "reports": 1,
    "output": 2,
}
SUPPORT_ARTIFACT_DIR_NAMES = {"research", "planning", "specs", "notes"}
NATIVE_MEDIA_PREFIX = Path("artifacts/native-media")
NATIVE_MEDIA_MAX_ITEMS = 24
NATIVE_MEDIA_MAX_BYTES = 8 * 1024 * 1024
NATIVE_MEDIA_MAX_TOTAL_BYTES = 32 * 1024 * 1024
NATIVE_IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
PROFESSIONAL_ARTIFACT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".xls",
    ".xlsx",
}

OOXML_ARTIFACT_MARKERS = {
    ".docx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
}
ODF_ARTIFACT_EXTENSIONS = {".odp", ".ods", ".odt"}
OLE_ARTIFACT_EXTENSIONS = {".doc", ".ppt", ".xls"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def is_user_deliverable_relative_path(
    relative_path: Path | str, *, is_directory: bool = False
) -> bool:
    try:
        rel = Path(str(relative_path))
    except TypeError:
        return False
    if not rel.parts or rel.is_absolute():
        return False
    parts = [part for part in rel.parts if part]
    if any(part in {".", ".."} for part in parts):
        return False
    lowered_parts = [part.lower() for part in parts]
    if any(part in NON_DELIVERABLE_DIR_NAMES for part in lowered_parts):
        return False
    if any(tuple(lowered_parts[: len(prefix)]) == prefix for prefix in NON_DELIVERABLE_PATH_PREFIXES):
        return False
    if any(part.startswith(".") for part in lowered_parts):
        return False
    return is_directory or rel.name.lower() not in NON_DELIVERABLE_FILE_NAMES


def _input_versions(worker: dict) -> dict[str, dict]:
    if "_workspace_input_versions" in worker:
        # The current stable-ID projection supersedes old bootstrap paths after
        # moves/deletions. Bootstrap is only a standalone native-run fallback.
        return worker["_workspace_input_versions"]
    versions = {}
    try:
        bundle = json.loads(worker.get("bootstrap_bundle_json") or "{}")
    except (ValueError, TypeError):
        bundle = {}
    # Native run capture also receives the exact, server-owned bootstrap entries.
    for entry in bundle.get("files", []) if isinstance(bundle, dict) else []:
        if isinstance(entry, dict) and entry.get("managed_upload_id") and entry.get("sha256"):
            versions.setdefault(str(entry["path"]), {"sha256": entry["sha256"], "size_bytes": entry["bytes"]})
    return versions


def _file_identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


@lru_cache(maxsize=4096)
def _matches_input_version(root: str, relative: str, identity: tuple, digest: str) -> bool:
    """Cache only a checked immutable filesystem revision, never just a path."""
    from .workspace_files import _directory, FileAdmissionError

    try:
        with _directory(Path(root), Path(relative).parent) as parent:
            descriptor = os.open(Path(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or _file_identity(metadata) != identity:
                    return True  # Do not promote an unverified concurrent revision.
                actual = hashlib.sha256()
                while chunk := os.read(descriptor, 256 * 1024):
                    actual.update(chunk)
                if (_file_identity(os.fstat(descriptor)) != identity or
                    _file_identity(os.stat(Path(relative).name, dir_fd=parent, follow_symlinks=False)) != identity):
                    return True
                return actual.hexdigest() == digest
            finally:
                os.close(descriptor)
    except (OSError, FileAdmissionError):
        return True


def is_unmodified_user_input(worker: dict, path: Path) -> bool:
    """Only the accepted bytes of a typed input version are excluded as output.

    Native edits at the same file ID become output candidates. Filename, folder,
    timestamp recency, and model text do not decide origin.
    """
    root = Path(str(worker.get("workspace_dir") or ""))
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return False
    version = _input_versions(worker).get(relative)
    if version is None:
        return False
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return True
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return True
    if metadata.st_size != version["size_bytes"]:
        return False
    return _matches_input_version(str(root), relative, _file_identity(metadata), version["sha256"])


def candidate_html_paths(worker: dict, max_entries: int = 20) -> list[Path]:
    raw_root = str(worker.get("workspace_dir") or "").strip()
    if not raw_root:
        return []
    root = Path(raw_root)
    if not root.exists():
        return []
    candidates: list[Path] = []
    for path in sorted(root.rglob("*.html")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        if not is_user_deliverable_relative_path(rel):
            continue
        if is_unmodified_user_input(worker, path):
            continue
        candidates.append(path)
        if len(candidates) >= max_entries:
            break
    return candidates


def candidate_artifact_paths(worker: dict, max_entries: int = 50) -> list[Path]:
    raw_root = str(worker.get("workspace_dir") or "").strip()
    if not raw_root:
        return []
    root = Path(raw_root)
    if not root.exists():
        return []
    candidates: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        relative_directory = Path(directory).relative_to(root)
        subdirectories[:] = [
            name for name in subdirectories
            if is_user_deliverable_relative_path(relative_directory / name, is_directory=True)
            and relative_directory / name != NATIVE_MEDIA_PREFIX
        ]
        for name in filenames:
            rel = relative_directory / name
            if not is_user_deliverable_relative_path(rel):
                continue
            path = root / rel
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            if is_unmodified_user_input(worker, path):
                continue
            candidates.append(path)

    def priority(path: Path) -> tuple[int, float, str]:
        try:
            rel = path.relative_to(root)
        except ValueError:
            return (99, -path.stat().st_mtime, path.as_posix())
        first_part = rel.parts[0].lower() if rel.parts else ""
        directory_priority = USER_DELIVERABLE_DIR_PRIORITY.get(first_part)
        if directory_priority is None:
            directory_priority = 3 if len(rel.parts) == 1 else 4
        return (directory_priority, -path.stat().st_mtime, rel.as_posix())

    return sorted(candidates, key=priority)[:max_entries]


def _native_tool_images(stdout: str):
    """Read typed tool results, never user uploads or image-like prose."""
    calls: dict[str, str] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if event.get("type") == "assistant" and isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call_id = block.get("id")
                    if isinstance(call_id, str) and 0 < len(call_id) <= 200:
                        calls[call_id] = str(block.get("name") or "")[:200]
        results = []
        if event.get("type") == "user" and isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = block.get("tool_use_id")
                if isinstance(call_id, str) and call_id in calls:
                    results.append((call_id, calls[call_id], block.get("content")))
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "mcp_tool_call"
            and isinstance(item.get("id"), str)
            and 0 < len(item["id"]) <= 200
            and isinstance(item.get("result"), dict)
        ):
            results.append((item["id"], str(item.get("tool") or "")[:200], item["result"].get("content")))
        for call_id, tool_name, content in results:
            if not isinstance(content, list):
                continue
            for index, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "image":
                    continue
                source = block.get("source")
                if isinstance(source, dict):
                    yield call_id, tool_name, index, source.get("media_type"), source.get("data") if source.get("type") == "base64" else None
                else:
                    yield call_id, tool_name, index, block.get("mimeType"), block.get("data")


def _native_image_bytes(mime_type: object, encoded: object) -> tuple[bytes, str]:
    if not isinstance(mime_type, str) or mime_type not in NATIVE_IMAGE_SUFFIXES or not isinstance(encoded, str):
        raise ValueError("Unsupported native inline image")
    if len(encoded) > ((NATIVE_MEDIA_MAX_BYTES + 2) // 3) * 4:
        raise ValueError("Native inline image exceeds byte limit")
    data = base64.b64decode(encoded, validate=True)
    matches = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not data or len(data) > NATIVE_MEDIA_MAX_BYTES or not matches[mime_type]:
        raise ValueError("Invalid native inline image bytes")
    return data, NATIVE_IMAGE_SUFFIXES[mime_type]


def _publish_native_media(workspace: Path, relative: Path, data: bytes) -> None:
    # Reuse the existing no-follow workspace directory owner. Publish a complete new
    # inode, so a worker-created symlink/hardlink is never followed or truncated.
    from .bootstrap import _sandbox_parent_descriptor

    with _sandbox_parent_descriptor(workspace, relative) as (parent_fd, filename):
        temporary = ".native-media-" + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def capture_native_media(workspace: Path, run_id: str, stdout: str) -> dict[str, object]:
    """Preserve native image observations without choosing a user deliverable."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id):
        raise ValueError("Invalid native media run identity")
    observations = []
    seen = set()
    total_bytes = 0
    omitted = 0
    for call_id, tool_name, index, mime_type, encoded in _native_tool_images(stdout):
        try:
            data, suffix = _native_image_bytes(mime_type, encoded)
            digest = hashlib.sha256(data).hexdigest()
            identity = (call_id, index, digest)
            if identity in seen:
                continue
            seen.add(identity)
            if len(observations) >= NATIVE_MEDIA_MAX_ITEMS or total_bytes + len(data) > NATIVE_MEDIA_MAX_TOTAL_BYTES:
                omitted += 1
                continue
            relative = NATIVE_MEDIA_PREFIX / run_id / (digest + suffix)
            _publish_native_media(workspace, relative, data)
            observations.append({
                "kind": "image", "source": "native_tool_result", "artifact_ref": "artifact_sha256:" + digest,
                "run_id": run_id, "tool_call_id": call_id, "tool_name": tool_name, "content_index": index,
                "mime_type": mime_type, "bytes": len(data), "sha256": digest, "workspace_path": relative.as_posix(),
            })
            total_bytes += len(data)
        except (OSError, ValueError, binascii.Error):
            omitted += 1
    return {"observations": observations, "omitted_count": omitted}


def _native_media_snapshot(workspace: Path, relative: Path, max_bytes: int) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = [os.open(workspace, directory_flags)]
    try:
        for part in relative.parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        fd = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptors[-1])
        descriptors.append(fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > max_bytes:
            raise ValueError("Native media source is not a bounded regular file")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Native media source exceeds byte limit")
        return data
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def native_media_observations(worker: dict, run: dict) -> dict[str, object]:
    """Resolve only hash-verified observations from the exact terminal run evidence."""
    empty = {"observations": [], "omitted_count": 0}
    run_id = str(run.get("run_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id) or not worker.get("workspace_dir"):
        return empty
    workspace = Path(worker["workspace_dir"])
    try:
        evidence = json.loads(_native_media_snapshot(workspace, Path("glasshive-run/runs") / run_id / "evidence.json", 4 * 1024 * 1024))
        if evidence.get("run_id") != run_id or evidence.get("schema") != "glasshive.run.evidence.v1" or evidence.get("worker", {}).get("worker_id") != worker.get("worker_id"):
            return empty
        media = evidence.get("native_media", {})
        items = media.get("observations", [])
        if not isinstance(items, list):
            return empty
        omitted = media.get("omitted_count", 0)
        omitted = omitted if isinstance(omitted, int) and not isinstance(omitted, bool) and omitted >= 0 else 0
        verified = []
        total_bytes = 0
        for item in items[:NATIVE_MEDIA_MAX_ITEMS]:
            try:
                if not isinstance(item, dict) or item.get("run_id") != run_id or item.get("source") != "native_tool_result" or item.get("kind") != "image":
                    raise ValueError("Invalid native media observation")
                digest = item.get("sha256")
                relative = Path(str(item.get("workspace_path") or ""))
                mime_type = item.get("mime_type")
                call_id = item.get("tool_call_id")
                index = item.get("content_index")
                if (
                    not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or relative.parent != NATIVE_MEDIA_PREFIX / run_id or relative.stem != digest
                    or item.get("artifact_ref") != "artifact_sha256:" + digest
                    or not isinstance(mime_type, str) or mime_type not in NATIVE_IMAGE_SUFFIXES
                    or relative.suffix != NATIVE_IMAGE_SUFFIXES[mime_type]
                    or not isinstance(call_id, str) or not 0 < len(call_id) <= 200
                    or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 4096
                    or not isinstance(item.get("bytes"), int) or isinstance(item.get("bytes"), bool)
                ):
                    raise ValueError("Invalid native media artifact identity")
                data = _native_media_snapshot(workspace, relative, NATIVE_MEDIA_MAX_BYTES)
                if len(data) != item.get("bytes") or hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("Native media artifact changed")
                if total_bytes + len(data) > NATIVE_MEDIA_MAX_TOTAL_BYTES:
                    raise ValueError("Native media observations exceed byte limit")
                total_bytes += len(data)
                tool_name = item.get("tool_name")
                verified.append({
                    "kind": "image", "source": "native_tool_result", "artifact_ref": "artifact_sha256:" + digest,
                    "run_id": run_id, "tool_call_id": call_id, "tool_name": tool_name[:200] if isinstance(tool_name, str) else "",
                    "content_index": index, "mime_type": mime_type, "bytes": len(data), "sha256": digest,
                    "workspace_path": relative.as_posix(),
                })
            except (OSError, ValueError):
                omitted += 1
        return {"observations": verified, "omitted_count": omitted + max(0, len(items) - NATIVE_MEDIA_MAX_ITEMS)}
    except (OSError, ValueError, TypeError, AttributeError):
        return empty


def is_valid_professional_artifact(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in PROFESSIONAL_ARTIFACT_EXTENSIONS:
        return False
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    if suffix == ".pdf":
        return header.startswith(b"%PDF-")
    if suffix == ".rtf":
        return header.startswith(b"{\\rtf")
    if suffix in OLE_ARTIFACT_EXTENSIONS:
        return header.startswith(OLE_MAGIC)
    if suffix in OOXML_ARTIFACT_MARKERS:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False
        return "[Content_Types].xml" in names and OOXML_ARTIFACT_MARKERS[suffix] in names
    if suffix in ODF_ARTIFACT_EXTENSIONS:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False
        return "mimetype" in names and "content.xml" in names
    return True


def workspace_browser_url(path: Path, worker: dict) -> str | None:
    raw_root = str(worker.get("workspace_dir") or "").strip()
    if not raw_root:
        return None
    root = Path(raw_root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if str(worker.get("execution_mode") or "docker") == "host":
        return None
    container_path = (SANDBOX_WORKSPACE_MOUNT / rel.as_posix()).as_posix()
    return f"file://{quote(container_path, safe='/:')}"


def extract_urls(*texts: str) -> list[str]:
    combined = "\n".join(texts)
    seen: set[str] = set()
    matches: list[str] = []
    for raw in re.findall(r"https?://[^\s<>'\"`]+", combined):
        cleaned = raw.rstrip(").,;")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        matches.append(cleaned)
    return matches


def is_deliverable_url(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:
        return False
    if NON_DELIVERABLE_URL_HOST_PATTERN.search(host):
        return False
    if NON_DELIVERABLE_URL_PATH_PATTERN.search(parsed.path or "") and (
        "azure" in host.lower() or "openai" in host.lower() or "anthropic" in host.lower()
    ):
        return False
    return True


def deliverable_payload(
    worker: dict,
    latest_run: dict | None,
    latest_output: str,
    stdout_text: str = "",
    stderr_text: str = "",
) -> dict[str, object] | None:
    execution_mode = str(worker.get("execution_mode") or "docker")
    artifact_candidates = candidate_artifact_paths(worker)
    valid_artifact_candidates = [
        path
        for path in artifact_candidates
        if path.suffix.lower() not in PROFESSIONAL_ARTIFACT_EXTENSIONS
        or is_valid_professional_artifact(path)
    ]
    preferred_artifact = next(
        (path for path in valid_artifact_candidates if path.suffix.lower() in PROFESSIONAL_ARTIFACT_EXTENSIONS),
        None,
    )
    if preferred_artifact is not None:
        raw_root = str(worker.get("workspace_dir") or "").strip()
        try:
            rel = preferred_artifact.relative_to(Path(raw_root))
        except ValueError:
            rel = Path(preferred_artifact.name)
        return {
            "kind": "file",
            "state": "ready" if latest_run else "available",
            "source": "workspace_file",
            "label": preferred_artifact.name,
            "preferred_surface": "download",
            "workspace_path": rel.as_posix(),
        }

    html_candidates = candidate_html_paths(worker)
    preferred_html = next((path for path in html_candidates if path.name.lower() == "index.html"), None)
    if preferred_html is None and html_candidates:
        preferred_html = html_candidates[0]

    urls = [url for url in extract_urls(latest_output, stdout_text, stderr_text) if is_deliverable_url(url)]
    local_url = next((url for url in urls if LOCALHOST_URL_PATTERN.search(url)), None)

    if preferred_html is not None:
        browser_url = workspace_browser_url(preferred_html, worker)
        raw_root = str(worker.get("workspace_dir") or "").strip()
        try:
            workspace_path = preferred_html.relative_to(Path(raw_root)).as_posix() if raw_root else preferred_html.name
        except ValueError:
            workspace_path = preferred_html.name
        payload: dict[str, object] = {
            "kind": "webpage",
            "state": "ready" if latest_run else "available",
            "source": "workspace_html",
            "label": preferred_html.name,
            "preferred_surface": "desktop",
            "workspace_path": workspace_path,
        }
        if browser_url:
            payload["browser_url"] = browser_url
        else:
            payload["browser_url_available"] = False
        return payload

    if valid_artifact_candidates:
        artifact = valid_artifact_candidates[0]
        raw_root = str(worker.get("workspace_dir") or "").strip()
        try:
            rel = artifact.relative_to(Path(raw_root))
        except ValueError:
            rel = Path(artifact.name)
        return {
            "kind": "file",
            "state": "ready" if latest_run else "available",
            "source": "workspace_file",
            "label": artifact.name,
            "preferred_surface": "download",
            "workspace_path": rel.as_posix(),
        }

    if local_url:
        if execution_mode == "host":
            return {
                "kind": "webpage",
                "state": "ready" if latest_run else "available",
                "source": "run_url",
                "label": "URL output available",
                "preferred_surface": "desktop",
                "workspace_path": None,
                "browser_url_available": False,
            }
        return {
            "kind": "webpage",
            "state": "ready" if latest_run else "available",
            "source": "run_url",
            "label": local_url,
            "browser_url": local_url,
            "preferred_surface": "desktop",
            "workspace_path": None,
        }

    return None
