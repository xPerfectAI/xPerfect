"""Descriptor-bound permission maintenance for managed Docker workspaces.

The control plane executes this source with the image's absolute system Python
in isolated mode. No worker module, PATH entry, shell profile or pathname link
is authority to choose a maintenance target.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
import ctypes
import errno
import json
import os
from pathlib import PurePosixPath
import stat
import sys


@contextmanager
def directory(path: str, *, create=False, missing_ok=False):
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path:
        raise ValueError("Runtime permission path must be canonical and absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parsed.parts[1:], start=1):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    if missing_ok:
                        yield None
                        return
                    raise
                created = None
                mode = 0o700 if index == len(parsed.parts) - 1 else 0o755
                try:
                    os.mkdir(part, mode, dir_fd=fd)
                    created = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                if created is not None:
                    actual = os.fstat(child)
                    if (actual.st_dev, actual.st_ino) != (created.st_dev, created.st_ino):
                        os.close(child)
                        raise PermissionError("Runtime directory changed during creation")
                    os.fchmod(child, mode)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def opened_entries(fd, path, excluded, *, hardlink_uid=None):
    """Walk pinned inodes iteratively, retaining at most32 ancestor descriptors.

    Missing or replaced children are ordinary workspace churn. They are skipped,
    never followed. Only worker-owned hardlinks on the root filesystem may be
    repaired; a linked inode owned by another principal is left unchanged.
    """
    root = os.fstat(fd)
    pending = [((), (root.st_dev, root.st_ino))]
    directories = OrderedDict()
    try:
        while pending:
            parts, identity = pending.pop()
            anchor = next((parts[:i] for i in range(len(parts), 0, -1)
                           if parts[:i] in directories), ())
            current = os.dup(directories[anchor] if anchor else fd)
            if anchor:
                directories.move_to_end(anchor)
            try:
                try:
                    for index in range(len(anchor), len(parts)):
                        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                        if index < len(parts) - 1:
                            flags |= os.O_DIRECTORY
                        child = os.open(parts[index], flags, dir_fd=current)
                        os.close(current)
                        current = child
                except OSError as error:
                    if parts and error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                        continue
                    raise
                info = os.fstat(current)
                if (info.st_dev, info.st_ino) != identity:
                    if parts:
                        continue
                    raise PermissionError("Runtime maintenance root changed while opening")
                if info.st_dev != root.st_dev:
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1 and info.st_uid != hardlink_uid:
                    continue
                yield current, info
                if not stat.S_ISDIR(info.st_mode):
                    continue
                if parts:
                    directories[parts] = os.dup(current)
                    if len(directories) > 32:
                        _, oldest = directories.popitem(last=False)
                        os.close(oldest)
                with os.scandir(current) as entries:
                    names = [entry.name for entry in entries]
                for name in names:
                    child_parts = (*parts, name)
                    if str(PurePosixPath(path).joinpath(*child_parts)) in excluded:
                        continue
                    try:
                        before = os.stat(name, dir_fd=current, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode):
                        pending.append((child_parts, (before.st_dev, before.st_ino)))
            finally:
                os.close(current)
    finally:
        for descriptor in directories.values():
            os.close(descriptor)


class Acl:
    def __init__(self):
        self.library = ctypes.CDLL("libacl.so.1", use_errno=True)
        self.library.acl_from_text.argtypes = [ctypes.c_char_p]
        self.library.acl_from_text.restype = ctypes.c_void_p
        self.library.acl_free.argtypes = [ctypes.c_void_p]
        self.library.acl_set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p]
        self.library.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]

    def set(self, fd, uids, *, executable, default=False, private=True):
        access = "rwx" if executable or default else "rw-"
        mode = os.fstat(fd).st_mode
        def permissions(bits):
            return ''.join(letter if bits & flag else '-' for flag, letter in ((4, 'r'), (2, 'w'), (1, 'x')))
        group = "---" if private else permissions((mode >> 3) & 7)
        other = "---" if private else permissions(mode & 7)
        text = ','.join([f"u::{access}", *[f"u:{uid}:{access}" for uid in sorted(set(uids))],
                         f"g::{group}", f"m::{access}", f"o::{other}"])
        acl = self.library.acl_from_text(text.encode())
        if not acl:
            raise OSError(ctypes.get_errno(), "Runtime ACL could not be constructed")
        try:
            # The sole followed link is kernel-owned /proc/self/fd/<already-open
            # descriptor>, pinned to the directory even if a worker renames it.
            result = (self.library.acl_set_file(f"/proc/self/fd/{fd}".encode(), 0x4000, acl)
                      if default else self.library.acl_set_fd(fd, acl))
            if result != 0:
                raise OSError(ctypes.get_errno(), "Runtime ACL could not be applied")
        finally:
            self.library.acl_free(acl)


def writable(arguments, uid):
    excluded = set(arguments.get("excluded", []))
    if any(path == target or path.startswith(target + "/") for path in arguments["paths"] for target in excluded):
        raise PermissionError("A declared read-only input cannot be made writable")
    strict = arguments["require_acl"]
    try:
        acl = Acl()
    except OSError:
        if strict:
            raise
        acl = None
    for path in arguments["paths"]:
        with directory(path, create=True) as root:
            for fd, info in opened_entries(root, path, excluded, hardlink_uid=uid):
                executable = stat.S_ISDIR(info.st_mode) or bool(info.st_mode & 0o111)
                if not arguments["preserve_owner"] and info.st_uid != uid:
                    os.fchown(fd, uid, -1)
                if acl is not None:
                    try:
                        acl.set(fd, (uid, arguments["host_uid"], 0), executable=executable, private=not arguments["local_world_fallback"])
                        if stat.S_ISDIR(info.st_mode):
                            acl.set(fd, (uid, arguments["host_uid"], 0), executable=True, default=True, private=not arguments["local_world_fallback"])
                        continue
                    except OSError as error:
                        if strict or error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
                            raise
                mode = stat.S_IMODE(os.fstat(fd).st_mode) & ~0o7000
                if arguments["local_world_fallback"]:
                    mode |= 0o666 | (0o111 if executable else 0)
                else:
                    if os.fstat(fd).st_uid != uid:
                        os.fchown(fd, uid, -1)
                    mode = (mode | 0o600 | (0o100 if executable else 0)) & ~0o077
                os.fchmod(fd, mode)


def secrets(arguments, uid):
    with directory(arguments["path"], missing_ok=True) as parent:
        if parent is None:
            return
        for name in ("secret-runtime.env", "secret-runtime.keys"):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PermissionError("Runtime secret must be a single regular file")
                os.fchown(fd, uid, -1)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)


def temporary(arguments, uid):
    path = str(PurePosixPath(arguments["path"]) / f"claude-{uid}") if arguments.get("provider_subdirectory") else arguments["path"]
    with directory(path, missing_ok=True) as root:
        if root is None:
            return
        for fd, info in opened_entries(root, path, set(), hardlink_uid=uid):
            os.fchown(fd, uid, arguments["gid"])
            os.fchmod(fd, (stat.S_IMODE(info.st_mode) | 0o600 | (0o100 if stat.S_ISDIR(info.st_mode) else 0)) & ~0o7077)


def screen(arguments, uid):
    with directory("/run/screen", create=True) as root:
        os.fchmod(root, 0o1777)
    with directory(arguments["path"], create=True) as root:
        os.fchown(root, uid, -1)
        os.fchmod(root, 0o700)


def main(arguments):
    if any(type(arguments.get(key)) is not int or not 0 <= arguments[key] < 2**32 - 1 for key in ("uid", "gid")):
        raise ValueError("Runtime maintenance requires control-plane numeric uid and gid")
    {"writable": writable, "secrets": secrets, "temporary": temporary, "screen": screen}[arguments["operation"]](arguments, arguments["uid"])


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
