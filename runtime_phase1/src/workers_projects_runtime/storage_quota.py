"""Owner storage contracts. Quota administration belongs to a trusted helper, never a worker.

Kernel allocated-byte usage and logical file length are intentionally different metrics.
The registry is control state and must live outside the quota-controlled filesystem.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
from pathlib import Path
import sqlite3
import stat

DEFAULT_HOSTED_STORAGE_BYTES = 5_000_000_000
_UNSET = object()


class QuotaUnavailable(RuntimeError):
    """Fail closed without claiming an unsupported native enforcement capability."""


class OwnerStorageNotProvisioned(QuotaUnavailable):
    """This owner has no committed project ID yet."""


def effective_storage_limit(*, hosted: bool, configured=_UNSET) -> int | None:
    """Omitted means deployment default; explicit None means unlimited; zero stays zero."""
    if configured is _UNSET:
        return DEFAULT_HOSTED_STORAGE_BYTES if hosted else None
    if configured is not None and (type(configured) is not int or configured < 0):
        raise ValueError("Storage limit must be a nonnegative integer or None")
    return configured


def quota_blocks(limit_bytes: int | None) -> int:
    """XFS basic blocks are 512 bytes; never turn a small/zero cap into unlimited."""
    effective_storage_limit(hosted=False, configured=limit_bytes)
    if limit_bytes is None:
        return 0
    if limit_bytes < 512:
        raise QuotaUnavailable("A zero or sub-512-byte native quota is unsupported")
    if limit_bytes // 512 >= 2**64:
        raise ValueError("Storage limit exceeds the kernel quota range")
    return limit_bytes // 512


def _identity(tenant_id: str, owner_id: str) -> tuple[str, str]:
    for value in (tenant_id, owner_id):
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError("Tenant and owner must be nonempty identifiers without NUL")
    return tenant_id, owner_id


def _trusted_ancestry(directory: Path) -> None:
    if directory != directory.resolve(strict=True):
        raise QuotaUnavailable("Control path ancestor contains a symlink")
    for ancestor in (directory, *directory.parents):
        info = ancestor.stat()
        if info.st_uid not in {0, os.geteuid()}:
            raise QuotaUnavailable("Control path ancestor has an untrusted owner")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise QuotaUnavailable("Control path ancestor permits untrusted replacement")


class OwnerProjectRegistry:
    """Atomic, persistent, non-recycling IDs for a dedicated filesystem's project namespace.

    No release/delete operation: retained data must never inherit a new owner's ID.
    The operator must reserve this namespace exclusively for this registry.
    """

    def __init__(self, path: Path, filesystem_id: str):
        self.path = Path(path).absolute()
        if not filesystem_id:
            raise ValueError("A verified filesystem identity is required")
        if self.path != self.path.resolve(strict=False):
            raise QuotaUnavailable("Registry path must not contain symlinks")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _trusted_ancestry(self.path.parent)
        parent = self.path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise QuotaUnavailable("Registry directory must be trusted and not writable by others")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise QuotaUnavailable("Registry is unavailable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise QuotaUnavailable("Registry must be a private regular file")
            self._file_identity = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS storage_filesystem (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO storage_filesystem VALUES (1, ?)", (filesystem_id,))
            current = conn.execute("SELECT identity FROM storage_filesystem WHERE singleton=1").fetchone()[0]
            if current != filesystem_id:
                raise QuotaUnavailable("Storage filesystem identity changed")
            conn.execute("CREATE TABLE IF NOT EXISTS storage_owner_projects (project_id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(project_id > 0 AND project_id < 2147483648), tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, UNIQUE(tenant_id, owner_id))")

    @classmethod
    def open_existing(cls, path: Path, filesystem_id: str):
        """Open an installed project registry for a read-only continuity probe."""
        registry = cls.__new__(cls)
        registry.path = Path(path).absolute()
        if not filesystem_id or registry.path != registry.path.resolve(strict=True):
            raise QuotaUnavailable("Registry identity is unavailable")
        _trusted_ancestry(registry.path.parent)
        info = registry.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise QuotaUnavailable("Registry must be a private regular file")
        registry._file_identity = (info.st_dev, info.st_ino)
        registry._read_only = True
        with sqlite3.connect(registry.path.as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT identity FROM storage_filesystem WHERE singleton=1").fetchone()
            if row is None or row[0] != filesystem_id:
                raise QuotaUnavailable("Storage filesystem identity changed")
        return registry

    @contextmanager
    def _connect(self):
        _trusted_ancestry(self.path.parent)
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise QuotaUnavailable("Registry control file is unavailable") from exc
        if (not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self._file_identity
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise QuotaUnavailable("Registry control file identity changed")
        conn = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if getattr(self, "_read_only", False) else "?mode=rw"), uri=True, timeout=15)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def allocate(self, tenant_id: str, owner_id: str) -> int:
        if getattr(self, "_read_only", False):
            raise QuotaUnavailable("Read-only project registry cannot allocate")
        tenant_id, owner_id = _identity(tenant_id, owner_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO storage_owner_projects (tenant_id, owner_id) VALUES (?, ?)", (tenant_id, owner_id))
            return int(conn.execute("SELECT project_id FROM storage_owner_projects WHERE tenant_id=? AND owner_id=?", (tenant_id, owner_id)).fetchone()[0])

    def lookup(self, tenant_id: str, owner_id: str) -> int:
        tenant_id, owner_id = _identity(tenant_id, owner_id)
        with self._connect() as conn:
            row = conn.execute("SELECT project_id FROM storage_owner_projects WHERE tenant_id=? AND owner_id=?", (tenant_id, owner_id)).fetchone()
        if row is None:
            raise OwnerStorageNotProvisioned("Owner storage has not been provisioned")
        return int(row[0])


@dataclass(frozen=True)
class MountIdentity:
    root: Path
    device: str
    filesystem_id: str
    block_bytes: int
    device_number: int


@dataclass(frozen=True)
class KernelQuota:
    project_id: int
    hard_limit_bytes: int
    used_bytes: int
    inode_hard_limit: int = 0
    inode_soft_limit: int = 0
    used_inodes: int = 0
    soft_limit_bytes: int = 0


@dataclass(frozen=True)
class StorageQuotaSnapshot:
    project_id: int
    root: Path
    limit_bytes: int | None
    used_allocated_bytes: int
    filesystem_block_bytes: int
    hard_enforced: bool
    kernel_hard_limit_bytes: int | None
    accounting_basis: str = "xfs_project_allocated_bytes"
    enforcement_scope: str = "owner_root"

    @property
    def available_bytes(self) -> int | None:
        return None if self.kernel_hard_limit_bytes is None else max(0, self.kernel_hard_limit_bytes - self.used_allocated_bytes)


def _trusted_directory(path: Path) -> None:
    _trusted_ancestry(path)
    if path != path.resolve(strict=True):
        raise QuotaUnavailable("Storage control paths must not contain symlinks")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise QuotaUnavailable("Storage control directory ownership is unsafe")


class XfsProjectStorage:
    """Privileged helper API for an operator-owned dedicated XFS mount.

    Only generated owner roots are tagged. Runtime provisions all retained writable
    child roots here before exposing them; it must separately enforce container mount
    coverage, read-only rootfs and owner/member ACLs. This class cannot attest to those.
    """

    def __init__(self, root: Path, registry_path: Path, *, kernel=None):
        self.root = Path(root).absolute()
        _trusted_directory(self.root)
        if Path(registry_path).resolve(strict=False).is_relative_to(self.root):
            raise QuotaUnavailable("Control registry must be outside owner storage")
        if kernel is None:
            from .storage_quota_linux import LinuxXfsQuota
            kernel = LinuxXfsQuota()
        self.kernel = kernel
        self.mount = kernel.inspect_mount(self.root)
        kernel.require_enforcement(self.mount)
        control_parent = Path(registry_path).absolute().parent
        control_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _trusted_directory(control_parent)
        if control_parent.stat().st_dev == self.mount.device_number:
            raise QuotaUnavailable("Control state requires a separate filesystem from owner storage")
        self.registry = OwnerProjectRegistry(registry_path, self.mount.filesystem_id)
        self.owners_root = self.root / "owners"
        self.owners_root.mkdir(mode=0o700, exist_ok=True)
        _trusted_directory(self.owners_root)

    @classmethod
    def open_existing(cls, root: Path, registry_path: Path, *, kernel=None):
        """Attest an installed quota owner without provisioning or control writes."""
        storage = cls.__new__(cls)
        storage.root = Path(root).absolute()
        _trusted_directory(storage.root)
        if Path(registry_path).resolve(strict=True).is_relative_to(storage.root):
            raise QuotaUnavailable("Control registry must be outside owner storage")
        if kernel is None:
            from .storage_quota_linux import LinuxXfsQuota
            kernel = LinuxXfsQuota()
        storage.kernel = kernel
        storage.mount = kernel.inspect_mount(storage.root)
        kernel.require_enforcement(storage.mount)
        control_parent = Path(registry_path).absolute().parent
        _trusted_directory(control_parent)
        if control_parent.stat().st_dev == storage.mount.device_number:
            raise QuotaUnavailable("Control state requires a separate filesystem from owner storage")
        storage.registry = OwnerProjectRegistry.open_existing(registry_path, storage.mount.filesystem_id)
        storage.owners_root = storage.root / "owners"
        _trusted_directory(storage.owners_root)
        return storage

    @contextmanager
    def _lock(self):
        _trusted_directory(self.registry.path.parent)
        path = self.registry.path.with_suffix(".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _owner_root(self, tenant_id: str, owner_id: str) -> Path:
        _identity(tenant_id, owner_id)
        digest = hashlib.sha256(f"{tenant_id}\0{owner_id}".encode()).hexdigest()
        return self.owners_root / digest

    def _verify_mount(self):
        _trusted_directory(self.root)
        _trusted_directory(self.owners_root)
        _trusted_directory(self.registry.path.parent)
        if self.registry.path.parent.stat().st_dev == self.mount.device_number:
            raise QuotaUnavailable("Control state moved onto the owner filesystem")
        if self.kernel.inspect_mount(self.root) != self.mount:
            raise QuotaUnavailable("Storage mount identity changed")
        self.kernel.require_enforcement(self.mount)

    def _blocks(self, limit_bytes: int | None) -> int:
        quota_blocks(limit_bytes)
        if limit_bytes is None:
            return 0
        block = self.mount.block_bytes
        if block < 512 or block % 512 or limit_bytes < block:
            raise QuotaUnavailable("Limit is smaller than the native filesystem allocation block")
        return (limit_bytes // block) * (block // 512)

    def provision(self, tenant_id: str, owner_id: str, limit_bytes: int | None) -> StorageQuotaSnapshot:
        blocks = self._blocks(limit_bytes)
        root = self._owner_root(tenant_id, owner_id)
        try:
            with self._lock():
                self._verify_mount()
                project_id = self.registry.allocate(tenant_id, owner_id)
                created = not root.exists() and not root.is_symlink()
                if created:
                    current = self.kernel.get_quota(self.mount, project_id)
                    if current.used_bytes or current.used_inodes or current.hard_limit_bytes:
                        raise QuotaUnavailable("Project ID already has retained storage or policy")
                    root.mkdir(mode=0o700)
                _trusted_directory(root)
                current_project, inherited = self.kernel.project_info(root)
                if current_project == 0 and not any(root.iterdir()):
                    self.kernel.tag_new_root(root, project_id)
                elif current_project != project_id or not inherited:
                    raise QuotaUnavailable("Owner root project assignment changed")
                self.kernel.set_limit(self.mount, project_id, blocks)
                return self.snapshot(tenant_id, owner_id, limit_bytes)
        except OSError as exc:
            raise QuotaUnavailable("Native storage provisioning failed") from exc

    def snapshot(self, tenant_id: str, owner_id: str, limit_bytes: int | None) -> StorageQuotaSnapshot:
        blocks = self._blocks(limit_bytes)
        self._verify_mount()
        project_id = self.registry.lookup(tenant_id, owner_id)
        root = self._owner_root(tenant_id, owner_id)
        _trusted_directory(root)
        if root.stat().st_dev != self.root.stat().st_dev or self.kernel.project_info(root) != (project_id, True):
            raise QuotaUnavailable("Owner root project coverage is unavailable")
        quota = self.kernel.get_quota(self.mount, project_id)
        if quota.project_id != project_id or quota.hard_limit_bytes != blocks * 512 or quota.soft_limit_bytes:
            raise QuotaUnavailable("Kernel storage limit differs from requested policy")
        if quota.inode_hard_limit or quota.inode_soft_limit:
            raise QuotaUnavailable("Unexpected inode count policy")
        return StorageQuotaSnapshot(project_id, root, limit_bytes, quota.used_bytes,
                                    self.mount.block_bytes, limit_bytes is not None,
                                    None if limit_bytes is None else blocks * 512)
