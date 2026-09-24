"""Irreversible child-process guard for XFS project identity/inheritance.

Install after dropping privileges, before any worker code. This stacks with (never
replaces) existing container seccomp policy. It is not a general sandbox or a mount
coverage attestation. Linux libseccomp is required; unsupported setup fails closed.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys

from .storage_quota import QuotaUnavailable

BLOCKED_IOCTL_REQUESTS = (0x401C5820, 0x40086602, 0x40046602)
# Linux v6.17 scripts/syscall.tbl and x86 syscall_64.tbl: native arm64/x86_64.
# Older libseccomp may not know the name; never silently omit this new setter.
FILE_SETATTR_SYSCALL = 469


class _Argument(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def install_storage_quota_guard() -> None:
    """Deny project-mutating ioctls in this process, all threads and future children.

    Must run inside the unprivileged worker launcher, NOT the control/helper process.
    The native architecture is supported; foreign syscall ABIs remain denied by
    libseccomp's bad-architecture action. Request comparisons mask to Linux's uint32.
    """
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise QuotaUnavailable("Worker storage guard requires supported 64-bit Linux")
    if os.geteuid() == 0:
        raise QuotaUnavailable("Drop worker privileges before installing the storage guard")
    try:
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise QuotaUnavailable("Worker storage guard requires libseccomp") from exc
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                              ctypes.c_uint, ctypes.POINTER(_Argument)]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW in this additional filter only.
    if not context:
        raise QuotaUnavailable("Could not allocate worker storage guard")
    try:
        syscall = library.seccomp_syscall_resolve_name(b"ioctl")
        if syscall < 0:
            raise QuotaUnavailable("Native ioctl syscall is unavailable")
        # NNP=3, TSYNC=4: cannot gain privileges or leave sibling threads unfiltered.
        for attribute in (3, 4):
            if library.seccomp_attr_set(context, attribute, 1) != 0:
                raise QuotaUnavailable("Worker storage guard attributes are unsupported")
        for request in BLOCKED_IOCTL_REQUESTS:
            comparison = _Argument(1, 7, 0xFFFFFFFF, request)  # SCMP_CMP_MASKED_EQ
            if library.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM,
                                              syscall, 1, ctypes.byref(comparison)) != 0:
                raise QuotaUnavailable("Could not construct worker storage guard")
        setter = library.seccomp_syscall_resolve_name(b"file_setattr")
        if setter not in {-1, FILE_SETATTR_SYSCALL}:
            raise QuotaUnavailable("Unexpected native file_setattr syscall ABI")
        if library.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM,
                                          FILE_SETATTR_SYSCALL, 0, None) != 0:
            raise QuotaUnavailable("Could not guard native file_setattr")
        if library.seccomp_load(context) != 0:
            raise QuotaUnavailable("Could not install worker storage guard")
    finally:
        library.seccomp_release(context)
    # Verify real syscall denial, including high-bit aliases; metadata is not proof.
    libc = ctypes.CDLL(None, use_errno=True)
    # Call the raw syscall: musl's ioctl wrapper truncates request to uint32,
    # which would make the high-bit alias check unable to exercise our mask.
    libc.syscall.restype = ctypes.c_long
    for request in BLOCKED_IOCTL_REQUESTS:
        for command in (request, request | (1 << 32)):
            ctypes.set_errno(0)
            result = libc.syscall(ctypes.c_long(syscall), ctypes.c_int(-1),
                                  ctypes.c_ulong(command), ctypes.c_void_p())
            if result != -1 or ctypes.get_errno() != errno.EPERM:
                raise QuotaUnavailable("Worker storage guard syscall verification failed")
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(FILE_SETATTR_SYSCALL), ctypes.c_int(-1),
                          ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(0))
    if result != -1 or ctypes.get_errno() != errno.EPERM:
        raise QuotaUnavailable("Worker file_setattr guard syscall verification failed")


if __name__ == "__main__":
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("Usage: python -m workers_projects_runtime.storage_quota_guard -- COMMAND [ARG...]")
    install_storage_quota_guard()
    os.execvp(command[0], command)
