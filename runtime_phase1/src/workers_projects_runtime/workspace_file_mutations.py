"""Recoverable same-filesystem moves and private trash for workspace entries."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .workspace_files import FileAdmissionError, _directory, _relative, _snapshot_fd


TRASH = ".xperfect-file-trash"


def ensure_file_mutation_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS workspace_file_operations (
        operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        workspace_key TEXT NOT NULL, file_id TEXT NOT NULL, kind TEXT NOT NULL,
        source_path TEXT NOT NULL, target_path TEXT NOT NULL, original_path TEXT NOT NULL,
        device INTEGER NOT NULL, inode INTEGER NOT NULL, is_dir INTEGER NOT NULL,
        state TEXT NOT NULL, undo_of TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        revision TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL)""")
    if "revision" not in {
        row[1] for row in conn.execute("PRAGMA table_info(workspace_file_operations)")
    }:
        conn.execute(
            "ALTER TABLE workspace_file_operations ADD COLUMN revision TEXT NOT NULL DEFAULT ''"
        )
    conn.execute("""CREATE INDEX IF NOT EXISTS workspace_file_operation_scope
        ON workspace_file_operations(tenant_id, owner_id, workspace_key, state)""")


def _rename_no_replace(
    source_parent: int, source: str, target_parent: int, target: str
) -> None:
    """Use the OS exclusive rename primitive for files and nonempty directories."""
    libc = ctypes.CDLL(None, use_errno=True)
    name, flags = (
        ("renameatx_np", 0x00000004) if sys.platform == "darwin" else ("renameat2", 1)
    )
    function = getattr(libc, name, None)
    if function is None:
        raise FileAdmissionError(
            "This filesystem cannot safely move files or folders", 503
        )
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(
        source_parent, os.fsencode(source), target_parent, os.fsencode(target), flags
    ):
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileAdmissionError(
                "Destination already exists; choose another name", 409
            )
        if code in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}:
            raise FileAdmissionError(
                "This filesystem cannot safely move files or folders", 503
            )
        raise FileAdmissionError(
            "File location changed or cannot be moved; refresh and retry", 409
        )


