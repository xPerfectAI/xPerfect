"""Hardening a live SQLite state file must not release this process's advisory locks.

POSIX drops every fcntl lock a process holds on a file when any descriptor to that file is closed.
SQLite keeps its WAL-index (``-shm``) lock through such locks, so an open/fchmod/close on the file
let a sibling process take the exclusive lock and truncate the mapping this process still used,
which surfaced as SIGBUS inside ``walFindFrame``. These tests pin the descriptor-free behaviour.
"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from workers_projects_runtime import state_permissions

SQLITE_DMS_LOCK_OFFSET = 128  # UNIX_SHM_DMS in SQLite's os_unix.c


def _other_process_can_lock_exclusively(path: Path, offset: int) -> bool:
    code = (
        "import fcntl, sys\n"
        f"fd = open({str(path)!r}, 'r+b')\n"
        "try:\n"
        f"    fcntl.lockf(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1, {offset})\n"
        "except OSError:\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )
    return subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_secure_state_file_keeps_this_process_sqlite_locks(tmp_path: Path) -> None:
    shm = tmp_path / "runtime.db-shm"
    shm.write_bytes(b"\0" * 32768)
    os.chmod(shm, 0o644)

    holder = open(shm, "r+b")
    try:
        fcntl.lockf(holder.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB, 1, SQLITE_DMS_LOCK_OFFSET)
        assert not _other_process_can_lock_exclusively(shm, SQLITE_DMS_LOCK_OFFSET)

        state_permissions.secure_state_file(shm)

        assert stat.S_IMODE(shm.stat().st_mode) == 0o600
        assert not _other_process_can_lock_exclusively(
            shm, SQLITE_DMS_LOCK_OFFSET
        ), "hardening released the SQLite WAL-index lock held by this process"
    finally:
        holder.close()
    assert _other_process_can_lock_exclusively(shm, SQLITE_DMS_LOCK_OFFSET)


def test_secure_state_file_rejects_symlinked_state(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    real.write_bytes(b"")
    link = tmp_path / "runtime.db"
    link.symlink_to(real)

    with pytest.raises(PermissionError):
        state_permissions.secure_state_file(link)


def test_secure_state_file_tolerates_transient_wal_files(tmp_path: Path) -> None:
    state_permissions.secure_state_file(tmp_path / "runtime.db-wal")


def test_state_directory_hardening_still_uses_a_directory_descriptor(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    state_permissions.ensure_state_directory(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
