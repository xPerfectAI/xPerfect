"""An Expert's own picture: one 192 by 192 PNG per Expert (its lineage), kept in this install's state rather than in
its versions, and applied to its Teams app as the colour icon (Teams wants 192 by 192 there)."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
from pathlib import Path

SIZE = 192
MAX_BYTES = 300_000
_PNG = b"\x89PNG\r\n\x1a\n"
_LINEAGE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _folder() -> Path:
    explicit = os.environ.get("XPERFECT_STATE_DIR", "").strip()
    base = Path(explicit) if explicit else Path(os.environ.get("WPR_DB_PATH", "")).resolve().parent.parent
    return base / "data" / "expert-pictures"


def _path(lineage_id: str) -> Path:
    if not _LINEAGE.match(str(lineage_id or "")):
        raise ValueError("unknown Expert")
    return _folder() / f"{lineage_id}.png"


def problem(data: bytes) -> str:
    """Why these bytes are not an Expert's picture, or '' when they are."""
    if len(data) > MAX_BYTES:
        return "The picture is too large: at most 300 KB."
    if not data.startswith(_PNG) or data[12:16] != b"IHDR" or len(data) < 33:
        return "Send the picture as a PNG."
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (SIZE, SIZE):
        return f"The picture must be {SIZE} by {SIZE} pixels."
    return ""


def save(lineage_id: str, data: bytes) -> str:
    path = _path(lineage_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(path)
    return digest(data)


def load(lineage_id: str) -> bytes | None:
    try:
        path = _path(lineage_id)
    except ValueError:
        return None
    return path.read_bytes() if path.is_file() and not path.is_symlink() else None


def remove(lineage_id: str) -> None:
    _path(lineage_id).unlink(missing_ok=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()
