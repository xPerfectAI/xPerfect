"""Files-owned portable Trash verification and post-install Undo binding."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from pathlib import Path

from .workspace_file_mutations import _revision
from .workspace_files import _relative


_TRASH_PATH = re.compile(r"\.xperfect-file-trash/item_[0-9a-f]{32}\Z")
_PENDING_PREFIX = "restore_pending:"


def _scope_root(connection: sqlite3.Connection, operation: dict) -> Path:
    rows = [dict(row) for row in connection.execute(
        "SELECT worker_id,workspace_id,workspace_dir FROM workers WHERE tenant_id=? AND owner_id=?",
        (operation["tenant_id"], operation["owner_id"]),
    )]
    candidates = {str(row["workspace_dir"] or "") for row in rows
                  if operation["workspace_key"] in {
                      row["worker_id"], row["workspace_id"], "member:" + row["worker_id"]
                  }}
    if len(candidates) != 1 or not next(iter(candidates)):
        raise ValueError("GlassHive Files Trash scope has no unique workspace owner")
    return Path(next(iter(candidates)))


def _trash_relative(operation: dict) -> Path:
    value = str(operation["target_path"])
    if not _TRASH_PATH.fullmatch(value):
        raise ValueError("GlassHive Files Trash target path is invalid")
    _relative(str(operation["original_path"]))
    return Path(value)


def _portable_digest(path: Path) -> str:
    """Hash all recoverable bytes and paths, never source inode or host timestamp."""
    digest = hashlib.sha256()
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def visit(parent: int, name: str, relative: Path) -> None:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError as exc:
            raise ValueError("GlassHive Files Trash contains an unsafe item") from exc
        try:
            before = os.fstat(descriptor)
            if stat.S_ISDIR(before.st_mode):
                digest.update(b"D\0" + relative.as_posix().encode() + b"\0")
                for child in sorted(os.listdir(descriptor)):
                    visit(descriptor, child, relative / child)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                digest.update(b"F\0" + relative.as_posix().encode() + b"\0")
                digest.update(b"X" if before.st_mode & stat.S_IXUSR else b"-")
                digest.update(str(before.st_size).encode() + b"\0")
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
            else:
                raise ValueError("GlassHive Files Trash contains an unsafe item")
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise ValueError("GlassHive Files Trash changed during continuity")
        finally:
            os.close(descriptor)

    if path.parent != path.parent.resolve(strict=True):
        raise ValueError("GlassHive Files Trash parent is unsafe")
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(parent, path.name, Path("."))
    finally:
        os.close(parent)
    return digest.hexdigest()


def project_applied_deletes(source_database: Path, archive_store, archive_root: Path) -> int:
    """Verify source/copy equality, then remove only source inode authority."""
    source = sqlite3.connect(f"{source_database.resolve().as_uri()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        with archive_store._connect() as target:
            operations = [dict(row) for row in target.execute(
                "SELECT * FROM workspace_file_operations WHERE kind='delete' AND state='applied'"
            )]
            for operation in operations:
                relative = _trash_relative(operation)
                source_root = _scope_root(source, operation)
                archive_relative = _scope_root(target, operation)
                if archive_relative.is_absolute() or ".." in archive_relative.parts:
                    raise ValueError("GlassHive Files Trash archive scope is unsafe")
                original = source_root / relative
                copied = archive_root / archive_relative / relative
                parent = os.open(original.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    _, metadata = _revision(parent, original.name)
                finally:
                    os.close(parent)
                if (metadata.st_dev, metadata.st_ino) != (operation["device"], operation["inode"]):
                    raise ValueError("GlassHive Files Trash source identity changed")
                portable = _portable_digest(original)
                if _portable_digest(copied) != portable:
                    raise ValueError("GlassHive Files Trash copy disagrees")
                target.execute(
                    "UPDATE workspace_file_operations SET device=-1,inode=-1,revision='',error=? "
                    "WHERE operation_id=?",
                    (_PENDING_PREFIX + portable, operation["operation_id"]),
                )
            return len(operations)
    finally:
        source.close()


def require_inert_applied_deletes(connection: sqlite3.Connection, archive_root: Path) -> None:
    for row in connection.execute(
        "SELECT * FROM workspace_file_operations WHERE kind='delete' AND state='applied'"
    ):
        operation = dict(row)
        marker = str(operation["error"] or "")
        if (operation["device"] != -1 or operation["inode"] != -1 or operation["revision"]
                or not re.fullmatch(r"restore_pending:[0-9a-f]{64}", marker)):
            raise ValueError("GlassHive Files Trash requires a portable restore binding")
        relative = _scope_root(connection, operation)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("GlassHive Files Trash archive scope is unsafe")
        if _portable_digest(archive_root / relative / _trash_relative(operation)) != marker[len(_PENDING_PREFIX):]:
            raise ValueError("GlassHive Files Trash archive payload disagrees")


def bind_restored_deletes(store) -> int:
    """Call after target files are installed, while the parent holds writers stopped."""
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        operations = [dict(row) for row in connection.execute(
            "SELECT * FROM workspace_file_operations WHERE kind='delete' AND state='applied'"
        )]
        rebound = 0
        for operation in operations:
            marker = str(operation["error"] or "")
            pending = bool(re.fullmatch(r"restore_pending:[0-9a-f]{64}", marker))
            if not pending and (marker or operation["device"] < 0 or operation["inode"] < 0):
                raise ValueError("GlassHive Files Trash restore binding is missing")
            workspace = _scope_root(connection, operation)
            if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
                raise ValueError("GlassHive Files Trash target workspace is unavailable")
            target = workspace / _trash_relative(operation)
            if pending and _portable_digest(target) != marker[len(_PENDING_PREFIX):]:
                raise ValueError("GlassHive Files Trash target payload disagrees")
            parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                revision, metadata = _revision(parent, target.name)
            finally:
                os.close(parent)
            if pending:
                connection.execute(
                    "UPDATE workspace_file_operations SET device=?,inode=?,revision=?,error='' WHERE operation_id=?",
                    (metadata.st_dev, metadata.st_ino, revision, operation["operation_id"]),
                )
                rebound += 1
            elif (metadata.st_dev, metadata.st_ino, revision) != (
                operation["device"], operation["inode"], operation["revision"]
            ):
                raise ValueError("GlassHive Files Trash restored identity changed")
        return rebound
