"""Run this read probe in the actual worker execution context, before inference."""

from __future__ import annotations

import errno
import hashlib
import os
import stat

from .manifest import EvidenceError, Limits, text


def read_probe(path, *, case_id, probe_run_id, limits=Limits()):
    text(path, maximum=4096)
    text(case_id)
    text(probe_run_id)
    result = {"case_id": case_id, "probe_run_id": probe_run_id, "operation": "read", "path": path,
              "result": "unavailable", "exit_code": 1, "content_sha256": None, "errno": None}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limits.file_bytes:
                raise EvidenceError("Read probe requires a bounded regular canary")
            hasher, size = hashlib.sha256(), 0
            while chunk := handle.read(64 * 1024):
                size += len(chunk)
                if size > limits.file_bytes:
                    raise EvidenceError("Canary changed or exceeds probe size bound")
                hasher.update(chunk)
            after = os.fstat(handle.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise EvidenceError("Canary changed during read probe")
            result.update(result="read", exit_code=0, content_sha256=hasher.hexdigest())
    except OSError as exc:
        result["errno"] = exc.errno
        if exc.errno in {errno.EACCES, errno.EPERM}:
            result["result"] = "denied"
        elif exc.errno == errno.ENOENT:
            result["result"] = "not_visible"
    return result
