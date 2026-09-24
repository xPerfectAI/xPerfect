"""Trusted owner placement and kernel-first quota transitions for native work."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from threading import local

from .storage_quota import OwnerStorageNotProvisioned, QuotaUnavailable, XfsProjectStorage


class OwnerStorageRuntime:
    def __init__(self, store, *, root, registry_path, backend=None):
        self.store = store
        self.backend = backend or XfsProjectStorage(root=Path(root), registry_path=Path(registry_path))
        self.root = Path(root)
        self.control_root = Path(registry_path).parent / 'runtime-storage'
        self.control_root.mkdir(mode=0o700, exist_ok=True)
        self._held = local()
        self.files = None
        self.runtime = None
        self.account_coverage = None

    @classmethod
    def from_environment(cls, store):
        root = os.environ.get('XPERFECT_STORAGE_ROOT')
        registry = os.environ.get('XPERFECT_STORAGE_REGISTRY_PATH')
        if not root and not registry:
            return None
        if not root or not registry:
            raise QuotaUnavailable('Both owner storage root and private registry are required')
        return cls(store, root=root, registry_path=registry)

    @staticmethod
    def _key(tenant_id, owner_id):
        return hashlib.sha256(json.dumps([tenant_id, owner_id]).encode()).hexdigest()

    def _failure_path(self, tenant_id, owner_id):
        return self.control_root / (self._key(tenant_id, owner_id) + '.unavailable')

    @contextmanager
    def owner_lock(self, tenant_id, owner_id):
        key = (tenant_id, owner_id)
        held = getattr(self._held, 'owners', set())
        if key in held:
            yield
            return
        info = self.control_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise QuotaUnavailable('Owner storage control directory is unsafe')
        fd = os.open(self.control_root / (self._key(tenant_id, owner_id) + '.lock'),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise QuotaUnavailable('Owner storage transition lock is unsafe')
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self._failure_path(tenant_id, owner_id).exists():
                raise QuotaUnavailable('Owner storage requires policy recovery')
            self._held.owners = held | {key}
            try:
                yield
            finally:
                self._held.owners = held
        finally:
            os.close(fd)

    def snapshot(self, tenant_id, owner_id, limit_bytes):
        # Files may hold the Store transaction here. Do not invert its lock order
        # against kernel-first policy updates; the backend serializes provisioning.
        if self._failure_path(tenant_id, owner_id).exists():
            raise QuotaUnavailable('Owner storage requires policy recovery')
        try:
            self.backend.registry.lookup(tenant_id, owner_id)
        except OwnerStorageNotProvisioned:
            return self.backend.provision(tenant_id, owner_id, limit_bytes)
        return self.backend.snapshot(tenant_id, owner_id, limit_bytes)

    def owner_snapshot(self, tenant_id, owner_id):
        if self.files is None:
            raise QuotaUnavailable('Owner storage policy is not initialized')
        with self.store._connect() as conn:
            policy = self.files._policy(conn, tenant_id, owner_id)
        return self.snapshot(tenant_id, owner_id, policy['storage_limit_bytes'])

    def owner_root(self, tenant_id, owner_id):
        return self.owner_snapshot(tenant_id, owner_id).root

    @staticmethod
    def managed_root(tenant_id, owner_id, snapshot):
        return snapshot.root / 'managed-files'

    def _workers(self, tenant_id, owner_id, conn=None):
        if conn is None:
            with self.store._connect() as connection:
                return self._workers(tenant_id, owner_id, connection)
        return [dict(row) for row in conn.execute('SELECT * FROM workers WHERE tenant_id=? AND owner_id=?', (tenant_id, owner_id))]

    def provider_account_coverage(self, binder, tenant_id, owner_id, snapshot):
        homes = binder.homes
        if homes.owner_root_resolver != self.owner_root or binder.store is None:
            return False
        # Missing managed setup capability fails before native launch; it is not
        # an unguarded writer. An enabled launcher must attest its own coverage.
        launcher = homes.native_launcher
        if launcher is not None:
            verify = getattr(launcher, "verify_owner_storage", None)
            if not callable(verify) or verify(tenant_id, owner_id, snapshot) is not True:
                return False
        with binder.store._connect() as conn:
            accounts = conn.execute("SELECT account_id FROM provider_accounts WHERE tenant_id=? AND owner_id=?",
                                    (tenant_id, owner_id)).fetchall()
        for account in accounts:
            path = homes.account_home_path(tenant_id=tenant_id, owner_id=owner_id, account_id=account["account_id"])
            if not path.is_relative_to(snapshot.root / 'provider-accounts'):
                return False
        return True

    def native_coverage(self, tenant_id, owner_id, snapshot):
        if self.runtime is None or self.account_coverage is None or not self.account_coverage(tenant_id, owner_id, snapshot):
            return False
        for worker in self._workers(tenant_id, owner_id):
            if worker['execution_mode'] != 'docker':
                return False
            for field in ('workspace_dir', 'home_dir'):
                raw = worker.get(field)
                if raw and not Path(raw).is_relative_to(snapshot.root):
                    return False
            native = self.runtime._runtime_for_worker(worker)
            sandbox = getattr(native, 'sandbox', None)
            if sandbox is None or not hasattr(sandbox, 'box') or sandbox.box.volume_root != snapshot.root:
                return False
            # A completed close or compute release can leave a durable
            # workspace without a box. Absence is proved by Docker inventory;
            # the release marker alone cannot excuse a live or unknown box.
            inspected = sandbox.box._inspect()
            if inspected is None:
                if worker['state'] == 'terminated':
                    continue
                if (worker['state'] in {'paused', 'ready', 'needs_input', 'failed'}
                        and worker.get('compute_released_at')
                        and not worker.get('compute_release_token')
                        and worker.get('pid') is None):
                    continue
                return False
            if worker['state'] == 'terminated':
                return False
            # A present box still needs its image receipt and full isolation
            # attestation from _inspect. Meter reads remain read-only at quota.
            if not (sandbox.box.supervisor / 'image.json').is_file():
                return False
        return True

    def _require_quiescent(self, tenant_id, owner_id):
        from .workspace_files import FileAdmissionError
        # The owner admission lock is held, but no Store transaction is held:
        # runtime identity lookup may itself need an independent Store write.
        for worker in self._workers(tenant_id, owner_id):
            native = self.runtime._runtime_for_worker(worker) if self.runtime is not None else None
            sandbox = getattr(native, 'sandbox', None)
            if sandbox is None or not hasattr(sandbox, 'box'):
                raise QuotaUnavailable('Native writer coverage is unavailable')
            info = sandbox.inspect(worker['worker_id'])
            if info is not None and info.state not in {'paused', 'stopped'}:
                raise FileAdmissionError('Pause active workers before lowering owner storage', 409)
        binder = getattr(self.runtime, 'provider_account_binder', None)
        if binder is not None and binder.store is not None:
            with binder.store._connect() as conn:
                active = conn.execute('SELECT 1 FROM provider_account_leases WHERE tenant_id=? AND owner_id=? AND released_at IS NULL LIMIT 1', (tenant_id, owner_id)).fetchone()
            if active:
                raise FileAdmissionError('Finish account setup and active account work before lowering storage', 409)

    def update_policy(self, tenant_id, owner_id, values, *, administrator=False, inherit=False):
        from .workspace_files import FileAdmissionError
        fields = ('storage_limit_bytes', 'max_file_bytes', 'max_batch_files', 'max_batch_bytes')
        if self.files is None:
            raise QuotaUnavailable('Owner policy is not initialized')
        with self.owner_lock(tenant_id, owner_id):
            with self.store._connect() as conn:
                current = self.files._policy(conn, tenant_id, owner_id)
                if inherit:
                    if not administrator:
                        raise FileAdmissionError('An administrator must restore deployment limits', 403)
                    conn.execute('SAVEPOINT deployment_policy')
                    conn.execute('DELETE FROM workspace_file_policies WHERE tenant_id=? AND owner_id=?', (tenant_id, owner_id))
                    proposed = self.files._policy(conn, tenant_id, owner_id)
                    conn.execute('ROLLBACK TO deployment_policy')
                    conn.execute('RELEASE deployment_policy')
                else:
                    proposed = {field: values.get(field, current[field]) for field in fields}
            if not administrator:
                for field in fields:
                    ceiling = current[field]
                    if ceiling is not None and (proposed[field] is None or proposed[field] > ceiling):
                        raise FileAdmissionError('An administrator must increase this limit', 403)
            old_limit, new_limit = current['storage_limit_bytes'], proposed['storage_limit_bytes']
            if new_limit is not None and (old_limit is None or new_limit < old_limit):
                self._require_quiescent(tenant_id, owner_id)
            kernel_attempted = False
            try:
                with self.store._connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    latest = self.files._policy(conn, tenant_id, owner_id)
                    if any(latest[field] != current[field] for field in fields):
                        raise FileAdmissionError('Storage policy changed; refresh and retry', 409)
                    kernel_attempted = True
                    self.backend.provision(tenant_id, owner_id, new_limit)
                    if inherit:
                        conn.execute('DELETE FROM workspace_file_policies WHERE tenant_id=? AND owner_id=?', (tenant_id, owner_id))
                    else:
                        conn.execute('INSERT INTO workspace_file_policies VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,owner_id) DO UPDATE SET storage_limit_bytes=excluded.storage_limit_bytes,max_file_bytes=excluded.max_file_bytes,max_batch_files=excluded.max_batch_files,max_batch_bytes=excluded.max_batch_bytes',
                                     (tenant_id, owner_id, *(proposed[field] for field in fields)))
            except BaseException:
                if kernel_attempted:
                    try:
                        self.backend.provision(tenant_id, owner_id, old_limit)
                    except Exception:
                        path = self._failure_path(tenant_id, owner_id)
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, 'w') as output:
                            output.write('policy rollback requires recovery\n'); output.flush(); os.fsync(output.fileno())
                        directory = os.open(self.control_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try: os.fsync(directory)
                        finally: os.close(directory)
                        raise QuotaUnavailable('Kernel storage rollback failed; owner remains unavailable') from None
                raise
