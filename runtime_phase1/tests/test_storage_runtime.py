from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from workers_projects_runtime.storage_quota import OwnerStorageNotProvisioned, QuotaUnavailable
from workers_projects_runtime.storage_runtime import OwnerStorageRuntime
from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import WorkspaceFiles


@pytest.fixture
def policy(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_OWNER_STORAGE_BYTES', '8192')
    store = Store(str(tmp_path / 'state.db'))
    files = WorkspaceFiles(store)
    class Backend:
        def __init__(self):
            self.limit = None
            self.calls = []
            self.registry = SimpleNamespace(lookup=self.lookup)
        def lookup(self, *args):
            if self.limit is None: raise OwnerStorageNotProvisioned('missing')
            return 1
        def provision(self, tenant, owner, limit):
            self.calls.append(limit); self.limit = limit
            return self.snapshot(tenant, owner, limit)
        def snapshot(self, tenant, owner, limit):
            if limit != self.limit: raise QuotaUnavailable('mismatch')
            return SimpleNamespace(root=tmp_path / 'owner', limit_bytes=limit)
    backend = Backend()
    runtime = OwnerStorageRuntime(store, root=tmp_path / 'storage', registry_path=tmp_path / 'registry.db', backend=backend)
    runtime.files = files
    return runtime, files, backend


def test_owner_policy_applies_kernel_then_sql_and_readback(policy):
    runtime, files, backend = policy
    assert runtime.owner_snapshot('tenant', 'owner').limit_bytes == 8192
    runtime.update_policy('tenant', 'owner', {'storage_limit_bytes': 4096})
    with files.store._connect() as conn:
        assert files._policy(conn, 'tenant', 'owner')['storage_limit_bytes'] == 4096
    assert backend.calls == [8192, 4096]
    assert runtime.owner_snapshot('tenant', 'owner').limit_bytes == 4096
    with runtime.owner_lock('tenant', 'owner'):
        with runtime.owner_lock('tenant', 'owner'):
            assert runtime.owner_snapshot('tenant', 'owner').limit_bytes == 4096


def test_sql_commit_failure_rolls_back_kernel_policy(policy, monkeypatch):
    runtime, files, backend = policy
    runtime.owner_snapshot('tenant', 'owner')
    original = runtime.store._connect
    @contextmanager
    def fail_commit():
        with original() as conn:
            yield conn
            if backend.limit == 4096:
                raise RuntimeError('synthetic SQL commit failure')
    monkeypatch.setattr(runtime.store, '_connect', fail_commit)
    with pytest.raises(RuntimeError, match='SQL commit'):
        runtime.update_policy('tenant', 'owner', {'storage_limit_bytes': 4096})
    assert backend.calls == [8192, 4096, 8192]
    with original() as conn:
        assert files._policy(conn, 'tenant', 'owner')['storage_limit_bytes'] == 8192


def test_failed_kernel_rollback_durably_closes_owner_admission(policy, monkeypatch):
    runtime, files, backend = policy
    runtime.owner_snapshot('tenant', 'owner')
    def unavailable(*args):
        raise QuotaUnavailable('synthetic kernel operation failure')
    monkeypatch.setattr(backend, 'provision', unavailable)
    with pytest.raises(QuotaUnavailable, match='rollback failed'):
        runtime.update_policy('tenant', 'owner', {'storage_limit_bytes': 4096})
    with pytest.raises(QuotaUnavailable, match='recovery'):
        runtime.owner_snapshot('tenant', 'owner')
    with pytest.raises(QuotaUnavailable, match='recovery'):
        with runtime.owner_lock('tenant', 'owner'): pass
    with files.store._connect() as conn:
        assert files._policy(conn, 'tenant', 'owner')['storage_limit_bytes'] == 8192


def test_native_coverage_accepts_only_proven_absent_released_box(tmp_path):
    storage = object.__new__(OwnerStorageRuntime)
    storage.account_coverage = lambda *_: True
    worker = {'worker_id': 'wrk_closed', 'state': 'terminated',
              'execution_mode': 'docker', 'workspace_dir': '', 'home_dir': ''}
    storage._workers = lambda *_: [worker]
    box = SimpleNamespace(volume_root=tmp_path, supervisor=tmp_path / 'supervisor',
                          _inspect=lambda: None)
    storage.runtime = SimpleNamespace(_runtime_for_worker=lambda _: SimpleNamespace(
        sandbox=SimpleNamespace(box=box)))
    snapshot = SimpleNamespace(root=tmp_path)
    assert storage.native_coverage('tenant', 'owner', snapshot) is True
    box._inspect = lambda: {'Id': 'synthetic-present'}
    assert storage.native_coverage('tenant', 'owner', snapshot) is False
    box._inspect = lambda: (_ for _ in ()).throw(RuntimeError('inventory unproved'))
    with pytest.raises(RuntimeError, match='inventory unproved'):
        storage.native_coverage('tenant', 'owner', snapshot)
    box._inspect = lambda: None
    worker['state'] = 'paused'
    assert storage.native_coverage('tenant', 'owner', snapshot) is False
    worker['compute_released_at'] = '2026-09-23T00:00:00Z'
    assert storage.native_coverage('tenant', 'owner', snapshot) is True
    worker['compute_release_token'] = 'in-flight-release'
    assert storage.native_coverage('tenant', 'owner', snapshot) is False
    worker['compute_release_token'] = None
    worker['pid'] = 100
    assert storage.native_coverage('tenant', 'owner', snapshot) is False
    worker['pid'] = None
    worker['state'] = 'ready'
    assert storage.native_coverage('tenant', 'owner', snapshot) is True
    worker['state'] = 'running'
    assert storage.native_coverage('tenant', 'owner', snapshot) is False
    worker['state'] = 'paused'
    box._inspect = lambda: {'Id': 'synthetic-present'}
    assert storage.native_coverage('tenant', 'owner', snapshot) is False
    box._inspect = lambda: None
    worker['state'] = 'terminated'
    box.volume_root = tmp_path / 'foreign'
    assert storage.native_coverage('tenant', 'owner', snapshot) is False


def test_native_coverage_accepts_prepared_boxless_workspace_without_image_receipt(tmp_path):
    storage = object.__new__(OwnerStorageRuntime)
    storage.account_coverage = lambda *_: True
    storage._workers = lambda *_: [{
        'worker_id': 'wrk_prepared', 'state': 'paused', 'execution_mode': 'docker',
        'workspace_dir': str(tmp_path / 'worktree'), 'home_dir': str(tmp_path / 'home'),
        'compute_released_at': '2026-09-23T00:00:00Z', 'compute_release_token': None,
        'pid': None,
    }]
    box = SimpleNamespace(volume_root=tmp_path, supervisor=tmp_path / 'supervisor',
                          _inspect=lambda: None)
    storage.runtime = SimpleNamespace(_runtime_for_worker=lambda _: SimpleNamespace(
        sandbox=SimpleNamespace(box=box)))
    assert storage.native_coverage('tenant', 'owner', SimpleNamespace(root=tmp_path)) is True
