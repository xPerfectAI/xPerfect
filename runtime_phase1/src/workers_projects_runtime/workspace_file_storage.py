"""Files adapter for an already-provisioned owner quota and private control state.

This module never provisions storage or starts workers. Runtime supplies those
boundaries; a kernel project snapshot alone is not full native coverage.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import time
import weakref
from pathlib import Path

from .workspace_files import FileAdmissionError, _directory, _relative, _snapshot_fd


_ADAPTERS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def ensure_storage_adapter_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS workspace_file_storage_config (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), quota_required INTEGER NOT NULL DEFAULT 0,
        receipt_migration_at REAL NOT NULL)""")
    conn.execute("INSERT OR IGNORE INTO workspace_file_storage_config VALUES (1,0,?)", (time.time(),))
    conn.execute("""CREATE TABLE IF NOT EXISTS workspace_file_projection_targets (
        projection_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        source_path TEXT NOT NULL, workspace_path TEXT NOT NULL, relative_path TEXT NOT NULL,
        workspace_device INTEGER NOT NULL, workspace_inode INTEGER NOT NULL,
        upload_id TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, scope_id TEXT NOT NULL DEFAULT '')""")
    if "scope_id" not in {row[1] for row in conn.execute("PRAGMA table_info(workspace_file_projection_targets)")}:
        conn.execute("ALTER TABLE workspace_file_projection_targets ADD COLUMN scope_id TEXT NOT NULL DEFAULT ''")


def _same_device(first: Path, second: Path) -> bool:
    return first.stat().st_dev == second.stat().st_dev


def _trusted_directory(path: Path) -> Path:
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise FileAdmissionError("File control path is not canonical", 503)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for ancestor in (path, *path.parents):
        metadata = ancestor.stat()
        if metadata.st_uid not in {0, os.geteuid()} or (
            metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX
        ):
            raise FileAdmissionError("File control path is not service-owned", 503)
    if not path.is_dir() or path.is_symlink():
        raise FileAdmissionError("File control directory is unavailable", 503)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise FileAdmissionError("File control directory must be private to the service", 503)
    return path


def _allocation(path: Path, *, private_staging: bool = False) -> int:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(metadata.st_mode) or (metadata.st_nlink != 1 and not private_staging):
        raise FileAdmissionError("File allocation identity is unavailable", 503)
    return metadata.st_blocks * 512


