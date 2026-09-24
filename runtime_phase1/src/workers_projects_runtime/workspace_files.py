"""Bounded, owner-scoped streaming admission for ordinary workspace files.

This enforces API admission against logical retained bytes. Native hard quota
capability must be supplied by the deployment filesystem separately.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import AsyncIterable


DEFAULT_OWNER_STORAGE_BYTES = 5_000_000_000


class FileAdmissionError(ValueError):
    def __init__(self, message: str, status_code: int = 409, *, code: str = "file_operation_failed") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def ensure_workspace_file_schema(conn) -> None:
    """Called by the existing runtime Store migration transaction."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS workspace_file_uploads (
            upload_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            draft_id TEXT NOT NULL, request_key TEXT NOT NULL, name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL, sha256 TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            lease_id TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
            expires_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(tenant_id, owner_id, request_key))""",
        """CREATE TABLE IF NOT EXISTS workspace_file_pins (
            kind TEXT NOT NULL, reference_id TEXT NOT NULL, upload_id TEXT NOT NULL,
            PRIMARY KEY(kind, reference_id, upload_id),
            FOREIGN KEY(upload_id) REFERENCES workspace_file_uploads(upload_id))""",
        """CREATE TABLE IF NOT EXISTS workspace_file_bindings (
            binding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            worker_id TEXT NOT NULL, request_key TEXT NOT NULL, manifest_json TEXT NOT NULL,
            request_directory_json TEXT,
            UNIQUE(tenant_id, owner_id, worker_id, request_key))""",
        """CREATE TABLE IF NOT EXISTS workspace_file_entries (
            file_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL, path TEXT NOT NULL, version_id TEXT NOT NULL DEFAULT '',
            deleted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(tenant_id, owner_id, workspace_key, path))""",
        """CREATE TABLE IF NOT EXISTS workspace_file_policies (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, storage_limit_bytes INTEGER,
            max_file_bytes INTEGER, max_batch_files INTEGER, max_batch_bytes INTEGER,
            PRIMARY KEY(tenant_id, owner_id))""",
        """CREATE TABLE IF NOT EXISTS workspace_file_run_inputs (
            run_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)""",
    ):
        conn.execute(statement)
    upload_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(workspace_file_uploads)")
    }
    if "relative_path" not in upload_columns:
        conn.execute(
            "ALTER TABLE workspace_file_uploads ADD COLUMN relative_path TEXT NOT NULL DEFAULT ''"
        )
    if "metadata_revision" not in upload_columns:
        conn.execute(
            "ALTER TABLE workspace_file_uploads ADD COLUMN metadata_revision INTEGER NOT NULL DEFAULT 0"
        )
    binding_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(workspace_file_bindings)")
    }
    if "request_directory_json" not in binding_columns:
        conn.execute(
            "ALTER TABLE workspace_file_bindings ADD COLUMN request_directory_json TEXT"
        )
    for column, source in (("initial_name", "name"), ("initial_relative_path", "relative_path")):
        if column not in upload_columns:
            conn.execute(f"ALTER TABLE workspace_file_uploads ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            conn.execute(f"UPDATE workspace_file_uploads SET {column}=COALESCE(NULLIF({source},''),name)")
    from .workspace_file_mutations import ensure_file_mutation_schema

    ensure_file_mutation_schema(conn)
    from .workspace_file_storage import ensure_storage_adapter_schema

    ensure_storage_adapter_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_runs)")}
    if columns and "file_manifest_json" not in columns:
        conn.execute(
            "ALTER TABLE scheduled_runs ADD COLUMN file_manifest_json TEXT NOT NULL DEFAULT '[]'"
        )
    conn.execute("""CREATE TRIGGER IF NOT EXISTS workspace_file_schedule_cancel
        AFTER UPDATE OF state ON scheduled_runs WHEN NEW.state='cancelled'
        BEGIN DELETE FROM workspace_file_pins WHERE kind='schedule' AND reference_id=NEW.schedule_id; END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS workspace_file_schedule_delete
        AFTER DELETE ON scheduled_runs
        BEGIN DELETE FROM workspace_file_pins WHERE kind='schedule' AND reference_id=OLD.schedule_id; END""")


def pin_file_manifest(
    conn,
    *,
    kind: str,
    reference_id: str,
    tenant_id: str,
    owner_id: str,
    manifest: list[dict],
) -> None:
    """Validate and pin in the caller's run/schedule admission transaction."""
    for entry in manifest:
        upload_id = str(entry.get("upload_id") or "")
        row = conn.execute(
            "SELECT * FROM workspace_file_uploads WHERE upload_id=? AND tenant_id=? AND owner_id=?",
            (upload_id, tenant_id, owner_id),
        ).fetchone()
        if (
            row is None
            or row["state"] != "ready"
            or row["sha256"] != entry.get("sha256")
        ):
            raise FileAdmissionError(
                "An accepted file is no longer ready; retry the file transfer", 409
            )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_file_pins VALUES (?, ?, ?)",
            (kind, reference_id, upload_id),
        )


def register_input_file_versions(conn, worker: dict, manifest: list[dict]) -> None:
    """Bind stable file IDs to their accepted immutable input version.

    The existing version_id names an owner-scoped upload. File moves and undo
    retain the entry ID and this version; current bytes determine input/output.
    """
    scope = (worker["tenant_id"], worker["owner_id"], WorkspaceFiles._workspace_key(worker))
    for item in manifest:
        path = _relative(str(item["path"])).as_posix()
        upload_id = str(item["upload_id"])
        version = conn.execute(
            "SELECT sha256,size_bytes,state FROM workspace_file_uploads WHERE upload_id=? AND tenant_id=? AND owner_id=?",
            (upload_id, scope[0], scope[1]),
        ).fetchone()
        if version is None or version["state"] != "ready" or version["sha256"] != item["sha256"]:
            raise FileAdmissionError("Accepted input version is unavailable", 409)
        existing = conn.execute(
            "SELECT file_id,version_id FROM workspace_file_entries WHERE tenant_id=? AND owner_id=? AND workspace_key=? AND path=?",
            (*scope, path),
        ).fetchone()
        if existing is not None and existing["version_id"] != upload_id:
            # Binding admitted an unused physical path. A native deletion may
            # have left an older indexed identity here; do not reuse its ID.
            from .workspace_file_mutations import TRASH

            conn.execute(
                "UPDATE workspace_file_entries SET path=?,deleted=1 WHERE file_id=?",
                (f"{TRASH}/stale-{existing['file_id']}", existing["file_id"]),
            )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_file_entries (file_id,tenant_id,owner_id,workspace_key,path,version_id) VALUES (?,?,?,?,?,?)",
            ("fen_" + uuid.uuid4().hex, *scope, path, upload_id),
        )


def artifact_worker_context(conn, worker: dict | None, *, files=None) -> dict | None:
    """Attach private typed input versions for output projection only.

    Fallback manifests cover accepted files from before entry-version tracking;
    typed mutation history resolves their current location. New moves and undo
    retain their stable entry/version identity directly.
    """
    if not worker or not worker.get("worker_id"):
        return worker
    if files is not None:
        worker = files._scope_worker(worker)
    tenant = str(worker.get("tenant_id") or "local")
    owner = str(worker.get("owner_id") or "")
    key = WorkspaceFiles._workspace_key(worker)
    entries = conn.execute(
        "SELECT e.path,e.version_id,e.deleted,u.sha256,u.size_bytes FROM workspace_file_entries e JOIN workspace_file_uploads u ON u.upload_id=e.version_id AND u.tenant_id=e.tenant_id AND u.owner_id=e.owner_id WHERE e.tenant_id=? AND e.owner_id=? AND e.workspace_key=?",
        (tenant, owner, key),
    ).fetchall()
    versions = {row["path"]: {"sha256": row["sha256"], "size_bytes": row["size_bytes"], "upload_id": row["version_id"]}
                for row in entries if not row["deleted"]}
    # Workspace member identity is structured runtime data, never a path/name test.
    manifests = conn.execute(
        "SELECT b.worker_id,b.manifest_json FROM workspace_file_bindings b WHERE b.tenant_id=? AND b.owner_id=? AND (b.worker_id=? OR EXISTS (SELECT 1 FROM json_each(b.manifest_json) m WHERE json_extract(m.value,'$.file_scope_id')=?))",
        (tenant, owner, worker["worker_id"], key),
    ).fetchall()
    manifests += conn.execute(
        "SELECT r.worker_id,f.manifest_json FROM workspace_file_run_inputs f JOIN runs r ON r.run_id=f.run_id JOIN workers w ON w.worker_id=r.worker_id WHERE w.tenant_id=? AND w.owner_id=? AND (r.worker_id=? OR EXISTS (SELECT 1 FROM json_each(f.manifest_json) m WHERE json_extract(m.value,'$.file_scope_id')=?))",
        (tenant, owner, worker["worker_id"], key),
    ).fetchall()
    tracked = {str(row["version_id"]) for row in entries}
    fallback = [item for row in manifests for item in json.loads(row["manifest_json"])
                if item["upload_id"] not in tracked and
                (item.get("file_scope_id") == key if item.get("file_scope_id") else row["worker_id"] == worker["worker_id"])]
    if not fallback:
        return {**worker, "_workspace_input_versions": versions}
    uploads = {row["upload_id"]: row for row in conn.execute(
        "SELECT upload_id,created_at FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=?", (tenant, owner)
    )}
    operations = list(conn.execute(
        "SELECT source_path,target_path,created_at FROM workspace_file_operations WHERE tenant_id=? AND owner_id=? AND workspace_key=? AND state IN ('applied','undone') ORDER BY created_at,operation_id",
        (tenant, owner, key),
    ))
    for item in fallback:
        path = item["path"]
        accepted = uploads.get(item["upload_id"])
        for operation in operations if accepted is not None else []:
            source = operation["source_path"]
            if operation["created_at"] >= accepted["created_at"] and (path == source or path.startswith(source + "/")):
                path = operation["target_path"] + path[len(source):]
        versions.setdefault(path, {"sha256": item["sha256"], "size_bytes": item["size_bytes"], "upload_id": item["upload_id"]})
    return {**worker, "_workspace_input_versions": versions}


