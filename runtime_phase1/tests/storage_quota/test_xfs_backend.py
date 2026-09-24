from dataclasses import replace
import errno
from pathlib import Path

import pytest

from workers_projects_runtime.storage_quota import (
    KernelQuota,
    MountIdentity,
    QuotaUnavailable,
    XfsProjectStorage,
)


class FakeKernel:
    """Deterministic boundary tests only; never native enforcement evidence."""
    def __init__(self, mount):
        self.mount = MountIdentity(mount, "fixture-device", "fixture-uuid", 4096, mount.stat().st_dev + 1)
        self.enforced = True
        self.quotas = {}
        self.projects = {}

    def inspect_mount(self, root):
        assert root == self.mount.root
        return self.mount

    def require_enforcement(self, mount):
        if not self.enforced:
            raise QuotaUnavailable("Project quota enforcement is unavailable")

    def get_quota(self, mount, project_id):
        return self.quotas.get(project_id, KernelQuota(project_id, 0, 0, 0, 0))

    def set_limit(self, mount, project_id, blocks):
        old = self.get_quota(mount, project_id)
        self.quotas[project_id] = replace(old, hard_limit_bytes=blocks * 512)

    def project_info(self, path):
        return self.projects.get(path, (0, False))

    def tag_new_root(self, path, project_id):
        self.projects[path] = (project_id, True)


@pytest.fixture
def fixture(tmp_path):
    mount = tmp_path / "data"
    mount.mkdir(mode=0o700)
    kernel = FakeKernel(mount)
    backend = XfsProjectStorage(mount, tmp_path / "control" / "quota.sqlite3", kernel=kernel)
    return backend, kernel


def test_provision_uses_same_owner_project_and_exact_decimal_limit(fixture):
    backend, kernel = fixture
    first = backend.provision("tenant", "owner", 5_000_000_000)
    second = backend.provision("tenant", "owner", 5_000_000_000)
    assert first == second
    assert first.limit_bytes == 5_000_000_000
    assert first.kernel_hard_limit_bytes == 4_999_999_488
    assert first.enforcement_scope == "owner_root"
    assert first.accounting_basis == "xfs_project_allocated_bytes"
    assert kernel.project_info(first.root) == (first.project_id, True)
    assert backend.provision("tenant", "other", 1024**2).project_id != first.project_id


def test_snapshot_verifies_live_kernel_and_expected_policy(fixture):
    backend, kernel = fixture
    provisioned = backend.provision("t", "o", 4096)
    kernel.quotas[provisioned.project_id] = replace(kernel.quotas[provisioned.project_id], used_bytes=4096)
    snapshot = backend.snapshot("t", "o", 4096)
    assert snapshot.used_allocated_bytes == 4096
    assert snapshot.available_bytes == 0
    kernel.enforced = False
    with pytest.raises(QuotaUnavailable):
        backend.snapshot("t", "o", 4096)


def test_existing_backend_opens_read_only_for_continuity(fixture):
    backend, kernel = fixture
    expected = backend.provision("t", "o", 4096)
    registry = backend.registry.path
    before = (registry.stat().st_size, registry.stat().st_mtime_ns)
    reader = XfsProjectStorage.open_existing(backend.root, registry, kernel=kernel)
    assert reader.snapshot("t", "o", 4096) == expected
    assert (registry.stat().st_size, registry.stat().st_mtime_ns) == before
    with pytest.raises(QuotaUnavailable):
        reader.registry.allocate("t", "new")


def test_snapshot_rejects_changed_limit_or_unexpected_inode_cap(fixture):
    backend, kernel = fixture
    value = backend.provision("t", "o", 4096)
    with pytest.raises(QuotaUnavailable, match="limit"):
        backend.snapshot("t", "o", 8192)
    kernel.quotas[value.project_id] = replace(kernel.quotas[value.project_id], inode_hard_limit=5)
    with pytest.raises(QuotaUnavailable, match="inode"):
        backend.snapshot("t", "o", 4096)


def test_unlimited_never_claims_hard_enforcement(fixture):
    backend, _ = fixture
    value = backend.provision("t", "o", None)
    assert value.limit_bytes is None
    assert value.hard_enforced is False


def test_zero_does_not_allocate_owner_or_change_quota(fixture):
    backend, kernel = fixture
    with pytest.raises(QuotaUnavailable):
        backend.provision("t", "o", 0)
    assert kernel.quotas == {}


def test_missing_provision_and_root_tag_drift_fail_closed(fixture):
    backend, kernel = fixture
    with pytest.raises(QuotaUnavailable):
        backend.snapshot("t", "o", 4096)
    value = backend.provision("t", "o", 4096)
    kernel.projects[value.root] = (value.project_id, False)
    with pytest.raises(QuotaUnavailable, match="project"):
        backend.snapshot("t", "o", 4096)


def test_registry_must_be_outside_quota_filesystem(tmp_path):
    mount = tmp_path / "data"
    mount.mkdir()
    with pytest.raises(QuotaUnavailable, match="outside"):
        XfsProjectStorage(mount, mount / "control.sqlite3", kernel=FakeKernel(mount))


def test_root_symlink_does_not_retag_foreign_data(fixture, tmp_path):
    backend, kernel = fixture
    value = backend.provision("t", "o", 4096)
    value.root.rmdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    value.root.symlink_to(foreign)
    with pytest.raises(QuotaUnavailable):
        backend.provision("t", "o", 4096)
    assert foreign not in kernel.projects


def test_existing_foreign_project_is_not_reassigned(fixture):
    backend, kernel = fixture
    value = backend.provision("t", "o", 4096)
    kernel.projects[value.root] = (999, True)
    with pytest.raises(QuotaUnavailable):
        backend.provision("t", "o", 4096)
    assert kernel.projects[value.root] == (999, True)


def test_kernel_limit_write_failure_leaves_no_claim_of_success(fixture, monkeypatch):
    backend, kernel = fixture
    def fail(*_):
        raise OSError(errno.EPERM, "denied")
    monkeypatch.setattr(kernel, "set_limit", fail)
    with pytest.raises(QuotaUnavailable):
        backend.provision("t", "o", 4096)


def test_owner_inputs_cannot_escape_generated_root(fixture):
    backend, _ = fixture
    value = backend.provision("../../tenant; command", "owner / with spaces", 4096)
    assert value.root.parent == backend.root / "owners"
    assert len(value.root.name) == 64


def test_policy_update_floors_native_granularity_before_mutation(fixture):
    backend, kernel = fixture
    original = backend.provision("t", "o", 8192)
    updated = backend.provision("t", "o", 5000)
    assert updated.limit_bytes == 5000
    assert updated.kernel_hard_limit_bytes == 4096
    assert kernel.quotas[original.project_id].hard_limit_bytes == 4096
    with pytest.raises(QuotaUnavailable):
        backend.provision("t", "o", 4095)
    assert kernel.quotas[original.project_id].hard_limit_bytes == 4096


def test_control_alias_on_same_device_is_rejected(tmp_path):
    mount = tmp_path / "data"
    mount.mkdir()
    kernel = FakeKernel(mount)
    kernel.mount = replace(kernel.mount, device_number=mount.stat().st_dev)
    with pytest.raises(QuotaUnavailable, match="filesystem"):
        XfsProjectStorage(mount, tmp_path / "control" / "registry.sqlite3", kernel=kernel)
