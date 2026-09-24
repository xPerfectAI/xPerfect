import pytest
import ctypes
import errno
from types import SimpleNamespace

from workers_projects_runtime.storage_quota import QuotaUnavailable
from workers_projects_runtime.storage_quota_guard import BLOCKED_IOCTL_REQUESTS, FILE_SETATTR_SYSCALL, install_storage_quota_guard


def test_guard_covers_project_id_and_native_compat_inheritance_mutation():
    assert set(BLOCKED_IOCTL_REQUESTS) == {0x401C5820, 0x40086602, 0x40046602}
    assert FILE_SETATTR_SYSCALL == 469


def test_guard_fails_closed_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr("workers_projects_runtime.storage_quota_guard.sys.platform", "unsupported")
    with pytest.raises(QuotaUnavailable):
        install_storage_quota_guard()


def test_guard_verifies_full_width_requests_via_raw_syscall(monkeypatch):
    import workers_projects_runtime.storage_quota_guard as guard

    calls = []

    def raw_syscall(*args):
        calls.append(tuple(arg.value for arg in args))
        ctypes.set_errno(errno.EPERM)
        return -1

    # Function wrappers accept ctypes declaration attributes like real CDLL exports.
    library = SimpleNamespace(
        seccomp_init=lambda *_: 1,
        seccomp_release=lambda *_: None,
        seccomp_syscall_resolve_name=lambda name: 29 if name == b"ioctl" else -1,
        seccomp_attr_set=lambda *_: 0,
        seccomp_rule_add_array=lambda *_: 0,
        seccomp_load=lambda *_: 0,
    )
    libc = SimpleNamespace(syscall=raw_syscall)  # Deliberately no libc.ioctl wrapper.
    monkeypatch.setattr(guard.sys, "platform", "linux")
    monkeypatch.setattr(guard.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(guard.os, "geteuid", lambda: 20001)
    monkeypatch.setattr(guard.ctypes, "CDLL", lambda name, **_: libc if name is None else library)
    install_storage_quota_guard()
    assert [call[2] for call in calls[:-1]] == [
        command for request in BLOCKED_IOCTL_REQUESTS
        for command in (request, request | (1 << 32))
    ]
    assert all(call[0] == 29 for call in calls[:-1])
    assert calls[-1][0] == FILE_SETATTR_SYSCALL