def _relative(value: str, *, directory: bool = False) -> Path:
    from .deliverables import NON_DELIVERABLE_DIR_NAMES, NON_DELIVERABLE_FILE_NAMES

    raw = str(value or "")
    if directory and not raw:
        return Path(".")
    if not raw or raw.startswith(("/", "\\")) or "\\" in raw or "\x00" in raw:
        raise FileAdmissionError("Choose a relative file path", 422)
    parts = raw.split("/")
    if any(
        not part or part in {".", ".."} or len(part.encode("utf-8")) > 255
        for part in parts
    ):
        raise FileAdmissionError("File path is invalid", 422)
    if any(
        part.startswith(".") or part.lower() in NON_DELIVERABLE_DIR_NAMES
        for part in parts
    ):
        raise FileAdmissionError(
            "This path is reserved for private workspace state", 403
        )
    if len(parts) == 1 and parts[0].lower() in NON_DELIVERABLE_FILE_NAMES:
        raise FileAdmissionError(
            "This path is reserved for workspace configuration", 403
        )
    return Path(*parts)


def _historical_binding_directory(manifest: list[dict]) -> dict | None:
    """Recover an old row's destination only when its accepted paths prove it."""
    if not manifest:
        return None
    if all(
        item.get("path")
        == f"inputs/{item['upload_id']}/{item.get('relative_path') or item['name']}"
        for item in manifest
    ):
        return {"mode": "default"}
    common = None
    for item in manifest:
        try:
            accepted = _relative(item["path"])
            relative = _relative(item.get("relative_path") or item["name"])
        except (KeyError, FileAdmissionError):
            return None
        parent = accepted.parent.parts
        suffix = relative.parent.parts
        if len(parent) < len(suffix) or (suffix and parent[-len(suffix):] != suffix):
            return None
        prefix = parent[:len(parent) - len(suffix)] if suffix else parent
        destination = Path(*prefix).as_posix() if prefix else "."
        if common is not None and destination != common:
            return None
        common = destination
    return {"mode": "directory", "path": common}


