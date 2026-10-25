"""POSIX no-follow snapshots and durable local transaction receipts."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .manifest import EvidenceError, Limits, canonical, loads, relative


def disjoint(*paths):
    resolved = [Path(p).absolute().resolve(strict=False) for p in paths]
    for index, first in enumerate(resolved):
        for second in resolved[index + 1:]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise EvidenceError("Source, evidence and destination roots must not overlap")
            if first.exists() and second.exists():
                first_id = _identity(first.stat())[:2]
                second_id = _identity(second.stat())[:2]
                if (first_id in {_identity(p.stat())[:2] for p in (second, *second.parents)}
                        or second_id in {_identity(p.stat())[:2] for p in (first, *first.parents)}):
                    raise EvidenceError("Root directory identities overlap")


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class Root:
    """A trusted existing root. Every descendant is opened relative to its descriptor."""

    def __init__(self, path, limits=Limits()):
        self.path = Path(path).absolute()
        if self.path != self.path.resolve(strict=True) or not self.path.is_dir():
            raise EvidenceError("Root must be an existing real directory without links")
        self.identity = _identity(self.path.stat())[:2]
        self.limits = limits
        self.deadline = time.monotonic() + limits.seconds

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise EvidenceError("Command time bound expired")

    @contextmanager
    def parent(self, path, *, create=False):
        self.check_deadline()
        parts = relative(path, self.limits).split("/")
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _identity(os.fstat(descriptor))[:2] != self.identity:
                raise EvidenceError("Root identity changed")
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        except OSError as exc:
            raise EvidenceError("File path is missing or unsafe") from exc
        finally:
            os.close(descriptor)

    @contextmanager
    def reader(self, path, *, maximum=None):
        with self.parent(path) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                bound = self.limits.file_bytes if maximum is None else maximum
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > bound:
                    raise EvidenceError("Only bounded regular files with one link are admitted")
                yield handle, info

    def snapshot(self, path, *, expected=None, sink=None):
        started, size, hasher = time.monotonic(), 0, hashlib.sha256()
        with self.reader(path) as (handle, before):
            while chunk := handle.read(256 * 1024):
                self.check_deadline()
                size += len(chunk)
                if size > self.limits.file_bytes or time.monotonic() - started > self.limits.seconds:
                    raise EvidenceError("Snapshot exceeds size or time bound")
                hasher.update(chunk)
                if sink is not None:
                    sink.write(chunk)
            if _identity(before) != _identity(os.fstat(handle.fileno())) or size != before.st_size:
                raise EvidenceError("File changed during snapshot")
        result = {"sha256": hasher.hexdigest(), "size_bytes": size}
        if expected is not None and any(result[k] != expected[k] for k in result):
            raise EvidenceError("File bytes do not match their pinned identity")
        return result

    def read(self, path, *, maximum=None):
        bound = self.limits.manifest_bytes if maximum is None else maximum
        with self.reader(path, maximum=bound) as (handle, before):
            data = handle.read(bound + 1)
            self.check_deadline()
            if len(data) > bound or _identity(before) != _identity(os.fstat(handle.fileno())):
                raise EvidenceError("Read exceeds bound or file changed")
            return data

    def json(self, path):
        return loads(self.read(path), self.limits)

    def inventory(self, directory):
        """Bounded exact regular-file membership, without following directory links."""
        result = set()
        started = time.monotonic()
        visited = 0

        def visit(descriptor, prefix, depth):
            nonlocal visited
            self.check_deadline()
            if depth > self.limits.depth or time.monotonic() - started > self.limits.seconds:
                raise EvidenceError("Inventory exceeds depth or time bound")
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    visited += 1
                    if visited > self.limits.files * (self.limits.depth + 1):
                        raise EvidenceError("Inventory exceeds entry bound")
                    name = relative(entry.name, self.limits)
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    path = prefix + name
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                        try:
                            visit(child, path + "/", depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        result.add(path)
                        if len(result) > self.limits.files:
                            raise EvidenceError("Inventory exceeds file count bound")
                    else:
                        raise EvidenceError("Inventory contains a link or special file")

        with self.parent(directory + "/.inventory") as (descriptor, _name):
            visit(descriptor, "", 0)
        return result

    def exists(self, path):
        try:
            with self.reader(path):
                return True
        except EvidenceError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return False
            raise
        except FileNotFoundError:
            return False

    def _write(self, path, producer, *, replace=False):
        with self.parent(path, create=True) as (parent, name):
            temporary = ".pending-" + uuid.uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    result = producer(handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                if replace:
                    if self.exists(path):
                        self.read(path)
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
                return result
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def put(self, path, data, *, replace=False):
        if len(data) > self.limits.manifest_bytes:
            raise EvidenceError("Receipt exceeds manifest limit")
        if not replace and self.exists(path):
            if self.read(path) != data:
                raise EvidenceError("Immutable receipt conflicts")
            return
        self._write(path, lambda handle: handle.write(data), replace=replace)
        if self.read(path) != data:
            raise EvidenceError("Receipt readback differs")

    def put_json(self, path, value, *, replace=False):
        self.put(path, canonical(value), replace=replace)

    def copy(self, source, source_path, destination_path, expected):
        if self.exists(destination_path):
            self.snapshot(destination_path, expected=expected)
            source.snapshot(source_path, expected=expected)
            return
        self._write(destination_path, lambda handle: source.snapshot(source_path, expected=expected, sink=handle))
        self.snapshot(destination_path, expected=expected)

    @contextmanager
    def lock(self, path):
        with self.parent(path, create=True) as (parent, name):
            try:
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise EvidenceError("Unsafe transaction lock")
                deadline = min(self.deadline, time.monotonic() + self.limits.seconds)
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise EvidenceError("Transaction lock timed out")
                        time.sleep(0.01)
                yield
            finally:
                os.close(descriptor)
