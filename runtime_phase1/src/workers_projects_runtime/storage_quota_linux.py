"""Small Linux XFS UAPI adapter for the trusted storage helper.

ABI: Linux include/uapi/linux/{dqblk_xfs.h,fs.h}, supported on 64-bit
x86_64/aarch64. No mount, format, shell, sudo, or device discovery side effects.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import struct
import subprocess
import sys

from .storage_quota import KernelQuota, MountIdentity, QuotaUnavailable

_QUOTA = struct.Struct("=bbHI6QiiHH4b3QiHh8s")
_FSX = struct.Struct("=5I8s")
_FSGETXATTR = 0x801C581F
_FSSETXATTR = 0x401C5820
_PROJINHERIT = 0x200


def require_project_flags(flags: int) -> None:
    if flags & 0x30 != 0x30:
        raise QuotaUnavailable("Project quota accounting and enforcement are required")


def decode_quota(data: bytes, project_id: int) -> KernelQuota:
    if len(data) != _QUOTA.size:
        raise QuotaUnavailable("Unexpected kernel quota record size")
    values = _QUOTA.unpack(data)
    if values[0] != 1 or values[1] != 2 or values[3] != project_id:
        raise QuotaUnavailable("Unexpected kernel quota record identity")
    if any(values[18:21]):
        raise QuotaUnavailable("Realtime device quotas are unsupported")
    return KernelQuota(project_id, values[4] * 512, values[8] * 512,
                       values[6], values[7], values[9], values[5] * 512)


class LinuxXfsQuota:
    def __init__(self):
        if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
            raise QuotaUnavailable("Native project quota requires supported 64-bit Linux")
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.quotactl.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        self.libc.quotactl.restype = ctypes.c_int

    def inspect_mount(self, root: Path) -> MountIdentity:
        executable = shutil.which("findmnt", path="/usr/sbin:/usr/bin:/sbin:/bin")
        if executable is None:
            raise QuotaUnavailable("Storage helper requires util-linux findmnt")
        try:
            result = subprocess.run([executable, "--json", "--target", str(root), "--output",
                                     "TARGET,SOURCE,FSTYPE,OPTIONS,UUID"],
                                    check=True, capture_output=True, text=True, timeout=10,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
            rows = json.loads(result.stdout)["filesystems"]
            if len(rows) != 1:
                raise ValueError("ambiguous mount")
            row = rows[0]
            options = set(row["options"].split(","))
            if (row["fstype"] != "xfs" or Path(row["target"]) != root
                    or not options.intersection({"prjquota", "pquota"})
                    or "pqnoenforce" in options or "rw" not in options or not row.get("uuid")):
                raise QuotaUnavailable("A dedicated read-write XFS project-quota mount is required")
            device = Path(row["source"]).resolve(strict=True)
            device_info = device.stat()
            if not stat.S_ISBLK(device_info.st_mode) or device_info.st_rdev != root.stat().st_dev:
                raise QuotaUnavailable("Storage device does not match the mounted filesystem")
            return MountIdentity(root, str(device), row["uuid"], os.statvfs(root).f_frsize,
                                 root.stat().st_dev)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise QuotaUnavailable("Storage mount verification failed") from exc

    def _call(self, mount: MountIdentity, operation: int, project_id: int, data: bytes) -> bytes:
        if type(project_id) is not int or not 0 <= project_id < 2**31:
            raise ValueError("Project ID exceeds supported kernel range")
        buffer = ctypes.create_string_buffer(data, len(data))
        command = ((0x5800 + operation) << 8) | 2
        if self.libc.quotactl(command, os.fsencode(mount.device), project_id, buffer) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return buffer.raw

    def require_enforcement(self, mount: MountIdentity) -> None:
        try:
            data = self._call(mount, 8, 0, b"\x01" + b"\0" * 159)
            if data[0] != 1:
                raise QuotaUnavailable("Unsupported quota status version")
            require_project_flags(struct.unpack_from("=H", data, 2)[0])
        except OSError as exc:
            raise QuotaUnavailable("Native quota status is unavailable or permission was denied") from exc

    def get_quota(self, mount: MountIdentity, project_id: int) -> KernelQuota:
        try:
            data = self._call(mount, 3, project_id, bytes(_QUOTA.size))
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                self.require_enforcement(mount)
                return KernelQuota(project_id, 0, 0)
            raise QuotaUnavailable("Native quota usage is unavailable") from exc
        return decode_quota(data, project_id)

    def set_limit(self, mount: MountIdentity, project_id: int, blocks: int) -> None:
        if project_id <= 0 or type(blocks) is not int or not 0 <= blocks < 2**64:
            raise ValueError("Invalid project quota limit")
        # Set all normal block/inode limits: no hidden soft or file-count cap.
        data = bytearray(_QUOTA.size)
        struct.pack_into("=bbHIQ", data, 0, 1, 2, 0x0F, project_id, blocks)
        self._call(mount, 4, project_id, bytes(data))

    def project_info(self, path: Path) -> tuple[int, bool]:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            values = _FSX.unpack(fcntl.ioctl(fd, _FSGETXATTR, bytes(_FSX.size)))
            if values[0] & 0x101:
                raise QuotaUnavailable("Realtime owner storage is unsupported")
            return values[3], bool(values[0] & _PROJINHERIT)
        finally:
            os.close(fd)

    def tag_new_root(self, path: Path, project_id: int) -> None:
        if type(project_id) is not int or not 0 < project_id < 2**31:
            raise ValueError("Invalid project ID")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            values = list(_FSX.unpack(fcntl.ioctl(fd, _FSGETXATTR, bytes(_FSX.size))))
            if values[3] != 0 or os.listdir(fd):
                raise QuotaUnavailable("Only a new empty unassigned root may be tagged")
            values[0] |= _PROJINHERIT
            values[3] = project_id
            fcntl.ioctl(fd, _FSSETXATTR, _FSX.pack(*values))
        finally:
            os.close(fd)
