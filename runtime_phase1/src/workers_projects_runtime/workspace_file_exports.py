"""Read-only ZIP exports: bounded byte buffers and no retained archive copy.

The standard ZIP writer and preflight retain metadata per entry, not file bytes.
An export is deliberately aborted if a selected file changes while being read.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator
import zipfile

from .workspace_files import FileAdmissionError, WorkspaceFiles, _directory, _relative

CHUNK_BYTES = 256 * 1024


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@dataclass(frozen=True)
class ExportEntry:
    path: Path
    identity: tuple[int, ...]
    is_dir: bool
    digest: str = ""


class _ChunkSink:
    """Non-seekable zip sink drained after every bounded input write."""

    def __init__(self):
        self.chunks: deque[bytes] = deque()
        self.position = 0

    def write(self, data):
        self.position += len(data)
        if data:
            self.chunks.append(bytes(data))
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        pass

    def drain(self):
        while self.chunks:
            yield self.chunks.popleft()


def _changed() -> FileAdmissionError:
    return FileAdmissionError("Export selection changed; refresh Files and retry", 409)


def _inspect(root: Path, path: Path) -> ExportEntry:
    # All descendants must pass policy; never silently omit a private child.
    _relative(path.as_posix())
    with _directory(root, path.parent) as parent:
        try:
            metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                return ExportEntry(path, _identity(metadata), True)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise FileAdmissionError(
                    "Export contains a link or non-ordinary file", 403
                )
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            try:
                if _identity(os.fstat(fd)) != _identity(metadata):
                    raise _changed()
                digest = hashlib.sha256()
                while chunk := os.read(fd, CHUNK_BYTES):
                    digest.update(chunk)
                if _identity(os.fstat(fd)) != _identity(metadata) or _identity(
                    os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                ) != _identity(metadata):
                    raise _changed()
                return ExportEntry(path, _identity(metadata), False, digest.hexdigest())
            finally:
                os.close(fd)
        except OSError as exc:
            raise _changed() from exc


class WorkspaceFileExport:
    """Preflight before response headers, then stream checked source bytes."""

    def __init__(
        self,
        files: WorkspaceFiles,
        worker_id: str,
        tenant_id: str,
        owner_id: str,
        file_ids: list[str],
    ):
        if not file_ids:
            raise FileAdmissionError("Select at least one file or folder", 422)
        worker = files._worker(worker_id, tenant_id, owner_id, read_only=True)
        self.root = files._root(worker)
        with _directory(self.root) as root_fd:
            # Ignore root directory mtime: unrelated workspace files may change.
            self.root_identity = _identity(os.fstat(root_fd))[:2]
        with files.store._connect() as conn:
            selected = {
                _relative(files._entry(conn, worker, file_id)["path"])
                for file_id in file_ids
            }
        roots = sorted(
            (
                path
                for path in selected
                if not any(parent in selected for parent in path.parents)
            ),
            key=lambda path: path.as_posix(),
        )
        self.entries: list[ExportEntry] = []
        for path in roots:
            self._walk(path)
        self.validate()

    def _walk(self, path: Path):
        entry = _inspect(self.root, path)
        self.entries.append(entry)
        if entry.is_dir:
            with _directory(self.root, path) as directory:
                if _identity(os.fstat(directory)) != entry.identity:
                    raise _changed()
                for name in sorted(os.listdir(directory)):
                    self._walk(path / name)
                if _identity(os.fstat(directory)) != entry.identity:
                    raise _changed()

    def validate(self):
        with _directory(self.root) as root_fd:
            if _identity(os.fstat(root_fd))[:2] != self.root_identity:
                raise _changed()
        for entry in self.entries:
            with _directory(self.root, entry.path.parent) as parent:
                try:
                    current = os.stat(
                        entry.path.name, dir_fd=parent, follow_symlinks=False
                    )
                except OSError as exc:
                    raise _changed() from exc
                if _identity(current) != entry.identity:
                    raise _changed()

    def stream(self) -> Iterator[bytes]:
        self.validate()
        sink = _ChunkSink()
        archive = zipfile.ZipFile(
            sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        )
        try:
            for entry in self.entries:
                info = zipfile.ZipInfo(
                    entry.path.as_posix() + ("/" if entry.is_dir else "")
                )
                info.external_attr = (
                    (stat.S_IFDIR | 0o700) if entry.is_dir else (stat.S_IFREG | 0o600)
                ) << 16
                if entry.is_dir:
                    info.external_attr |= 0x10
                    archive.writestr(info, b"")
                    yield from sink.drain()
                    continue
                with _directory(self.root, entry.path.parent) as parent:
                    try:
                        fd = os.open(
                            entry.path.name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent,
                        )
                    except OSError as exc:
                        raise _changed() from exc
                    try:
                        if _identity(os.fstat(fd)) != entry.identity:
                            raise _changed()
                        digest = hashlib.sha256()
                        with archive.open(info, "w", force_zip64=True) as output:
                            yield from sink.drain()
                            while chunk := os.read(fd, CHUNK_BYTES):
                                digest.update(chunk)
                                output.write(chunk)
                                yield from sink.drain()
                        if (
                            _identity(os.fstat(fd)) != entry.identity
                            or digest.hexdigest() != entry.digest
                            or _identity(
                                os.stat(
                                    entry.path.name,
                                    dir_fd=parent,
                                    follow_symlinks=False,
                                )
                            )
                            != entry.identity
                        ):
                            raise _changed()
                    finally:
                        os.close(fd)
                yield from sink.drain()
            self.validate()
            # Central directory metadata scales with entry count, never payload.
            archive.close()
            yield from sink.drain()
        finally:
            # On abort do not append a valid central directory to partial bytes.
            if archive.fp is not None:
                archive.fp = None
