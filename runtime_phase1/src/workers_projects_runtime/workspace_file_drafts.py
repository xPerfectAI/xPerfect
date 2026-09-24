"""Owner-only immutable draft bytes with mutable, revision-checked display paths.

Downloads and ZIPs retain only bounded byte buffers. ZIP metadata grows with the
selection count; preflight reads each selected blob once without retaining a copy.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import quote, urlencode
import zipfile

from .workspace_file_exports import CHUNK_BYTES, _ChunkSink, _identity
from .workspace_files import FileAdmissionError, _directory, _relative


def draft_revision(row: dict) -> str:
    value = [row["upload_id"], row["state"], row["sha256"], row["size_bytes"],
             row["relative_path"] or row["name"], row.get("metadata_revision", 0)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _changed():
    return FileAdmissionError("Stored file changed; refresh the draft and retry", 409)


def ready_row(files, conn, tenant, owner, upload_id, revision=None):
    row = files._upload(conn, tenant, owner, upload_id)
    if row["state"] != "ready":
        raise FileAdmissionError("Stored file is not ready; finish uploading or select it again", 409)
    if revision is not None and draft_revision(row) != revision:
        raise _changed()
    _relative(row["relative_path"] or row["name"])
    return row


def get_draft(files, tenant, owner, upload_id, *, revision=None):
    files.expire(tenant, owner)
    with files.store._connect() as conn:
        row = ready_row(files, conn, tenant, owner, upload_id, revision)
    result = files._public(row)
    result["download_url"] = f"/v1/file-uploads/{quote(upload_id, safe='')}/content?" + urlencode({"revision": result["revision"]})
    return result


def move_draft(files, tenant, owner, upload_id, *, relative_path, revision):
    path = _relative(relative_path).as_posix()
    if not revision:
        raise FileAdmissionError("A stored file revision is required", 422)
    files.expire(tenant, owner)
    with files.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = ready_row(files, conn, tenant, owner, upload_id, revision)
        if path != (row["relative_path"] or row["name"]):
            for other in conn.execute(
                "SELECT relative_path,name FROM workspace_file_uploads WHERE tenant_id=? AND owner_id=? AND draft_id=? AND upload_id!=? AND state NOT IN ('cancelled','expired')",
                (tenant, owner, row["draft_id"], upload_id),
            ):
                target = other["relative_path"] or other["name"]
                if path == target or path.startswith(target + "/") or target.startswith(path + "/"):
                    raise FileAdmissionError("Another stored file already uses this path", 409)
            conn.execute(
                "UPDATE workspace_file_uploads SET relative_path=?,name=?,metadata_revision=metadata_revision+1,updated_at=? WHERE upload_id=?",
                (path, Path(path).name, time.time(), upload_id),
            )
            row = files._upload(conn, tenant, owner, upload_id)
    return files._public(row)


class DraftDownload:
    """A checked open descriptor; release on abort, completion or unconsumed response."""

    def __init__(self, files, tenant, owner, upload_id, *, revision=None, expire=True, verify_digest=True):
        self.files, self.tenant, self.owner = files, tenant, owner
        self.descriptor = None
        if expire:
            files.expire(tenant, owner)
        with files.store._connect() as conn:
            self.row = ready_row(files, conn, tenant, owner, upload_id, revision)
        self.revision = draft_revision(self.row)
        self.size = self.row["size_bytes"]
        self.name = self.row["name"]
        self.path = self.row["relative_path"] or self.name
        self.root = files._owner_root(tenant, owner)
        self.blob_name = upload_id + ".blob"
        try:
            with _directory(self.root) as parent:
                self.root_identity = _identity(os.fstat(parent))[:2]
                metadata = os.stat(self.blob_name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise FileAdmissionError("Stored file is not an ordinary private file", 403)
                self.identity = _identity(metadata)
                self.descriptor = os.open(self.blob_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            if metadata.st_size != self.size:
                raise _changed()
            digest = hashlib.sha256()
            remaining = self.size
            while remaining and verify_digest:
                self.check_identity()
                chunk = os.read(self.descriptor, min(CHUNK_BYTES, remaining))
                self.check_identity()
                if not chunk:
                    raise _changed()
                digest.update(chunk)
                remaining -= len(chunk)
            self.check()
            if verify_digest and digest.hexdigest() != self.row["sha256"]:
                raise FileAdmissionError("Stored file bytes do not match the accepted version", 409)
            os.lseek(self.descriptor, 0, os.SEEK_SET)
        except BaseException as exc:
            self.close()
            if isinstance(exc, OSError):
                raise _changed() from exc
            raise

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def check_identity(self):
        try:
            if self.descriptor is None or _identity(os.fstat(self.descriptor)) != self.identity:
                raise _changed()
        except OSError as exc:
            raise _changed() from exc

    def check(self):
        self.check_identity()
        with self.files.store._connect() as conn:
            ready_row(self.files, conn, self.tenant, self.owner, self.row["upload_id"], self.revision)
        try:
            with _directory(self.root) as parent:
                if _identity(os.fstat(parent))[:2] != self.root_identity or _identity(os.stat(self.blob_name, dir_fd=parent, follow_symlinks=False)) != self.identity:
                    raise _changed()
        except OSError as exc:
            raise _changed() from exc

    def stream(self, start=0, end=None):
        remaining = self.size - start if end is None else end - start + 1
        full = start == 0 and remaining == self.size
        digest = hashlib.sha256()
        try:
            self.check()
            os.lseek(self.descriptor, start, os.SEEK_SET)
            while remaining:
                self.check_identity()
                chunk = os.read(self.descriptor, min(CHUNK_BYTES, remaining))
                self.check_identity()
                if not chunk:
                    raise _changed()
                remaining -= len(chunk)
                digest.update(chunk)
                yield chunk
            self.check()
            if full and digest.hexdigest() != self.row["sha256"]:
                raise _changed()
        finally:
            self.close()


class DraftExport:
    def __init__(self, files, tenant, owner, upload_ids, revisions=None):
        if not upload_ids or len(set(upload_ids)) != len(upload_ids):
            raise FileAdmissionError("Select each stored file once", 422)
        if revisions is not None and len(revisions) != len(upload_ids):
            raise FileAdmissionError("Each selected stored file needs its matching revision", 422)
        self.files, self.tenant, self.owner = files, tenant, owner
        files.expire(tenant, owner)
        self.entries = []
        paths = set()
        for index, upload_id in enumerate(upload_ids):
            prepared = DraftDownload(files, tenant, owner, upload_id,
                revision=revisions[index] if revisions is not None else None, expire=False)
            try:
                path = prepared.path
                if path in paths:
                    raise FileAdmissionError("Selected stored files have conflicting paths; rename or move one first", 409)
                paths.add(path)
                self.entries.append((upload_id, prepared.revision, path, prepared.identity))
            finally:
                prepared.close()
        if any(parent.as_posix() in paths for path in paths for parent in Path(path).parents):
            raise FileAdmissionError("Selected stored files have conflicting paths; rename or move one first", 409)
        self.validate()

    def validate(self):
        with self.files.store._connect() as conn:
            with _directory(self.files._owner_root(self.tenant, self.owner)) as parent:
                for upload_id, revision, _, identity in self.entries:
                    ready_row(self.files, conn, self.tenant, self.owner, upload_id, revision)
                    try:
                        if _identity(os.stat(upload_id + ".blob", dir_fd=parent, follow_symlinks=False)) != identity:
                            raise _changed()
                    except OSError as exc:
                        raise _changed() from exc

    def descriptor(self):
        return {
            "method": "POST",
            "path": "/v1/file-uploads/export",
            "body": {
                "upload_ids": [item[0] for item in self.entries],
                "revisions": [item[1] for item in self.entries],
            },
            "content_type": "application/json",
            "media_type": "application/zip",
            "filename": "stored-files.zip",
        }

    def stream(self):
        self.validate()
        sink = _ChunkSink()
        archive = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
        try:
            for upload_id, revision, path, identity in self.entries:
                prepared = DraftDownload(self.files, self.tenant, self.owner, upload_id, revision=revision, expire=False, verify_digest=False)
                try:
                    if prepared.identity != identity:
                        raise _changed()
                    info = zipfile.ZipInfo(path)
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    with archive.open(info, "w", force_zip64=True) as output:
                        yield from sink.drain()
                        for chunk in prepared.stream():
                            output.write(chunk)
                            yield from sink.drain()
                    yield from sink.drain()
                finally:
                    prepared.close()
            self.validate()
            archive.close()
            yield from sink.drain()
        finally:
            if archive.fp is not None:
                archive.fp = None
