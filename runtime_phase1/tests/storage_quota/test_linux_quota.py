import struct

import pytest

from workers_projects_runtime.storage_quota import QuotaUnavailable
from workers_projects_runtime.storage_quota_linux import decode_quota, require_project_flags


def record(*, version=1, flags=2, project=17, hard=9_765_625, used=8, rt=0):
    return struct.pack("=bbHI6QiiHH4b3QiHh8s", version, flags, 0, project,
                       hard, 0, 0, 0, used, 2, 0, 0, 0, 0, 0, 0, 0, 0,
                       rt, 0, 0, 0, 0, 0, b"\0" * 8)


def test_native_quota_abi_reads_exact_bytes_not_rounded_cli_text():
    data = record()
    assert len(data) == 112
    result = decode_quota(data, 17)
    assert result.hard_limit_bytes == 5_000_000_000
    assert result.used_bytes == 4096
    assert result.used_inodes == 2


@pytest.mark.parametrize("kwargs", [{"version": 0}, {"flags": 1}, {"project": 18}, {"rt": 1}])
def test_invalid_or_unsupported_kernel_record_is_not_enforcement(kwargs):
    with pytest.raises(QuotaUnavailable):
        decode_quota(record(**kwargs), 17)


@pytest.mark.parametrize("flags", [0, 0x10, 0x20])
def test_accounting_without_enforcement_is_not_capability(flags):
    with pytest.raises(QuotaUnavailable):
        require_project_flags(flags)


def test_both_kernel_accounting_and_enforcement_required():
    require_project_flags(0x30)


def test_truncated_kernel_record_is_rejected():
    with pytest.raises(QuotaUnavailable):
        decode_quota(record()[:-1], 17)