def round_allocation(size: int, block: int) -> int:
    return ((size + block - 1) // block) * block


def manifests(conn, tenant: str, owner: str):
    rows = conn.execute(
        "SELECT worker_id,manifest_json FROM workspace_file_bindings WHERE tenant_id=? AND owner_id=?", (tenant, owner)
    ).fetchall()
    rows += conn.execute(
        "SELECT worker_id,file_manifest_json AS manifest_json FROM scheduled_runs WHERE tenant_id=? AND owner_id=? AND state!='cancelled'", (tenant, owner)
    ).fetchall()
    rows += conn.execute(
        "SELECT r.worker_id,f.manifest_json FROM workspace_file_run_inputs f JOIN runs r ON r.run_id=f.run_id JOIN workers w ON w.worker_id=r.worker_id WHERE w.tenant_id=? AND w.owner_id=?", (tenant, owner)
    ).fetchall()
    for row in rows:
        for item in json.loads(row["manifest_json"]):
            yield row["worker_id"], item


def authorize_projection_source(entry: dict, worker: dict) -> Path:
    """Check source authority before a binding or projection target is durable."""
    from .auth import multi_user_security_enabled
    from .bootstrap import resolve_authorized_bootstrap_source_path, sign_bootstrap_source_path

    source = Path(str(entry.get("source_path") or ""))
    if multi_user_security_enabled() and not sign_bootstrap_source_path(
        source,
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id=str(worker.get("owner_id") or ""),
    ):
        raise FileAdmissionError(
            "Stored files cannot be attached until this server's source authorization is configured; upgrade this installation",
            503,
            code="stored_file_source_authority_unavailable",
        )
    try:
        return resolve_authorized_bootstrap_source_path(entry, source, worker)
    except PermissionError as exc:
        raise FileAdmissionError(
            "Stored file source authorization was rejected",
            403,
            code="stored_file_source_unauthorized",
        ) from exc
    except FileNotFoundError as exc:
        raise FileAdmissionError("Accepted file version is unavailable", 409) from exc


class FileStorageAdapter:
    def __init__(self, files, *, backend, managed_root_resolver, native_coverage, policy_updater, control_root):
        self.files = files
        self.backend = backend
        self.managed_root_resolver = managed_root_resolver
        self.native_coverage = native_coverage
        self.policy_updater = policy_updater
        self.control_root = Path(control_root) if control_root is not None else Path(files.store.db_path).resolve().parent / "file-control"
        self._root_cache = {}
        self._identity_cache = {}
        with files.store._connect() as conn:
            ensure_storage_adapter_schema(conn)
            config = conn.execute("SELECT * FROM workspace_file_storage_config WHERE singleton=1").fetchone()
            if config["quota_required"] and backend is None:
                raise FileAdmissionError("This Files store requires its configured quota backend", 503)
            if backend is not None:
                if managed_root_resolver is None:
                    raise FileAdmissionError("Quota Files requires a canonical managed-root resolver", 503)
                conn.execute("UPDATE workspace_file_storage_config SET quota_required=1 WHERE singleton=1")
            self.migration_at = config["receipt_migration_at"]
        key = str(Path(files.store.db_path).resolve())
        old = _ADAPTERS.get(key)
        if old is not None and old.backend is not None and backend is None:
            raise FileAdmissionError("The configured quota adapter cannot be replaced by logical accounting", 503)
        with files.store._connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_file_uploads'").fetchone():
                _ADAPTERS[key] = self
        # Store schedule transactions must use this same injected adapter.
        files.store._workspace_files_adapter = files

    def snapshot(self, conn, tenant: str, owner: str):
        if self.backend is None:
            return None
        policy = self.files._policy(conn, tenant, owner)["storage_limit_bytes"]
        try:
            snapshot = self.backend.snapshot(tenant, owner, policy)
        except Exception as exc:
            raise FileAdmissionError("Owner storage quota backend is unavailable", 503) from exc
        try:
            block = snapshot.filesystem_block_bytes
            effective = snapshot.kernel_hard_limit_bytes
            if type(block) is not int or block <= 0 or block % 512 or type(snapshot.used_allocated_bytes) is not int or snapshot.used_allocated_bytes < 0:
                raise FileAdmissionError("Owner storage allocation meter is invalid", 503)
            expected = None if policy is None else (policy // block) * block
            if (snapshot.limit_bytes != policy or effective != expected or
                    (policy is not None and (policy < block or snapshot.hard_enforced is not True)) or
                    snapshot.accounting_basis != "xfs_project_allocated_bytes" or
                    snapshot.enforcement_scope != "owner_root" or snapshot.project_id <= 0):
                raise FileAdmissionError("Owner storage quota does not match runtime policy", 503)
            root = Path(snapshot.root)
            if not root.is_absolute() or root != root.resolve(strict=True) or not root.is_dir():
                raise FileAdmissionError("Canonical owner storage root is unavailable", 503)
            identity = (snapshot.project_id, root, root.stat().st_dev)
            if (tenant, owner) in self._identity_cache and self._identity_cache[tenant, owner] != identity:
                raise FileAdmissionError("Owner storage quota identity changed", 503)
            control = _trusted_directory(self.control_root)
            if _same_device(root, control) or _same_device(root, Path(self.files.store.db_path).resolve().parent):
                raise FileAdmissionError("Files control state must be outside the quota filesystem", 503)
            self._identity_cache[tenant, owner] = identity
            return snapshot
        except FileAdmissionError:
            raise
        except Exception as exc:
            raise FileAdmissionError("Owner storage quota verification is unavailable", 503) from exc

    def owner_root(self, tenant: str, owner: str, *, snapshot=None) -> Path:
        if self.backend is None:
            return self.files._local_owner_root(tenant, owner)
        if snapshot is None:
            with self.files.store._connect() as conn:
                snapshot = self.snapshot(conn, tenant, owner)
        try:
            root = Path(self.managed_root_resolver(tenant, owner, snapshot))
            owner_root = Path(snapshot.root)
            if (not root.is_absolute() or root != root.resolve(strict=False) or
                    root == owner_root or not root.is_relative_to(owner_root)):
                raise FileAdmissionError("Managed files are outside the canonical owner root", 503)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = root.stat()
            if root.is_symlink() or metadata.st_dev != owner_root.stat().st_dev:
                raise FileAdmissionError("Managed files are outside the owner filesystem", 503)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise FileAdmissionError("Managed file versions must be private to the service", 503)
            if (tenant, owner) in self._root_cache and self._root_cache[tenant, owner] != root:
                raise FileAdmissionError("Managed file root changed", 503)
            self._root_cache[tenant, owner] = root
            return root
        except FileAdmissionError:
            raise
        except Exception as exc:
            raise FileAdmissionError("Managed file root resolution is unavailable", 503) from exc

    def receipt_root(self, tenant: str, owner: str) -> Path:
        key = hashlib.sha256(f"{tenant}\0{owner}".encode()).hexdigest()
        _trusted_directory(self.control_root)
        root = _trusted_directory(self.control_root / key)
        if self.backend is None and (root == self.files.private_root or root.is_relative_to(self.files.private_root)):
            raise FileAdmissionError("Projection controls must be outside managed payload storage", 503)
        return root

    def receipt(self, tenant: str, owner: str, projection_id: str) -> Path:
        import uuid
        try:
            if not projection_id.startswith("prj_") or len(projection_id) != 36:
                raise ValueError()
            uuid.UUID(hex=projection_id[4:])
        except (ValueError, AttributeError) as exc:
            raise FileAdmissionError("Accepted file projection identity is invalid", 409) from exc
        return self.receipt_root(tenant, owner) / f"{projection_id}.receipt"

    def completed(self, tenant: str, owner: str, item: dict, *, owner_root: Path | None = None) -> bool:
        receipt = self.receipt(tenant, owner, item["projection_id"])
        if receipt.exists() or receipt.is_symlink():
            metadata = receipt.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size or metadata.st_uid != os.geteuid():
                raise FileAdmissionError("File projection control receipt is invalid", 503)
            return True
        # Read only pre-migration receipts at the exact accepted source lineage.
        legacy = (owner_root if owner_root is not None else self.owner_root(tenant, owner)) / f"{item['projection_id']}.receipt"
        if legacy.exists() or legacy.is_symlink():
            metadata = legacy.lstat()
            with self.files.store._connect() as conn:
                upload = conn.execute("SELECT created_at FROM workspace_file_uploads WHERE upload_id=? AND tenant_id=? AND owner_id=?", (item["upload_id"], tenant, owner)).fetchone()
            if (upload is not None and upload[0] <= self.migration_at and metadata.st_mtime <= self.migration_at and
                    stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and metadata.st_size == 0):
                return True
        return False

    def reservation(self, conn, tenant: str, owner: str, snapshot) -> int:
        root = self.owner_root(tenant, owner, snapshot=snapshot)
        block = snapshot.filesystem_block_bytes
        reserved = 0
        for row in conn.execute("SELECT upload_id,size_bytes FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND state IN ('pending','receiving')", (tenant, owner)):
            allocated = _allocation(root / f"{row['upload_id']}.part")
            reserved += max(0, round_allocation(row["size_bytes"], block) - allocated)
        seen = set()
        for worker_id, item in manifests(conn, tenant, owner):
            projection = item.get("projection_id")
            if not projection or projection in seen:
                continue
            seen.add(projection)
            if self.completed(tenant, owner, item, owner_root=root):
                continue
            temporary = root / f".xperfect-transfer-{projection}"
            allocated = _allocation(temporary, private_staging=True)
            worker = self.files.store.get_worker(worker_id, tenant, owner)
            if worker:
                try:
                    workspace = self.files._root(worker)
                except FileAdmissionError as exc:
                    if exc.status_code != 409:
                        raise
                    workspace = None
                if workspace is not None:
                    if not workspace.resolve().is_relative_to(Path(snapshot.root)):
                        raise FileAdmissionError("Workspace copy is outside owner quota coverage", 503)
                    relative = _relative(item["path"])
                    destination = workspace / relative
                    if allocated < round_allocation(item["size_bytes"], block) and destination.exists():
                        with _directory(workspace, relative.parent) as parent:
                            descriptor, _, size = _snapshot_fd(parent, relative.name)
                            digest = hashlib.sha256()
                            try:
                                while chunk := os.read(descriptor, 1024 * 1024):
                                    digest.update(chunk)
                                published_allocation = os.fstat(descriptor).st_blocks * 512
                            finally:
                                os.close(descriptor)
                        if digest.hexdigest() == item["sha256"] and size == item["size_bytes"]:
                            allocated += published_allocation
            reserved += max(0, round_allocation(item["size_bytes"], block) - allocated)
        return reserved

    def failure(self, tenant: str, owner: str, error: OSError):
        if error.errno not in {errno.ENOSPC, errno.EDQUOT}:
            return error
        if self.backend is not None:
            with self.files.store._connect() as conn:
                snapshot = self.snapshot(conn, tenant, owner)
            limit = snapshot.kernel_hard_limit_bytes
            if limit is not None and limit - snapshot.used_allocated_bytes < snapshot.filesystem_block_bytes:
                return FileAdmissionError(
                    "Owner storage quota is full; free space and retry", 413,
                    code="owner_storage_insufficient",
                )
            if error.errno == errno.EDQUOT:
                return FileAdmissionError(
                    "An operating-system quota blocked this write; operator review is needed", 507,
                    code="storage_quota_blocked",
                )
            try:
                free = os.statvfs(snapshot.root)
            except OSError as exc:
                raise FileAdmissionError("Filesystem capacity verification is unavailable", 503) from exc
            if free.f_bavail * free.f_frsize < snapshot.filesystem_block_bytes:
                return FileAdmissionError(
                    "The storage filesystem is full; ask the operator to free space", 507,
                    code="storage_filesystem_full",
                )
            return FileAdmissionError(
                "Storage allocation failed; quota and filesystem capacity need review", 507,
                code="storage_allocation_failed",
            )
        return FileAdmissionError(
            "Storage allocation failed; free space or review storage capacity", 507,
            code="storage_allocation_failed",
        )


def resolve_projection_control(entry: dict, worker: dict, workspace_root: Path):
    """Resolve trusted DB lineage; never read a caller-supplied key or control path."""
    if any(key in entry for key in ("managed_receipt_path", "managed_control_root", "managed_receipt_key")):
        raise FileAdmissionError("Caller-supplied projection control paths are forbidden", 403)
    tenant, owner = str(worker.get("tenant_id") or "local"), str(worker.get("owner_id") or "")
    accepted = []
    for adapter in list(_ADAPTERS.values()):
        if getattr(adapter.files.store, "_lifetime_connection", False) is None:
            continue
        with adapter.files.store._connect() as conn:
            upload = conn.execute("SELECT * FROM workspace_file_uploads WHERE upload_id=? AND tenant_id=? AND owner_id=?", (entry.get("managed_upload_id"), tenant, owner)).fetchone()
            if upload is None:
                continue
            stored_worker = adapter.files._worker(str(worker.get("worker_id") or ""), tenant, owner)
            scope_id = adapter.files._workspace_key(stored_worker)
            matches = [item for worker_id, item in manifests(conn, tenant, owner)
                       if item.get("projection_id") == entry.get("managed_projection_id")
                       and (worker_id == worker.get("worker_id") or item.get("file_scope_id") == scope_id)]
        if not matches:
            continue
        expected_source = adapter.owner_root(tenant, owner) / f"{upload['upload_id']}.blob"
        if str(expected_source) != str(entry.get("source_path")):
            continue
        expected = (entry.get("managed_upload_id"), entry.get("path"), entry.get("sha256"), entry.get("bytes"))
        if any((item["upload_id"], item["path"], item["sha256"], item["size_bytes"]) != expected for item in matches):
            raise FileAdmissionError("Projection does not match its accepted owner manifest", 403)
        if any((item.get("file_scope_id") or str(worker["worker_id"])) != scope_id for item in matches):
            raise FileAdmissionError("Accepted file scope changed before projection", 409)
        try:
            canonical_workspace = adapter.files._root(stored_worker).resolve(strict=True)
        except Exception as exc:
            raise FileAdmissionError("The canonical projection workspace is not prepared", 503) from exc
        if workspace_root.resolve(strict=True) != canonical_workspace or workspace_root.is_symlink():
            raise FileAdmissionError("Projection destination does not match its canonical workspace", 403)
        metadata = canonical_workspace.stat()
        identity = (str(entry.get("managed_projection_id")), tenant, owner, str(expected_source),
                    str(canonical_workspace), entry["path"], metadata.st_dev, metadata.st_ino,
                    entry["managed_upload_id"], entry["sha256"], entry["bytes"], adapter.files._workspace_key(stored_worker))
        accepted.append((adapter, matches[0], identity))
    if len(accepted) != 1:
        raise FileAdmissionError("A unique initialized Files control owner is required for this projection", 503)
    adapter, item, identity = accepted[0]
    authorize_projection_source(entry, worker)
    with adapter.files.store._connect() as conn:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO workspace_file_projection_targets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", identity)
        current = conn.execute("SELECT * FROM workspace_file_projection_targets WHERE projection_id=?", (item["projection_id"],)).fetchone()
        if tuple(current) != identity:
            raise FileAdmissionError("Projection is already bound to another workspace root or version", 409)
    return adapter, item, adapter.receipt(tenant, owner, item["projection_id"])