@contextmanager
def _directory(root: Path, relative: Path = Path("."), *, create: bool = False, access=None):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        descriptor = os.open(root, flags)
        descriptors.append(descriptor)
        if access is not None:
            access(descriptor)
        for part in relative.parts:
            if part == ".":
                continue
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
            if access is not None:
                access(descriptor)
        yield descriptor
    except (NotADirectoryError, FileNotFoundError) as exc:
        raise FileAdmissionError(
            "The file location changed or is unavailable", 409
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _snapshot_fd(parent: int, name: str) -> tuple[int, str, int]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
    except OSError as exc:
        raise FileAdmissionError("File is unavailable or has changed", 409) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise FileAdmissionError(
                "Only ordinary workspace files can be accessed", 403
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = lambda row: (
            row.st_dev,
            row.st_ino,
            row.st_size,
            row.st_mtime_ns,
            row.st_ctime_ns,
        )
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise FileAdmissionError("File changed while it was read; retry", 409)
        revision = hashlib.sha256(
            f"{digest.hexdigest()}:{identity(after)}".encode()
        ).hexdigest()
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, revision, after.st_size
    except BaseException:
        os.close(descriptor)
        raise


def _keep_both_name(name: str, number: int) -> str:
    """Add a collision suffix without exceeding the filesystem byte limit."""
    stem, extension = os.path.splitext(name)
    suffix = f"-{number}"
    budget = 255 - len(suffix.encode()) - len(extension.encode())
    if budget < 1:
        stem, extension = name, ""
        budget = 255 - len(suffix.encode())
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return f"{stem}{suffix}{extension}"


class WorkspaceFiles:
    """One SQLite owner for drafts, immutable uploads, references and policy."""

    def __init__(
        self, store, *, root_resolver=None, scope_resolver=None, scope_access=None, owner_roots=None, quota_backend=None,
        managed_root_resolver=None, native_coverage=None, policy_updater=None, control_root=None,
    ):
        self.store = store
        from .execution_profile import packaged_linux
        if packaged_linux():
            configured_root = os.environ.get("XPERFECT_SHARED_VOLUME_ROOT", "")
            if not configured_root or not Path(configured_root).is_absolute():
                raise FileAdmissionError("Packaged file storage is not configured", 503)
            self.private_root = Path(configured_root) / "managed-files"
        else:
            self.private_root = Path(store.db_path).resolve().parent / "managed-files"
        self.root_resolver = root_resolver
        self.scope_resolver = scope_resolver
        self.scope_access = scope_access
        self.owner_roots = owner_roots
        from .workspace_file_storage import FileStorageAdapter
        self.storage_adapter = FileStorageAdapter(
            self, backend=quota_backend, managed_root_resolver=managed_root_resolver,
            native_coverage=native_coverage, policy_updater=policy_updater, control_root=control_root,
        )

    def _owner_root(self, tenant_id: str, owner_id: str) -> Path:
        return self.storage_adapter.owner_root(tenant_id, owner_id)

    def _local_owner_root(self, tenant_id: str, owner_id: str) -> Path:
        key = hashlib.sha256(f"{tenant_id}\0{owner_id}".encode()).hexdigest()
        root = self.private_root / key
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise FileAdmissionError("File storage is unavailable", 503)
        root.chmod(0o700)
        return root

    def _roots(self, tenant_id: str, owner_id: str) -> list[Path]:
        if self.owner_roots is not None:
            roots = self.owner_roots(tenant_id, owner_id)
        else:
            roots = [
                Path(str(row[key]))
                for row in self.store.list_all_workers()
                if str(row.get("tenant_id") or "local") == tenant_id
                and row.get("owner_id") == owner_id
                for key in ("state_dir", "workspace_dir")
                if row.get(key)
            ]
            if getattr(self, "scope_resolver", None) is not None:
                roots.extend(Path(self._scope_worker(row)["workspace_dir"])
                             for row in self.store.list_all_workers()
                             if str(row.get("tenant_id") or "local") == tenant_id and row.get("owner_id") == owner_id)
        return [*roots, self._owner_root(tenant_id, owner_id)]

    def _policy(self, conn, tenant_id: str, owner_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM workspace_file_policies WHERE tenant_id=? AND owner_id=?",
            (tenant_id, owner_id),
        ).fetchone()
        if row:
            return dict(row)
        try:
            from .auth import multi_user_security_enabled

            configured = os.environ.get("GLASSHIVE_OWNER_STORAGE_BYTES")
            policy = {
                "storage_limit_bytes": int(configured)
                if configured is not None
                else (
                    DEFAULT_OWNER_STORAGE_BYTES
                    if multi_user_security_enabled()
                    else None
                )
            }
            for field, variable in (
                ("max_file_bytes", "GLASSHIVE_FILE_MAX_BYTES"),
                ("max_batch_files", "GLASSHIVE_UI_UPLOAD_MAX_FILES"),
                ("max_batch_bytes", "GLASSHIVE_UI_UPLOAD_MAX_BYTES"),
            ):
                policy[field] = (
                    int(os.environ[variable]) if variable in os.environ else None
                )
            if any(value is not None and value < 0 for value in policy.values()):
                raise ValueError()
            return policy
        except ValueError as exc:
            raise FileAdmissionError("File storage policy is invalid", 503) from exc

    def _logical_usage(self, conn, tenant_id: str, owner_id: str) -> tuple[int, int]:
        used = logical_bytes(self._roots(tenant_id, owner_id))
        reserved = conn.execute(
            "SELECT COALESCE(SUM(MAX(0,size_bytes-received_bytes)),0) FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND state IN ('pending','receiving')",
            (tenant_id, owner_id),
        ).fetchone()[0]
        manifests = conn.execute(
            "SELECT manifest_json FROM workspace_file_bindings WHERE tenant_id=? AND owner_id=?",
            (tenant_id, owner_id),
        ).fetchall()
        manifests += conn.execute(
            "SELECT file_manifest_json FROM scheduled_runs WHERE tenant_id=? AND owner_id=? AND state!='cancelled'",
            (tenant_id, owner_id),
        ).fetchall()
        manifests += conn.execute(
            "SELECT f.manifest_json FROM workspace_file_run_inputs f JOIN runs r ON r.run_id=f.run_id JOIN workers w ON w.worker_id=r.worker_id WHERE w.tenant_id=? AND w.owner_id=?",
            (tenant_id, owner_id),
        ).fetchall()
        owner_root = self._owner_root(tenant_id, owner_id)
        seen = set()
        for row in manifests:
            for item in json.loads(row[0]):
                projection_id = item.get("projection_id")
                if projection_id and projection_id not in seen:
                    seen.add(projection_id)
                    if not self.storage_adapter.completed(tenant_id, owner_id, item, owner_root=owner_root):
                        reserved += item["size_bytes"]
        return used, int(reserved)

    def _capacity(self, conn, tenant_id: str, owner_id: str):
        snapshot = self.storage_adapter.snapshot(conn, tenant_id, owner_id)
        if snapshot is None:
            used, reserved = self._logical_usage(conn, tenant_id, owner_id)
            return used, reserved, self._policy(conn, tenant_id, owner_id)["storage_limit_bytes"], 1, None
        reserved = self.storage_adapter.reservation(conn, tenant_id, owner_id, snapshot)
        return snapshot.used_allocated_bytes, reserved, snapshot.kernel_hard_limit_bytes, snapshot.filesystem_block_bytes, snapshot

    def _usage(self, conn, tenant_id: str, owner_id: str) -> tuple[int, int]:
        used, reserved, *_ = self._capacity(conn, tenant_id, owner_id)
        return used, reserved

    def reserve_projection_manifest(
        self, conn, tenant_id: str, owner_id: str, manifest: list[dict]
    ) -> None:
        """Caller must persist the manifest in this same IMMEDIATE transaction."""
        from .workspace_file_storage import round_allocation
        used, reserved, limit, block, _ = self._capacity(conn, tenant_id, owner_id)
        if (
            limit is not None
            and used + reserved + sum(round_allocation(item["size_bytes"], block) for item in manifest) > limit
        ):
            raise FileAdmissionError(
                "Not enough storage for the workspace copy", 413,
                code="owner_storage_insufficient",
            )
        for item in manifest:
            item.setdefault("projection_id", "prj_" + uuid.uuid4().hex)

    def storage(self, tenant_id: str, owner_id: str) -> dict:
        self.expire(tenant_id, owner_id)
        with self.store._connect() as conn:
            policy = self._policy(conn, tenant_id, owner_id)
            used, reserved, limit, block, snapshot = self._capacity(conn, tenant_id, owner_id)
        coverage = False
        if snapshot is not None and self.storage_adapter.native_coverage is not None:
            try:
                coverage = self.storage_adapter.native_coverage(tenant_id, owner_id, snapshot) is True
            except Exception as exc:
                raise FileAdmissionError("Native storage coverage verification is unavailable", 503) from exc
        return {
            **policy, "limit_bytes": policy["storage_limit_bytes"],
            "kernel_hard_limit_bytes": limit if snapshot is not None else None,
            "used_logical_bytes": logical_bytes([Path(snapshot.root)]) if snapshot is not None else used,
            "used_allocated_bytes": used if snapshot is not None else None,
            "reserved_bytes": reserved,
            "reserved_allocated_bytes": reserved if snapshot is not None else None,
            "filesystem_block_bytes": block if snapshot is not None else None,
            "available_bytes": max(0, limit-used-reserved) if limit is not None else None,
            "accounting_basis": snapshot.accounting_basis if snapshot is not None else "logical_retained_bytes",
            "quota_hard_enforced": snapshot.hard_enforced if snapshot is not None else False,
            "native_hard_enforcement": bool(snapshot is not None and snapshot.hard_enforced and coverage),
        }

    def set_policy(
        self,
        tenant_id: str,
        owner_id: str,
        values: dict,
        *,
        administrator: bool = False,
        inherit: bool = False,
    ) -> dict:
        fields = (
            "storage_limit_bytes",
            "max_file_bytes",
            "max_batch_files",
            "max_batch_bytes",
        )
        if set(values) - set(fields) or any(
            value is not None
            and (not isinstance(value, int) or isinstance(value, bool) or value < 0)
            for value in values.values()
        ):
            raise FileAdmissionError(
                "Storage limits must be non-negative byte or file counts", 422
            )
        if self.storage_adapter.backend is not None:
            updater = self.storage_adapter.policy_updater
            if updater is None:
                raise FileAdmissionError("Storage policy changes require the trusted runtime policy owner", 503)
            with self.store._connect() as conn:
                current = self._policy(conn, tenant_id, owner_id)
            if inherit and not administrator:
                raise FileAdmissionError("An administrator must restore deployment limits", 403)
            if not administrator:
                for field, value in values.items():
                    ceiling = current[field]
                    if ceiling is not None and (value is None or value > ceiling):
                        raise FileAdmissionError("An administrator must increase this limit", 403)
            try:
                updater(tenant_id, owner_id, dict(values), administrator=administrator, inherit=inherit)
            except FileAdmissionError:
                raise
            except Exception as exc:
                raise FileAdmissionError("The trusted storage policy transition did not complete", 503) from exc
            return self.storage(tenant_id, owner_id)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._policy(conn, tenant_id, owner_id)
            if inherit:
                if not administrator:
                    raise FileAdmissionError(
                        "An administrator must restore deployment limits", 403
                    )
                conn.execute(
                    "DELETE FROM workspace_file_policies WHERE tenant_id=? AND owner_id=?",
                    (tenant_id, owner_id),
                )
            else:
                proposed = {
                    field: values.get(field, current[field]) for field in fields
                }
                if not administrator:
                    for field, value in proposed.items():
                        ceiling = current[field]
                        if ceiling is not None and (value is None or value > ceiling):
                            raise FileAdmissionError(
                                "An administrator must increase this limit", 403
                            )
                conn.execute(
                    "INSERT INTO workspace_file_policies VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,owner_id) DO UPDATE SET storage_limit_bytes=excluded.storage_limit_bytes,max_file_bytes=excluded.max_file_bytes,max_batch_files=excluded.max_batch_files,max_batch_bytes=excluded.max_batch_bytes",
                    (tenant_id, owner_id, *(proposed[field] for field in fields)),
                )
        return self.storage(tenant_id, owner_id)

    def create_upload(
        self,
        tenant_id: str,
        owner_id: str,
        *,
        draft_id: str,
        name: str,
        size_bytes: int,
        idempotency_key: str,
        relative_path: str = "",
    ) -> dict:
        _safe_name(name)
        relative_path = str(relative_path or name)
        relative = _relative(relative_path)
        if relative.name != name:
            raise FileAdmissionError("File name does not match its selected path", 422)
        if (
            not draft_id
            or not idempotency_key
            or len(draft_id) > 128
            or len(idempotency_key) > 128
            or size_bytes < 0
        ):
            raise FileAdmissionError("Upload metadata is invalid", 422)
        self.expire(tenant_id, owner_id)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT * FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND request_key=?",
                (tenant_id, owner_id, idempotency_key),
            ).fetchone()
            if old:
                if (
                    old["draft_id"],
                    old["initial_name"] or old["name"],
                    old["size_bytes"],
                    old["initial_relative_path"] or old["relative_path"] or old["name"],
                ) != (draft_id, name, size_bytes, relative_path):
                    raise FileAdmissionError(
                        "Upload retry does not match the original file", 409
                    )
                return self._public(dict(old))
            policy = self._policy(conn, tenant_id, owner_id)
            count, size = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND draft_id=? AND state NOT IN ('cancelled','expired')",
                (tenant_id, owner_id, draft_id),
            ).fetchone()
            if (
                policy["max_file_bytes"] is not None
                and size_bytes > policy["max_file_bytes"]
            ):
                raise FileAdmissionError("File exceeds the configured size limit", 413)
            if (
                policy["max_batch_files"] is not None
                and count + 1 > policy["max_batch_files"]
            ):
                raise FileAdmissionError(
                    "Selection exceeds the configured file count", 413
                )
            if (
                policy["max_batch_bytes"] is not None
                and size + size_bytes > policy["max_batch_bytes"]
            ):
                raise FileAdmissionError(
                    "Selection exceeds the configured byte limit", 413
                )
            from .workspace_file_storage import round_allocation
            used, reserved, limit, block, _ = self._capacity(conn, tenant_id, owner_id)
            if (
                limit is not None
                and used + reserved + round_allocation(size_bytes, block) > limit
            ):
                raise FileAdmissionError("Not enough storage for this file", 413)
            now = time.time()
            upload_id = "fil_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO workspace_file_uploads (upload_id,tenant_id,owner_id,draft_id,request_key,name,size_bytes,state,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    upload_id,
                    tenant_id,
                    owner_id,
                    draft_id,
                    idempotency_key,
                    name,
                    size_bytes,
                    now + 86400,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE workspace_file_uploads SET relative_path=?,initial_relative_path=?,initial_name=? WHERE upload_id=?",
                (relative_path, relative_path, name, upload_id),
            )
            return self._public(
                dict(
                    conn.execute(
                        "SELECT * FROM workspace_file_uploads WHERE upload_id=?",
                        (upload_id,),
                    ).fetchone()
                )
            )

    @staticmethod
    def _public(row: dict) -> dict:
        from .workspace_file_drafts import draft_revision

        result = {
            key: row[key]
            for key in (
                "upload_id",
                "draft_id",
                "name",
                "size_bytes",
                "received_bytes",
                "state",
                "sha256",
                "error",
                "expires_at",
                "relative_path",
            )
        }
        result["revision"] = draft_revision(row)
        return result

    def get_upload(self, tenant_id, owner_id, upload_id, *, revision=None):
        from .workspace_file_drafts import get_draft
        return get_draft(self, tenant_id, owner_id, upload_id, revision=revision)

    def move_upload(self, tenant_id, owner_id, upload_id, *, relative_path, revision):
        from .workspace_file_drafts import move_draft
        return move_draft(self, tenant_id, owner_id, upload_id, relative_path=relative_path, revision=revision)

    def _upload(self, conn, tenant_id, owner_id, upload_id):
        row = conn.execute(
            "SELECT * FROM workspace_file_uploads WHERE upload_id=? AND tenant_id=? AND owner_id=?",
            (upload_id, tenant_id, owner_id),
        ).fetchone()
        if row is None:
            raise FileAdmissionError("File upload not found", 404)
        return dict(row)

    def list_uploads(self, tenant_id: str, owner_id: str, draft_id: str) -> dict:
        self.expire(tenant_id, owner_id)
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND draft_id=? ORDER BY created_at,upload_id",
                (tenant_id, owner_id, draft_id),
            ).fetchall()
        return {"items": [self._public(dict(row)) for row in rows]}

    async def receive(
        self,
        tenant_id: str,
        owner_id: str,
        upload_id: str,
        chunks: AsyncIterable[bytes],
    ) -> dict:
        root = self._owner_root(tenant_id, owner_id)
        part, blob = root / f"{upload_id}.part", root / f"{upload_id}.blob"
        lease = uuid.uuid4().hex
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._upload(conn, tenant_id, owner_id, upload_id)
            if row["state"] == "ready":
                return self._public(row)
            if row["state"] in {"cancelled", "expired"}:
                raise FileAdmissionError(
                    "Upload was cancelled or expired; select it again", 409
                )
            if (
                row["state"] in {"receiving", "cancelling"}
                and row["lease_until"] > time.time()
            ):
                raise FileAdmissionError("This file is already uploading", 409)
            try:
                parallel = int(os.environ.get("GLASSHIVE_MAX_PARALLEL_UPLOADS", "4"))
            except ValueError as exc:
                raise FileAdmissionError(
                    "Upload concurrency configuration is invalid", 503
                ) from exc
            if parallel < 1:
                raise FileAdmissionError("File transfers are disabled", 503)
            active = conn.execute(
                "SELECT COUNT(*) FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND state IN ('receiving','cancelling') AND lease_until>? AND upload_id!=?",
                (tenant_id, owner_id, time.time(), upload_id),
            ).fetchone()[0]
            if active >= parallel:
                raise FileAdmissionError(
                    "Other files are transferring; retry when one finishes", 429
                )
            if blob.exists():
                with _directory(root) as parent:
                    descriptor, _, size = _snapshot_fd(parent, blob.name)
                    recovered = hashlib.sha256()
                    try:
                        while chunk := os.read(descriptor, 1024 * 1024):
                            recovered.update(chunk)
                    finally:
                        os.close(descriptor)
                if size != row["size_bytes"]:
                    raise FileAdmissionError("Stored upload needs recovery", 409)
                conn.execute(
                    "UPDATE workspace_file_uploads SET state='ready',received_bytes=?,sha256=?,lease_id='',lease_until=0 WHERE upload_id=?",
                    (size, recovered.hexdigest(), upload_id),
                )
                return self._public(self._upload(conn, tenant_id, owner_id, upload_id))
            part.unlink(missing_ok=True)
            conn.execute(
                "UPDATE workspace_file_uploads SET state='receiving',received_bytes=0,error='',lease_id=?,lease_until=?,updated_at=? WHERE upload_id=?",
                (lease, time.time() + 120, time.time(), upload_id),
            )
            used, reserved, limit, _, _ = self._capacity(conn, tenant_id, owner_id)
            if limit is not None and used + reserved > limit:
                raise FileAdmissionError("Not enough storage to retry this file", 413)
        received, digest = 0, hashlib.sha256()
        try:
            fd = os.open(
                part,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as output:
                async for chunk in chunks:
                    if received + len(chunk) > row["size_bytes"]:
                        raise FileAdmissionError(
                            "Received bytes exceed the reserved file size", 413
                        )
                    with self.store._connect() as conn:
                        current = self._upload(conn, tenant_id, owner_id, upload_id)
                        if (
                            current["state"] != "receiving"
                            or current["lease_id"] != lease
                        ):
                            raise FileAdmissionError(
                                "Upload was cancelled or replaced", 409
                            )
                    output.write(chunk)
                    output.flush()
                    received += len(chunk)
                    digest.update(chunk)
                    with self.store._connect() as conn:
                        conn.execute(
                            "UPDATE workspace_file_uploads SET received_bytes=?,lease_until=?,updated_at=? WHERE upload_id=? AND lease_id=? AND state='receiving'",
                            (
                                received,
                                time.time() + 120,
                                time.time(),
                                upload_id,
                                lease,
                            ),
                        )
                output.flush()
                os.fsync(output.fileno())
            if received != row["size_bytes"]:
                raise FileAdmissionError(
                    "Upload was interrupted; retry the same file", 422
                )
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._upload(conn, tenant_id, owner_id, upload_id)
                if current["state"] != "receiving" or current["lease_id"] != lease:
                    raise FileAdmissionError("Upload was cancelled or replaced", 409)
                used, reserved, limit, _, _ = self._capacity(conn, tenant_id, owner_id)
                if limit is not None and used + reserved > limit:
                    raise FileAdmissionError(
                        "Storage filled during upload; retry after freeing space", 413
                    )
                os.replace(part, blob)
                conn.execute(
                    "UPDATE workspace_file_uploads SET state='ready',sha256=?,lease_id='',lease_until=0,expires_at=?,updated_at=? WHERE upload_id=?",
                    (digest.hexdigest(), time.time() + 86400, time.time(), upload_id),
                )
                return self._public(self._upload(conn, tenant_id, owner_id, upload_id))
        except BaseException as exc:
            original = exc
            if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                try:
                    exc = self.storage_adapter.failure(tenant_id, owner_id, exc)
                except FileAdmissionError as unavailable:
                    exc = unavailable
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._upload(conn, tenant_id, owner_id, upload_id)
                if current["lease_id"] == lease:
                    part.unlink(missing_ok=True)
                    # A completed rename with a lost DB commit is recoverable on retry.
                    if blob.exists() and blob.stat().st_size == row["size_bytes"]:
                        conn.execute(
                            "UPDATE workspace_file_uploads SET state='ready',received_bytes=?,sha256=?,lease_id='',lease_until=0,updated_at=? WHERE upload_id=?",
                            (received, digest.hexdigest(), time.time(), upload_id),
                        )
                    else:
                        state = (
                            "cancelled"
                            if current["state"] == "cancelling"
                            else "failed"
                        )
                        conn.execute(
                            "UPDATE workspace_file_uploads SET state=?,received_bytes=0,error=?,lease_id='',lease_until=0,updated_at=? WHERE upload_id=?",
                            (
                                state,
                                str(exc)
                                if isinstance(exc, FileAdmissionError)
                                else "Transfer interrupted; retry the file",
                                time.time(),
                                upload_id,
                            ),
                        )
            if exc is not original:
                raise exc from original
            raise

    def cancel(self, tenant_id: str, owner_id: str, upload_id: str) -> dict:
        root = self._owner_root(tenant_id, owner_id)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._upload(conn, tenant_id, owner_id, upload_id)
            pinned = conn.execute(
                "SELECT 1 FROM workspace_file_pins WHERE upload_id=? LIMIT 1",
                (upload_id,),
            ).fetchone()
            if pinned:
                raise FileAdmissionError(
                    "This file is attached; remove its workspace copy separately", 409
                )
            if row["state"] == "receiving" and row["lease_until"] > time.time():
                conn.execute(
                    "UPDATE workspace_file_uploads SET state='cancelling',updated_at=? WHERE upload_id=?",
                    (time.time(), upload_id),
                )
                return {"upload_id": upload_id, "state": "cancelling"}
            # Fence writers first. Cleanup releases capacity only when bytes are gone.
            conn.execute(
                "UPDATE workspace_file_uploads SET state='cancelled',lease_id='',lease_until=0,updated_at=? WHERE upload_id=?",
                (time.time(), upload_id),
            )
            for suffix in ("part", "blob"):
                (root / f"{upload_id}.{suffix}").unlink(missing_ok=True)
            return {"upload_id": upload_id, "state": "cancelled"}

    def expire(self, tenant_id: str, owner_id: str) -> None:
        root = self._owner_root(tenant_id, owner_id)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT upload_id FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND expires_at<? AND lease_until<? AND state NOT IN ('expired','cancelled') AND NOT EXISTS (SELECT 1 FROM workspace_file_pins p WHERE p.upload_id=workspace_file_uploads.upload_id)",
                (tenant_id, owner_id, time.time(), time.time()),
            ).fetchall()
            for row in rows:
                for suffix in ("part", "blob"):
                    (root / f"{row['upload_id']}.{suffix}").unlink(missing_ok=True)
                conn.execute(
                    "UPDATE workspace_file_uploads SET state='expired',received_bytes=0,lease_id='',lease_until=0 WHERE upload_id=?",
                    (row["upload_id"],),
                )

    def manifest(
        self,
        tenant_id: str,
        owner_id: str,
        upload_ids: list[str],
        revisions: list[str] | None = None,
    ) -> list[dict]:
        if len(set(upload_ids)) != len(upload_ids):
            raise FileAdmissionError(
                "Each selected upload must have its own identity", 422
            )
        if revisions is not None and len(revisions) != len(upload_ids):
            raise FileAdmissionError(
                "Each selected file needs its matching revision", 422
            )
        from .workspace_file_drafts import draft_revision

        with self.store._connect() as conn:
            result = []
            for index, upload_id in enumerate(upload_ids):
                row = self._upload(conn, tenant_id, owner_id, upload_id)
                if row["state"] != "ready":
                    raise FileAdmissionError(
                        "Finish or remove every selected file before starting", 409
                    )
                actual_revision = draft_revision(row)
                if revisions is not None and actual_revision != revisions[index]:
                    raise FileAdmissionError(
                        "The selected file version changed; refresh Files and retry",
                        409,
                    )
                result.append(
                    {
                        "upload_id": upload_id,
                        "revision": actual_revision,
                        "name": row["name"],
                        "size_bytes": row["size_bytes"],
                        "sha256": row["sha256"],
                        "relative_path": row["relative_path"] or row["name"],
                        "path": f"inputs/{upload_id}/{row['relative_path'] or row['name']}",
                    }
                )
            return result

    def bootstrap_entries(
        self, tenant_id: str, owner_id: str, manifest: list[dict]
    ) -> list[dict]:
        from .bootstrap import sign_bootstrap_source_path

        root = self._owner_root(tenant_id, owner_id)
        entries = []
        for item in manifest:
            source = root / f"{item['upload_id']}.blob"
            entries.append(
                {
                    "scope": "workspace",
                    "path": item["path"],
                    "filename": item["name"],
                    "source_path": str(source),
                    "source_path_token": sign_bootstrap_source_path(
                        source, tenant_id=tenant_id, owner_id=owner_id
                    ),
                    "managed_upload_id": item["upload_id"],
                    "managed_projection_id": item.get("projection_id"),
                    "sha256": item["sha256"],
                    "bytes": item["size_bytes"],
                    "allow_empty": True,
                }
            )
        return entries

    def _worker(self, worker_id: str, tenant_id: str, owner_id: str, *, read_only: bool = False) -> dict:
        worker = self.store.get_worker(worker_id, tenant_id, owner_id)
        if worker is None:
            raise FileAdmissionError("Workspace not found", 404)
        if str(worker.get("state")) in {
            "terminating",
            "termination_failed",
            "terminated",
        } and not (read_only and worker.get("state") == "terminated"):
            raise FileAdmissionError("Workspace is closed", 409)
        return self._scope_worker(worker)

    def _scope_worker(self, worker: dict, *, connection=None) -> dict:
        """Resolve physical identity without requiring the directory to exist."""
        resolver = getattr(self, "scope_resolver", None)
        if resolver is not None:
            scope = resolver(worker["worker_id"], tenant_id=worker["tenant_id"], owner_id=worker["owner_id"],
                **({"connection": connection} if connection is not None else {}))
            if not isinstance(scope, dict) or not isinstance(scope.get("scope_id"), str) or not scope["scope_id"] or scope.get("kind") not in {"member", "workspace"}:
                raise FileAdmissionError("Runtime file scope identity is unavailable", 409)
            raw_root = scope.get("root")
            if not isinstance(raw_root, (str, Path)) or not str(raw_root) or not Path(raw_root).is_absolute():
                raise FileAdmissionError("Runtime file scope root is unavailable", 409)
            scope = {key: scope.get(key) for key in ("scope_id", "kind", "workspace_id", "member_id", "root")}
            scope["root"] = str(raw_root)
        else:
            # Legacy runtime roots carry no authority to share IDs across bees.
            raw_root = str(worker.get("workspace_dir") or "")
            scope = {"scope_id": worker["worker_id"], "kind": "member",
                     "workspace_id": worker.get("workspace_id"), "member_id": worker["worker_id"], "root": raw_root}
        return {**worker, "_file_scope": scope, "workspace_dir": scope["root"]}

    def _root(self, worker: dict) -> Path:
        worker = self._scope_worker(worker)
        if self.root_resolver and getattr(self, "scope_resolver", None) is None:
            return self.root_resolver(
                worker["worker_id"],
                tenant_id=worker["tenant_id"],
                owner_id=worker["owner_id"],
            )
        raw = str(worker.get("workspace_dir") or "")
        if not raw or not Path(raw).is_absolute() or not Path(raw).is_dir():
            raise FileAdmissionError("Workspace files are not ready yet", 409)
        if Path(raw).is_symlink():
            raise FileAdmissionError("Workspace file root is unavailable", 409)
        return Path(raw)

    def apply_scope_access(self, worker: dict, descriptor: int, *, is_directory: bool) -> None:
        """Apply runtime-owned ACLs to the already opened inode, never a path."""
        callback = getattr(self, "scope_access", None)
        if callback is None:
            return
        worker = self._scope_worker(worker)
        metadata = os.fstat(descriptor)
        if not (stat.S_ISDIR(metadata.st_mode) if is_directory else stat.S_ISREG(metadata.st_mode)):
            raise FileAdmissionError("File access target is unavailable", 409)
        try:
            callback(worker["worker_id"], tenant_id=worker["tenant_id"], owner_id=worker["owner_id"],
                     scope_id=self._workspace_key(worker), descriptor=descriptor, is_directory=is_directory)
        except FileAdmissionError:
            raise
        except Exception as exc:
            raise FileAdmissionError("File access setup failed; retry the transfer", 503) from exc

    def scope_has_active_run(self, worker: dict) -> bool:
        worker = self._scope_worker(worker)
        key = self._workspace_key(worker)
        for member in self.store.list_all_workers():
            if member.get("tenant_id") != worker["tenant_id"] or member.get("owner_id") != worker["owner_id"]:
                continue
            if self.store.get_active_run(member["worker_id"]) and self._workspace_key(self._scope_worker(member)) == key:
                return True
        return False

    @staticmethod
    def _workspace_key(worker: dict) -> str:
        # Logical workspace membership never implies a common physical root.
        return str((worker.get("_file_scope") or {}).get("scope_id") or worker["worker_id"])

    def copy_exact_input_versions(self, source_worker: dict, target_worker: dict,
                                  copied_paths: set[str]) -> None:
        """Keep accepted-input names on a copy only when its bytes still match."""
        if ((source_worker["tenant_id"], source_worker["owner_id"])
                != (target_worker["tenant_id"], target_worker["owner_id"])):
            raise FileAdmissionError("A workspace copy cannot cross owners", 403)
        source = self._scope_worker(source_worker)
        target = self._scope_worker(target_worker)
        root = Path(target["workspace_dir"])
        if (Path(source["workspace_dir"]) != Path(source_worker["workspace_dir"])
                or root != Path(target_worker["workspace_dir"])):
            return
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT e.path,e.version_id,u.sha256,u.size_bytes "
                "FROM workspace_file_entries e JOIN workspace_file_uploads u "
                "ON u.upload_id=e.version_id AND u.tenant_id=e.tenant_id AND u.owner_id=e.owner_id "
                "WHERE e.tenant_id=? AND e.owner_id=? AND e.workspace_key=? "
                "AND e.deleted=0 AND u.state='ready'",
                (source["tenant_id"], source["owner_id"], self._workspace_key(source)),
            ).fetchall()
        verified = []
        for row in rows:
            if row["path"] not in copied_paths:
                continue
            relative = _relative(row["path"])
            with _directory(root, relative.parent) as parent:
                descriptor, _, size = _snapshot_fd(parent, relative.name)
                digest = hashlib.sha256()
                try:
                    while chunk := os.read(descriptor, 1024 * 1024):
                        digest.update(chunk)
                finally:
                    os.close(descriptor)
            if size == row["size_bytes"] and digest.hexdigest() == row["sha256"]:
                verified.append(row)
        if not verified:
            return
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in verified:
                upload = conn.execute(
                    "SELECT state,sha256 FROM workspace_file_uploads "
                    "WHERE upload_id=? AND tenant_id=? AND owner_id=?",
                    (row["version_id"], target["tenant_id"], target["owner_id"]),
                ).fetchone()
                if upload is None or upload["state"] != "ready" or upload["sha256"] != row["sha256"]:
                    continue
                conn.execute(
                    "INSERT INTO workspace_file_entries "
                    "(file_id,tenant_id,owner_id,workspace_key,path,version_id) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(tenant_id,owner_id,workspace_key,path) DO UPDATE SET "
                    "version_id=excluded.version_id WHERE workspace_file_entries.version_id='' "
                    "AND workspace_file_entries.deleted=0",
                    ("fen_" + uuid.uuid4().hex, target["tenant_id"], target["owner_id"],
                     self._workspace_key(target), row["path"], row["version_id"]),
                )
                bound = conn.execute(
                    "SELECT version_id FROM workspace_file_entries WHERE tenant_id=? AND owner_id=? "
                    "AND workspace_key=? AND path=? AND deleted=0",
                    (target["tenant_id"], target["owner_id"], self._workspace_key(target), row["path"]),
                ).fetchone()
                if bound is not None and bound["version_id"] == row["version_id"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO workspace_file_pins VALUES (?,?,?)",
                        ("workspace", "duplicate:" + target["worker_id"], row["version_id"]),
                    )

    def control_lock_root(self, worker: dict) -> Path:
        worker = self._scope_worker(worker)
        scope = "\0".join(
            (worker["tenant_id"], worker["owner_id"], self._workspace_key(worker))
        )
        root = (
            Path(self.store.db_path).resolve().parent
            / "workspace-file-locks"
            / hashlib.sha256(scope.encode()).hexdigest()
        )
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise FileAdmissionError("Workspace control lock is unavailable", 503)
        root.chmod(0o700)
        return root

    @contextmanager
    def native_start_guard(self, worker: dict):
        """Compose after durable native run claim, inside the lifecycle guard."""
        from .workspace_file_mutations import _locked

        with _locked(self.control_lock_root(worker)):
            yield

    def _pending_binding_paths(self, conn, worker: dict) -> set[str]:
        rows = conn.execute(
            "SELECT worker_id,manifest_json FROM workspace_file_bindings WHERE tenant_id=? AND owner_id=?",
            (worker["tenant_id"], worker["owner_id"]),
        ).fetchall()
        root = self._owner_root(worker["tenant_id"], worker["owner_id"])
        return {
            item["path"]
            for row in rows
            for item in json.loads(row["manifest_json"])
            if (item.get("file_scope_id") == self._workspace_key(worker) if item.get("file_scope_id") else row["worker_id"] == worker["worker_id"])
            if not self.storage_adapter.completed(worker["tenant_id"], worker["owner_id"], item)
        }

    def bind(
        self,
        worker_id: str,
        tenant_id: str,
        owner_id: str,
        upload_ids: list[str],
        request_key: str,
        *,
        directory: str | None = None,
        revisions: list[str] | None = None,
    ) -> dict:
        worker = self._worker(worker_id, tenant_id, owner_id)
        with self.native_start_guard(worker):
            return self._bind(
                worker_id,
                tenant_id,
                owner_id,
                upload_ids,
                request_key,
                directory=directory,
                revisions=revisions,
            )

    def _bind(
        self,
        worker_id: str,
        tenant_id: str,
        owner_id: str,
        upload_ids: list[str],
        request_key: str,
        *,
        directory: str | None = None,
        revisions: list[str] | None = None,
    ) -> dict:
        from .service import merge_bootstrap_bundle

        if not request_key or len(request_key) > 128:
            raise FileAdmissionError("A file binding request key is required", 422)
        if len(set(upload_ids)) != len(upload_ids):
            raise FileAdmissionError("Each selected upload must have its own identity", 422)
        if revisions is not None and len(revisions) != len(upload_ids):
            raise FileAdmissionError("Each selected file needs its matching revision", 422)
        worker = self._worker(worker_id, tenant_id, owner_id)
        root = None
        request_directory = {"mode": "default"}
        if directory is not None:
            relative_dir = _relative(directory, directory=True)
            request_directory = {"mode": "directory", "path": relative_dir.as_posix()}
            root = self._root(worker)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM workspace_file_bindings WHERE tenant_id=? AND owner_id=? AND worker_id=? AND request_key=?",
                (tenant_id, owner_id, worker_id, request_key),
            ).fetchone()
            if existing:
                manifest = json.loads(existing["manifest_json"])
                self.scope_manifest(worker, manifest)
                if [item["upload_id"] for item in manifest] != upload_ids:
                    raise FileAdmissionError("This retry selected different files", 409)
                if revisions is not None and [item.get("revision") for item in manifest] != revisions:
                    raise FileAdmissionError(
                        "This retry selected different file versions", 409
                    )
                recorded_directory = (
                    json.loads(existing["request_directory_json"])
                    if existing["request_directory_json"] is not None
                    else _historical_binding_directory(manifest)
                )
                if recorded_directory != request_directory:
                    raise FileAdmissionError(
                        "This retry selected a different file destination; use a new request key",
                        409,
                    )
            else:
                manifest = self.manifest(tenant_id, owner_id, upload_ids, revisions)
                self.scope_manifest(worker, manifest)
                # Directory selection may create destination parents. Refuse
                # unavailable source authority before changing that workspace.
                from .workspace_file_storage import authorize_projection_source

                for entry in self.bootstrap_entries(tenant_id, owner_id, manifest):
                    authorize_projection_source(entry, worker)
                if directory is not None:
                    selected = self._pending_binding_paths(conn, worker)
                    access = lambda fd: self.apply_scope_access(worker, fd, is_directory=True)
                    with _directory(root, relative_dir, access=access) as parent:
                        for item in manifest:
                            selected_relative = _relative(
                                item.get("relative_path") or item["name"]
                            )
                            destination_dir = relative_dir / selected_relative.parent
                            with _directory(
                                root, destination_dir, create=True, access=access
                            ) as destination_parent:
                                candidate = item["name"]
                                number = 1
                                while (
                                    destination_dir / candidate
                                ).as_posix() in selected or self._exists_at(
                                    destination_parent, candidate
                                ):
                                    number += 1
                                    candidate = _keep_both_name(item["name"], number)
                                item["path"] = (destination_dir / candidate).as_posix()
                                selected.add(item["path"])
                self.reserve_projection_manifest(conn, tenant_id, owner_id, manifest)
            entries = self.bootstrap_entries(tenant_id, owner_id, manifest)
            from .workspace_file_storage import authorize_projection_source

            for entry in entries:
                authorize_projection_source(entry, worker)
            if not existing:
                register_input_file_versions(conn, worker, manifest)
                binding_id = "fbd_" + uuid.uuid4().hex
                pin_file_manifest(
                    conn,
                    kind="workspace",
                    reference_id=binding_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    manifest=manifest,
                )
                conn.execute(
                    "INSERT INTO workspace_file_bindings (binding_id,tenant_id,owner_id,worker_id,request_key,manifest_json,request_directory_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        binding_id,
                        tenant_id,
                        owner_id,
                        worker_id,
                        request_key,
                        json.dumps(manifest),
                        json.dumps(request_directory, sort_keys=True),
                    ),
                )
            # The accepted binding and worker projection context commit together.
            # Read the current bundle under the same writer transaction so two
            # simultaneous attachments cannot discard each other's manifests.
            current = conn.execute(
                "SELECT bootstrap_bundle_json,state FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",
                (worker_id, tenant_id, owner_id),
            ).fetchone()
            if current is None or current["state"] in {
                "terminating",
                "termination_failed",
                "terminated",
            }:
                raise FileAdmissionError("Workspace is closed", 409)
            old_bundle = json.loads(str(current["bootstrap_bundle_json"] or "{}"))
            from .store import utc_now

            conn.execute(
                "UPDATE workers SET bootstrap_bundle_json=?,updated_at=? WHERE worker_id=?",
                (
                    json.dumps(merge_bootstrap_bundle(old_bundle, {"files": entries})),
                    utc_now(),
                    worker_id,
                ),
            )
        if root is None:
            try:
                root = self._root(worker)
            except FileAdmissionError:
                root = None
        if root is not None:
            for entry in entries:
                materialize_managed_file(
                    root, entry, worker, self._owner_root(tenant_id, owner_id)
                )
        return {"items": manifest, "state": "available" if root else "accepted"}

    def artifact_worker(self, worker: dict) -> dict:
        with self.store._connect() as conn:
            return artifact_worker_context(conn, worker, files=self)

    def scope_manifest(self, worker: dict, manifest: list[dict], *, connection=None) -> None:
        scope_id = self._workspace_key(self._scope_worker(worker, connection=connection))
        for item in manifest:
            if item.get("file_scope_id") and item["file_scope_id"] != scope_id:
                raise FileAdmissionError("Accepted file scope changed; attach the files again", 409)
            item["file_scope_id"] = scope_id

    def register_run_input_versions(self, worker: dict, manifest: list[dict]) -> None:
        worker = self._scope_worker(worker)
        self.scope_manifest(worker, manifest)
        with self.store._connect() as conn:
            register_input_file_versions(conn, worker, manifest)

    @staticmethod
    def _exists_at(parent: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def list_files(
        self,
        worker_id: str,
        tenant_id: str,
        owner_id: str,
        *,
        directory: str = "",
        cursor: int = 0,
        limit: int = 100,
    ) -> dict:
        worker = self._worker(worker_id, tenant_id, owner_id, read_only=True)
        root = self._root(worker)
        from .workspace_file_mutations import FileMutations, directory_revision

        FileMutations(self, worker).recover()
        relative = _relative(directory, directory=True)
        if cursor < 0 or not 1 <= limit <= 500:
            raise FileAdmissionError("File page is invalid", 422)
        items = []
        directory_display_name = ""
        with _directory(root, relative) as parent:
            names = sorted(os.listdir(parent))
            for name in names:
                path = (relative / name).as_posix()
                try:
                    _relative(path)
                except FileAdmissionError:
                    continue
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not (
                    stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
                ):
                    continue
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                    continue
                items.append((name, path, metadata))
            page = items[cursor : cursor + limit]
            result = []
            with self.store._connect() as conn:
                workspace_key = self._workspace_key(worker)

                def accepted_input_name(folder_path: str) -> str:
                    parts = Path(folder_path).parts
                    if len(parts) != 2 or parts[0] != "inputs":
                        return ""
                    rows = conn.execute(
                        "SELECT e.path,u.relative_path,u.name FROM workspace_file_entries e "
                        "JOIN workspace_file_uploads u ON u.upload_id=e.version_id "
                        "AND u.tenant_id=e.tenant_id AND u.owner_id=e.owner_id "
                        "WHERE e.tenant_id=? AND e.owner_id=? AND e.workspace_key=? "
                        "AND e.version_id=? AND e.deleted=0",
                        (tenant_id, owner_id, workspace_key, parts[1]),
                    ).fetchall()
                    return next((str(row["relative_path"] or row["name"])
                                 for row in rows if row["path"].startswith(folder_path + "/")), "")

                directory_display_name = accepted_input_name(relative.as_posix())
                for name, path, metadata in page:
                    is_dir = stat.S_ISDIR(metadata.st_mode)
                    if is_dir:
                        revision = directory_revision(parent, name)
                        size = 0
                    else:
                        descriptor, revision, size = _snapshot_fd(parent, name)
                        os.close(descriptor)
                    file_id = "fen_" + uuid.uuid4().hex
                    conn.execute(
                        "INSERT OR IGNORE INTO workspace_file_entries (file_id,tenant_id,owner_id,workspace_key,path) VALUES (?,?,?,?,?)",
                        (
                            file_id,
                            tenant_id,
                            owner_id,
                            self._workspace_key(worker),
                            path,
                        ),
                    )
                    row = conn.execute(
                        "SELECT file_id FROM workspace_file_entries WHERE tenant_id=? AND owner_id=? AND workspace_key=? AND path=?",
                        (tenant_id, owner_id, self._workspace_key(worker), path),
                    ).fetchone()
                    conn.execute(
                        "UPDATE workspace_file_entries SET deleted=0 WHERE file_id=?",
                        (row["file_id"],),
                    )
                    item = {
                            "file_id": row["file_id"],
                            "path": path,
                            "name": name,
                            "is_dir": is_dir,
                            "size_bytes": size,
                            "revision": revision,
                    }
                    if is_dir:
                        display_name = accepted_input_name(path)
                        if display_name:
                            item["display_name"] = display_name
                    result.append(item)
        targets = {
            value.strip()
            for value in os.environ.get("GLASSHIVE_FILE_DRAG_OUT_TARGETS", "").split(
                ","
            )
            if value.strip()
        }
        if targets - {"chromium_macos", "chromium_windows"}:
            raise FileAdmissionError(
                "File drag-out capability configuration is invalid", 503
            )
        return {
            "items": result,
            "directory_display_name": directory_display_name,
            "next_cursor": cursor + len(page)
            if cursor + len(page) < len(items)
            else None,
            "can_write": worker.get("state") != "terminated",
            "drag_out_supported": bool(targets),
            "drag_out_targets": sorted(targets),
            "scope": {key: worker["_file_scope"].get(key) for key in ("scope_id", "kind", "workspace_id", "member_id")},
        }

    def _entry(self, conn, worker: dict, file_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM workspace_file_entries WHERE file_id=? AND tenant_id=? AND owner_id=? AND workspace_key=? AND deleted=0",
            (
                file_id,
                worker["tenant_id"],
                worker["owner_id"],
                self._workspace_key(worker),
            ),
        ).fetchone()
        if row is None:
            raise FileAdmissionError("File not found", 404)
        return dict(row)

    def get_file(self, worker_id, tenant_id, owner_id, file_id, *, revision=None):
        from .workspace_file_mutations import FileMutations, directory_revision
        worker = self._worker(worker_id, tenant_id, owner_id, read_only=True)
        FileMutations(self, worker).recover()
        with self.store._connect() as conn:
            entry = self._entry(conn, worker, file_id)
        relative = _relative(entry["path"])
        with _directory(self._root(worker), relative.parent) as parent:
            try:
                metadata = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise FileAdmissionError("File changed; refresh Files and retry", 409) from exc
            is_dir = stat.S_ISDIR(metadata.st_mode)
            if is_dir:
                current, size = directory_revision(parent, relative.name), 0
            else:
                descriptor, current, size = _snapshot_fd(parent, relative.name)
                os.close(descriptor)
        if revision is not None and current != revision:
            raise FileAdmissionError("The selected file version changed; refresh Files and retry", 409)
        return {"file_id": file_id, "path": relative.as_posix(), "name": relative.name,
                "is_dir": is_dir, "size_bytes": size, "revision": current}

    def open_download(
        self,
        worker_id: str,
        tenant_id: str,
        owner_id: str,
        file_id: str,
        *,
        revision: str | None = None,
    ) -> tuple[int, str, int]:
        worker = self._worker(worker_id, tenant_id, owner_id, read_only=True)
        with self.store._connect() as conn:
            entry = self._entry(conn, worker, file_id)
        relative = _relative(entry["path"])
        with _directory(self._root(worker), relative.parent) as parent:
            descriptor, current, size = _snapshot_fd(parent, relative.name)
            if revision is not None and current != revision:
                os.close(descriptor)
                raise FileAdmissionError(
                    "The selected file version changed; refresh Files and retry", 409
                )
        return descriptor, relative.name, size

    def mkdir(self, worker_id: str, tenant_id: str, owner_id: str, path: str) -> dict:
        worker = self._worker(worker_id, tenant_id, owner_id)
        relative = _relative(path)
        access = lambda fd: self.apply_scope_access(worker, fd, is_directory=True)
        with _directory(self._root(worker), relative.parent, access=access) as parent:
            try:
                os.mkdir(relative.name, 0o700, dir_fd=parent)
            except FileExistsError as exc:
                raise FileAdmissionError(
                    "A file or folder already has this name", 409
                ) from exc
            try:
                descriptor = os.open(relative.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    self.apply_scope_access(worker, descriptor, is_directory=True)
                finally:
                    os.close(descriptor)
            except BaseException:
                try:
                    os.rmdir(relative.name, dir_fd=parent)
                except OSError:
                    pass
                raise
        return {"path": relative.as_posix()}

    def mutate(
        self,
        worker_id: str,
        tenant_id: str,
        owner_id: str,
        file_id: str,
        *,
        revision: str,
        path: str | None = None,
        delete: bool = False,
    ) -> dict:
        from .workspace_file_mutations import FileMutations

        return FileMutations(self, self._worker(worker_id, tenant_id, owner_id)).mutate(
            file_id,
            revision=revision,
            path=path,
            delete=delete,
        )

    def undo(
        self, worker_id: str, tenant_id: str, owner_id: str, file_id: str, undo_id: str
    ) -> dict:
        from .workspace_file_mutations import FileMutations

        return FileMutations(self, self._worker(worker_id, tenant_id, owner_id)).undo(
            file_id, undo_id
        )

    def trash(self, worker_id: str, tenant_id: str, owner_id: str) -> dict:
        from .workspace_file_mutations import FileMutations

        return FileMutations(self, self._worker(worker_id, tenant_id, owner_id)).trash()


def materialize_managed_file(
    root: Path, entry: dict, worker: dict, staging_root: Path | None = None
) -> None:
    import fcntl
    from .workspace_file_storage import resolve_projection_control

    adapter, item, receipt = resolve_projection_control(entry, worker, root)
    tenant, owner = str(worker.get("tenant_id") or "local"), str(worker.get("owner_id") or "")
    if adapter.backend is not None:
        with adapter.files.store._connect() as conn:
            snapshot = adapter.snapshot(conn, tenant, owner)
        if not root.resolve().is_relative_to(Path(snapshot.root)):
            raise FileAdmissionError("Workspace projection is outside owner quota coverage", 503)
    with _directory(receipt.parent) as parent:
        lock_fd = os.open(receipt.stem + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
            raise FileAdmissionError("Projection control lock is invalid", 503)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if adapter.completed(tenant, owner, item):
            return
        trusted_worker = adapter.files._worker(str(worker["worker_id"]), tenant, owner)
        # A native member's worktree grants that member an inherited ACL. Stage
        # beside the accepted upload when it shares the destination filesystem,
        # so the unpublished copy stays behind the service-private owner root.
        owner_root = adapter.owner_root(tenant, owner)
        copy_stage_root = owner_root if adapter.backend is not None or owner_root.stat().st_dev == root.stat().st_dev else None
        _materialize_managed_file(
            root, entry, trusted_worker, staging_root, receipt=receipt, copy_stage_root=copy_stage_root,
            scope_access=lambda descriptor, is_directory: adapter.files.apply_scope_access(
                trusted_worker, descriptor, is_directory=is_directory,
            ),
        )
    except OSError as exc:
        raise adapter.failure(tenant, owner, exc) from exc
    finally:
        os.close(lock_fd)


def _materialize_managed_file(
    root: Path, entry: dict, worker: dict, staging_root: Path | None = None, *, receipt: Path,
    copy_stage_root: Path | None = None, scope_access=None,
) -> None:
    """Publish once from service-private staging, then commit the control receipt."""
    from .bootstrap import resolve_authorized_bootstrap_source_path

    relative = _relative(str(entry["path"]))
    source = resolve_authorized_bootstrap_source_path(entry, Path(str(entry["source_path"])), worker)
    projection_id = str(entry.get("managed_projection_id") or "")
    access_directory = (lambda descriptor: scope_access(descriptor, True)) if scope_access else None
    with _directory(root, relative.parent, create=True, access=access_directory) as parent:
        recovery_root = copy_stage_root if copy_stage_root is not None else root / ".xperfect-file-staging"
        if recovery_root.exists():
            with _directory(recovery_root) as recovery_parent:
                stage_name = ".xperfect-transfer-" + projection_id
                if WorkspaceFiles._exists_at(recovery_parent, stage_name) and WorkspaceFiles._exists_at(parent, relative.name):
                    staged = os.stat(stage_name, dir_fd=recovery_parent, follow_symlinks=False)
                    published = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISREG(staged.st_mode) and (staged.st_dev, staged.st_ino) == (published.st_dev, published.st_ino):
                        os.unlink(stage_name, dir_fd=recovery_parent)
        if WorkspaceFiles._exists_at(parent, relative.name):
            descriptor, _, size = _snapshot_fd(parent, relative.name)
            digest = hashlib.sha256()
            try:
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                if size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                    raise FileAdmissionError("An accepted input path was edited; move it before retrying this run", 409)
                if scope_access:
                    scope_access(descriptor, False)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            # ACL-granted staging stays inaccessible through its private parent
            # until an atomic link publishes it in the admitted physical scope.
            if copy_stage_root is None:
                with _directory(root, Path(".xperfect-file-staging"), create=True) as staging_parent:
                    stage_info = os.fstat(staging_parent)
                    if stage_info.st_uid != os.geteuid() or stage_info.st_mode & 0o077:
                        raise FileAdmissionError("File staging must be private to the service", 503)
                    stage_fd = os.dup(staging_parent)
            else:
                with _directory(copy_stage_root) as staging_parent:
                    stage_fd = os.dup(staging_parent)
            temporary = ".xperfect-transfer-" + projection_id
            temporary_created = False
            try:
                if WorkspaceFiles._exists_at(stage_fd, temporary):
                    stale = os.stat(temporary, dir_fd=stage_fd, follow_symlinks=False)
                    if not stat.S_ISREG(stale.st_mode) or stale.st_nlink != 1:
                        raise FileAdmissionError("Projection staging identity is invalid", 409)
                    os.unlink(temporary, dir_fd=stage_fd)
                source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    out_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage_fd)
                    temporary_created = True
                except BaseException:
                    os.close(source_fd)
                    raise
                digest, received = hashlib.sha256(), 0
                with os.fdopen(source_fd, "rb") as incoming, os.fdopen(out_fd, "wb") as outgoing:
                    metadata = os.fstat(incoming.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise FileAdmissionError("Accepted file version is unavailable", 409)
                    while chunk := incoming.read(1024 * 1024):
                        outgoing.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                    outgoing.flush()
                    if received != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                        raise FileAdmissionError("Accepted file version failed its content check", 409)
                    if scope_access:
                        scope_access(outgoing.fileno(), False)
                    os.fsync(outgoing.fileno())
                try:
                    os.link(temporary, relative.name, src_dir_fd=stage_fd, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError as exc:
                    raise FileAdmissionError("File destination changed; retry the transfer", 409) from exc
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise FileAdmissionError("Projection staging is outside the destination filesystem", 503) from exc
                    raise
            finally:
                try:
                    if temporary_created:
                        os.unlink(temporary, dir_fd=stage_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(stage_fd)
        os.fsync(parent)
    with _directory(receipt.parent) as receipt_parent:
        descriptor = os.open(receipt.name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=receipt_parent)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size:
                raise FileAdmissionError("Projection control receipt identity is invalid", 503)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(receipt_parent)


def logical_bytes(roots: list[Path]) -> int:
    total = 0
    unique_roots = sorted(
        {root.resolve(strict=False) for root in roots}, key=lambda root: len(root.parts)
    )
    visited_roots: list[Path] = []
    for root in unique_roots:
        if (
            any(
                root == parent or root.is_relative_to(parent)
                for parent in visited_roots
            )
            or not root.is_dir()
        ):
            continue
        visited_roots.append(root)
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                name for name in directories if not (Path(current) / name).is_symlink()
            ]
            for name in files:
                entry = Path(current) / name
                metadata = entry.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
    return total


def _safe_name(name: str) -> str:
    value = str(name or "")
    if not value or value in {".", ".."} or len(value.encode("utf-8")) > 255:
        raise FileAdmissionError("Choose a file with a valid name", 422)
    if any(character in value for character in ("/", "\\", "\x00")):
        raise FileAdmissionError("File name cannot contain a path", 422)
    return value
