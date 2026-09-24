from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from workers_projects_runtime.storage_quota import (
    DEFAULT_HOSTED_STORAGE_BYTES,
    OwnerProjectRegistry,
    QuotaUnavailable,
    effective_storage_limit,
    quota_blocks,
)


def test_hosted_default_is_decimal_five_gb_and_local_is_unlimited():
    assert DEFAULT_HOSTED_STORAGE_BYTES == 5_000_000_000
    assert effective_storage_limit(hosted=True) == 5_000_000_000
    assert effective_storage_limit(hosted=False) is None
    assert effective_storage_limit(hosted=True, configured=1234) == 1234
    assert effective_storage_limit(hosted=True, configured=None) is None


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "100"])
def test_storage_policy_rejects_invalid_values(invalid):
    with pytest.raises(ValueError):
        effective_storage_limit(hosted=True, configured=invalid)


def test_explicit_zero_is_not_kernel_unlimited():
    assert effective_storage_limit(hosted=True, configured=0) == 0
    with pytest.raises(QuotaUnavailable, match="zero"):
        quota_blocks(0)


def test_quota_units_never_round_up_past_policy():
    assert quota_blocks(5_000_000_000) == 9_765_625
    assert quota_blocks(1025) == 2
    assert quota_blocks(None) == 0
    with pytest.raises(QuotaUnavailable):
        quota_blocks(511)


def test_registry_is_stable_across_reopen_and_distinguishes_tenants(tmp_path):
    path = tmp_path / "control" / "storage.sqlite3"
    first = OwnerProjectRegistry(path, "filesystem-one")
    alice = first.allocate("tenant-a", "owner-a")
    assert first.allocate("tenant-a", "owner-a") == alice
    assert OwnerProjectRegistry(path, "filesystem-one").allocate("tenant-a", "owner-a") == alice
    assert first.allocate("tenant-b", "owner-a") != alice
    assert first.allocate("tenant-a", "owner-b") != alice
    assert 0 < alice < 2**31


def test_registry_concurrent_allocation_is_unique_and_same_owner_is_stable(tmp_path):
    path = tmp_path / "control" / "storage.sqlite3"
    registry = OwnerProjectRegistry(path, "filesystem-one")
    with ThreadPoolExecutor(max_workers=8) as pool:
        same = list(pool.map(lambda _: registry.allocate("t", "same"), range(24)))
        different = list(pool.map(lambda n: registry.allocate("t", str(n)), range(24)))
    assert len(set(same)) == 1
    assert len(set(different)) == 24
    assert same[0] not in different


def test_registry_rejects_filesystem_replacement(tmp_path):
    path = tmp_path / "control" / "storage.sqlite3"
    OwnerProjectRegistry(path, "filesystem-one")
    with pytest.raises(QuotaUnavailable, match="identity"):
        OwnerProjectRegistry(path, "filesystem-two")


def test_registry_does_not_follow_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserve")
    link = tmp_path / "registry.sqlite3"
    link.symlink_to(target)
    with pytest.raises(QuotaUnavailable):
        OwnerProjectRegistry(link, "filesystem-one")
    assert target.read_text() == "preserve"


@pytest.mark.parametrize("tenant,owner", [("", "a"), ("a", ""), ("a\0b", "c"), ("a", 12)])
def test_registry_rejects_ambiguous_owner_identity(tmp_path, tenant, owner):
    registry = OwnerProjectRegistry(tmp_path / "registry.sqlite3", "filesystem-one")
    with pytest.raises(ValueError):
        registry.allocate(tenant, owner)


def test_writable_nonsticky_ancestor_cannot_host_registry(tmp_path):
    untrusted = tmp_path / "untrusted"
    untrusted.mkdir(mode=0o777)
    untrusted.chmod(0o777)
    with pytest.raises(QuotaUnavailable, match="ancestor"):
        OwnerProjectRegistry(untrusted / "control" / "registry.sqlite3", "filesystem-one")


def test_registry_rechecks_path_after_parent_replacement(tmp_path):
    path = tmp_path / "control" / "registry.sqlite3"
    registry = OwnerProjectRegistry(path, "filesystem-one")
    registry.allocate("t", "o")
    path.parent.rename(tmp_path / "original")
    other = tmp_path / "other"
    other.mkdir()
    path.parent.symlink_to(other, target_is_directory=True)
    with pytest.raises(QuotaUnavailable):
        registry.allocate("t", "o")
    assert not (other / "registry.sqlite3").exists()
