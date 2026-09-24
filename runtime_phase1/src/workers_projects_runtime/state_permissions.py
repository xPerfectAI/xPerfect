from __future__ import annotations

import os
import stat
from pathlib import Path


_ALLOWED_DIRECTORY_MODES = {0o700, 0o770}
_ALLOWED_FILE_MODES = {0o600, 0o660}


def _configured_mode(name: str, *, default: int, allowed: set[int]) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        mode = int(raw, 8)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an octal permission mode") from exc
    if mode not in allowed:
        choices = ", ".join(f"{candidate:04o}" for candidate in sorted(allowed))
        raise RuntimeError(f"{name} must be one of: {choices}")
    return mode


def state_directory_mode() -> int:
    return _configured_mode(
        "GLASSHIVE_STATE_DIR_MODE",
        default=0o700,
        allowed=_ALLOWED_DIRECTORY_MODES,
    )


def state_file_mode() -> int:
    return _configured_mode(
        "GLASSHIVE_STATE_FILE_MODE",
        default=0o600,
        allowed=_ALLOWED_FILE_MODES,
    )


def _secure_state_file_path(path: Path, *, mode: int) -> None:
    """Harden a state file without ever opening a descriptor on it.

    The state files include the live SQLite database and its ``-wal`` and ``-shm`` companions.
    POSIX releases every advisory lock a process holds on a file as soon as *any* descriptor to that
    file is closed, and SQLite keeps its WAL-index lock exactly that way. An open/fchmod/close here
    therefore let a sibling GlassHive process take the exclusive WAL-index lock and truncate the
    mapping this process was still reading, which surfaced as SIGBUS inside ``walFindFrame``.
    Path-based ``lstat``/``chmod`` keep the locks intact.
    """
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("GlassHive state path is not a file")
    if metadata.st_uid == os.geteuid():
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(path, mode, follow_symlinks=False)
        else:
            os.chmod(path, mode)
        return
    process_groups = {os.getegid(), *os.getgroups()}
    if (
        metadata.st_uid == 0
        and mode == 0o660
        and metadata.st_gid in process_groups
        and stat.S_IMODE(metadata.st_mode) == mode
    ):
        return
    raise PermissionError("GlassHive prepared state file has unexpected ownership or permissions")


def _secure_state_path(path: Path, *, mode: int, directory: bool) -> None:
    if not directory:
        _secure_state_file_path(path, mode=mode)
        return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        label = "directory" if directory else "file"
        if not expected_type(metadata.st_mode):
            raise PermissionError(f"GlassHive state path is not a {label}")
        if metadata.st_uid == os.geteuid():
            os.fchmod(descriptor, mode)
            return
        process_groups = {os.getegid(), *os.getgroups()}
        prepared_mode = 0o770 if directory else 0o660
        if (
            metadata.st_uid == 0
            and mode == prepared_mode
            and metadata.st_gid in process_groups
            and stat.S_IMODE(metadata.st_mode) == mode
        ):
            return
        raise PermissionError(
            f"GlassHive prepared state {label} has unexpected ownership or permissions"
        )
    finally:
        os.close(descriptor)


def ensure_state_directory(path: Path) -> None:
    mode = state_directory_mode()
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if os.name != "nt":
        _secure_state_path(path, mode=mode, directory=True)


def secure_state_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        _secure_state_path(path, mode=state_file_mode(), directory=False)
    except FileNotFoundError:
        # SQLite may unlink transient WAL/SHM files between lookup and chmod.
        return