@contextmanager
def _locked(root: Path):
    try:
        with _directory(root) as parent:
            descriptor = os.open(
                ".xperfect-files.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
    except OSError as exc:
        raise FileAdmissionError("Workspace file lock is unavailable", 409) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FileAdmissionError("Workspace file lock is invalid", 409)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _identity(parent: int, name: str):
    try:
        value = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino


def _audit_tree(parent: int, name: str, relative: Path) -> None:
    """Do not move private runtime state, links, or special files inside folders."""
    _relative(relative.as_posix())
    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_nlink != 1:
            raise FileAdmissionError("Linked files cannot be changed from Files", 403)
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise FileAdmissionError(
            "Linked or special files cannot be changed from Files", 403
        )
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        with os.scandir(child) as entries:
            for entry in entries:
                _audit_tree(child, entry.name, relative / entry.name)
    finally:
        os.close(child)


def directory_revision(parent: int, name: str) -> str:
    """Fingerprint nested metadata without following links or reading file data."""
    digest = hashlib.sha256()

    def visit(directory, entry_name):
        before = os.stat(entry_name, dir_fd=directory, follow_symlinks=False)
        digest.update(
            json.dumps(
                (
                    entry_name,
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ),
                separators=(",", ":"),
            ).encode()
        )
        if stat.S_ISDIR(before.st_mode):
            child = os.open(
                entry_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            try:
                for nested in sorted(os.listdir(child)):
                    visit(child, nested)
                after = os.fstat(child)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise FileAdmissionError(
                        "Folder changed; refresh before trying again", 409
                    )
            finally:
                os.close(child)
        digest.update(b"\0")

    try:
        visit(parent, name)
    except OSError as exc:
        raise FileAdmissionError(
            "Folder changed; refresh before trying again", 409
        ) from exc
    return digest.hexdigest()


def _revision(parent: int, name: str) -> tuple[str, os.stat_result]:
    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISDIR(metadata.st_mode):
        return directory_revision(parent, name), metadata
    descriptor, value, _ = _snapshot_fd(parent, name)
    try:
        return value, os.fstat(descriptor)
    finally:
        os.close(descriptor)


class FileMutations:
    def __init__(self, files, worker: dict):
        worker = files._scope_worker(worker)
        self.files = files
        self.worker = worker
        self.root = files._root(worker)
        self.lock_root = files.control_lock_root(worker)
        self.scope = (
            worker["tenant_id"],
            worker["owner_id"],
            files._workspace_key(worker),
        )

    def _idle(self):
        # Only bees admitted to the same physical scope can block this mutation.
        if self.files.scope_has_active_run(self.worker):
            raise FileAdmissionError(
                "The worker is editing this workspace; retry when it is idle", 409
            )

    def _recover(self):
        with self.files.store._connect() as conn:
            pending = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM workspace_file_operations WHERE tenant_id=? AND owner_id=? AND workspace_key=? AND state='prepared' ORDER BY created_at",
                    self.scope,
                )
            ]
        for operation in pending:
            self._finish(operation, recovering=True)

    def recover(self):
        with _locked(self.lock_root):
            self._recover()

    def _apply_entries(self, conn, operation):
        source, target = operation["source_path"], operation["target_path"]
        deleted = 1 if operation["kind"] == "delete" else 0
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM workspace_file_entries WHERE tenant_id=? AND owner_id=? AND workspace_key=?",
                self.scope,
            )
            if row["path"] == source or row["path"].startswith(source + "/")
        ]
        for row in rows:
            destination = target + row["path"][len(source) :]
            # A native process may have removed a previously indexed destination.
            # Keep its ID as a tombstone instead of reusing or deleting that ID.
            collision = conn.execute(
                "SELECT file_id FROM workspace_file_entries WHERE tenant_id=? AND owner_id=? AND workspace_key=? AND path=? AND file_id!=?",
                (*self.scope, destination, row["file_id"]),
            ).fetchone()
            if collision:
                conn.execute(
                    "UPDATE workspace_file_entries SET path=?,deleted=1 WHERE file_id=?",
                    (f"{TRASH}/stale-{collision['file_id']}", collision["file_id"]),
                )
            conn.execute(
                "UPDATE workspace_file_entries SET path=?,deleted=? WHERE file_id=?",
                (destination, deleted, row["file_id"]),
            )
        if operation["kind"] == "restore":
            conn.execute(
                "UPDATE workspace_file_operations SET state='undone' WHERE operation_id=? AND state='applied'",
                (operation["undo_of"],),
            )
        conn.execute(
            "UPDATE workspace_file_operations SET state='applied',error='' WHERE operation_id=?",
            (operation["operation_id"],),
        )

    def _finish(self, operation: dict, *, recovering: bool = False):
        source, target = Path(operation["source_path"]), Path(operation["target_path"])
        expected = operation["device"], operation["inode"]
        try:
            with (
                _directory(self.root, source.parent) as source_parent,
                _directory(self.root, target.parent) as target_parent,
            ):
                at_source = _identity(source_parent, source.name)
                at_target = _identity(target_parent, target.name)
                if at_target != expected:
                    if at_source != expected:
                        raise FileAdmissionError(
                            "File changed before the move completed; refresh to review it",
                            409,
                        )
                    self._idle()
                    if (
                        operation.get("revision")
                        and _revision(source_parent, source.name)[0]
                        != operation["revision"]
                    ):
                        raise FileAdmissionError(
                            "File changed; refresh before trying again", 409
                        )
                    _rename_no_replace(
                        source_parent, source.name, target_parent, target.name
                    )
                    os.fsync(source_parent)
                    if target_parent != source_parent:
                        os.fsync(target_parent)
                with self.files.store._connect() as conn:
                    conn.execute("PRAGMA synchronous=FULL")
                    conn.execute("BEGIN IMMEDIATE")
                    self._apply_entries(conn, operation)
        except FileAdmissionError as exc:
            # A known conflict before a successful rename is safe to abandon.
            # Unexpected interruption retains prepared intent for the next reload.
            with self.files.store._connect() as conn:
                conn.execute(
                    "UPDATE workspace_file_operations SET state='aborted',error=? WHERE operation_id=? AND state='prepared'",
                    (str(exc), operation["operation_id"]),
                )
            if not recovering:
                raise

    def _prepare(
        self, *, file_id, kind, source, target, original, metadata, revision, undo_of=""
    ):
        operation = {
            "operation_id": "fop_" + uuid.uuid4().hex,
            "tenant_id": self.scope[0],
            "owner_id": self.scope[1],
            "workspace_key": self.scope[2],
            "file_id": file_id,
            "kind": kind,
            "source_path": source.as_posix(),
            "target_path": target.as_posix(),
            "original_path": original,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "is_dir": int(stat.S_ISDIR(metadata.st_mode)),
            "state": "prepared",
            "undo_of": undo_of,
            "revision": revision,
            "created_at": time.time(),
        }
        with self.files.store._connect() as conn:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO workspace_file_operations (
                operation_id,tenant_id,owner_id,workspace_key,file_id,kind,source_path,target_path,original_path,
                device,inode,is_dir,state,undo_of,revision,created_at
                ) VALUES (:operation_id,:tenant_id,:owner_id,:workspace_key,:file_id,:kind,:source_path,:target_path,:original_path,
                :device,:inode,:is_dir,:state,:undo_of,:revision,:created_at)""",
                operation,
            )
        return operation

    def mutate(self, file_id: str, *, revision: str, path: str | None, delete: bool):
        with _locked(self.lock_root):
            self._recover()
            self._idle()
            with self.files.store._connect() as conn:
                entry = self.files._entry(conn, self.worker, file_id)
            source = _relative(entry["path"])
            with _directory(self.root, source.parent) as source_parent:
                current, metadata = _revision(source_parent, source.name)
                if current != revision:
                    raise FileAdmissionError(
                        "File changed; refresh before trying again", 409
                    )
                _audit_tree(source_parent, source.name, source)
                if delete:
                    trash_name = "item_" + uuid.uuid4().hex
                    target = Path(TRASH) / trash_name
                    with _directory(self.root, Path(TRASH), create=True):
                        pass
                else:
                    target = _relative(str(path or ""))
                    if target == source:
                        return {"file_id": file_id, "path": source.as_posix()}
                    if target.is_relative_to(source):
                        raise FileAdmissionError(
                            "A folder cannot move inside itself", 409
                        )
                # Verify the destination parent before durable intent is recorded.
                with _directory(self.root, target.parent) as target_parent:
                    if self.files._exists_at(target_parent, target.name):
                        raise FileAdmissionError(
                            "Destination already exists; choose another name", 409
                        )
                operation = self._prepare(
                    file_id=file_id,
                    kind="delete" if delete else "move",
                    source=source,
                    target=target,
                    original=source.as_posix(),
                    metadata=metadata,
                    revision=current,
                )
            self._finish(operation)
            if delete:
                return {
                    "file_id": file_id,
                    "state": "deleted",
                    "undo_id": operation["operation_id"],
                }
            return {"file_id": file_id, "path": target.as_posix()}

    def undo(self, file_id: str, undo_id: str):
        with _locked(self.lock_root):
            self._recover()
            self._idle()
            with self.files.store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM workspace_file_operations WHERE operation_id=? AND file_id=? AND tenant_id=? AND owner_id=? AND workspace_key=? AND kind='delete'",
                    (undo_id, file_id, *self.scope),
                ).fetchone()
            if row is None:
                raise FileAdmissionError("Removed file not found", 404)
            removed = dict(row)
            if removed["state"] == "undone":
                return {
                    "file_id": file_id,
                    "state": "restored",
                    "path": removed["original_path"],
                }
            if removed["state"] != "applied":
                raise FileAdmissionError("This removal cannot be restored", 409)
            source, target = (
                Path(removed["target_path"]),
                _relative(removed["original_path"]),
            )
            with (
                _directory(self.root, source.parent) as source_parent,
                _directory(self.root, target.parent) as target_parent,
            ):
                current, metadata = _revision(source_parent, source.name)
                if (metadata.st_dev, metadata.st_ino) != (
                    removed["device"],
                    removed["inode"],
                ):
                    raise FileAdmissionError(
                        "Removed file changed; restore is unavailable", 409
                    )
                if self.files._exists_at(target_parent, target.name):
                    raise FileAdmissionError(
                        "Destination already exists; move it before restoring this file",
                        409,
                    )
                # Audit using the public destination so internal trash is never
                # treated as an ordinary user-editable path.
                _audit_tree(source_parent, source.name, target)
                operation = self._prepare(
                    file_id=file_id,
                    kind="restore",
                    source=source,
                    target=target,
                    original=target.as_posix(),
                    metadata=metadata,
                    revision=current,
                    undo_of=undo_id,
                )
            self._finish(operation)
            return {"file_id": file_id, "state": "restored", "path": target.as_posix()}

    def trash(self):
        with _locked(self.lock_root):
            self._recover()
            with self.files.store._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM workspace_file_operations WHERE tenant_id=? AND owner_id=? AND workspace_key=? AND kind='delete' AND state='applied' ORDER BY created_at DESC",
                    self.scope,
                ).fetchall()
            return {
                "items": [
                    {
                        "file_id": row["file_id"],
                        "undo_id": row["operation_id"],
                        "path": row["original_path"],
                        "name": Path(row["original_path"]).name,
                        "is_dir": bool(row["is_dir"]),
                        "removed_at": row["created_at"],
                    }
                    for row in rows
                ]
            }
