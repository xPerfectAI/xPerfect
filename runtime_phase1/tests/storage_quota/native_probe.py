"""Explicit disposable Linux acceptance probe, not part of default unit tests.

Run as a trusted administrator against a fresh XFS project-quota mount. Native
writers run with distinct UIDs and no capabilities. All fixtures stay under the
supplied mount/control paths. Does not mount, format, delete roots, or alter quotas
outside owners provisioned through the backend.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import struct
import sys
import uuid
import time

from workers_projects_runtime.storage_quota import QuotaUnavailable, XfsProjectStorage
from workers_projects_runtime.storage_quota_guard import install_storage_quota_guard


def worker():
    install_storage_quota_guard()
    print("READY", flush=True)
    request = json.loads(sys.stdin.readline())
    started = time.monotonic()
    status = Path("/proc/self/status").read_text()
    assert "CapEff:\t0000000000000000" in status
    assert "CapBnd:\t0000000000000000" in status
    assert os.geteuid() != 0
    result = {"uid": os.geteuid(), "started": started, "errno": None, "written": 0}
    try:
        action = request["action"]
        if action == "fill":
            fd = os.open(request["name"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                chunk = b"x" * 65536
                while result["written"] < request["maximum"]:
                    result["written"] += os.write(fd, chunk[:min(len(chunk), request["maximum"] - result["written"])])
                os.fsync(fd)
            finally:
                os.close(fd)
        elif action == "read":
            result["sha256"] = hashlib.sha256(Path(request["name"]).read_bytes()).hexdigest()
        elif action == "delete":
            Path(request["name"]).unlink()
        elif action == "copy":
            with open(request["source"], "rb") as source, open(request["name"], "xb", buffering=0) as target:
                while block := source.read(65536):
                    target.write(block)
                os.fsync(target.fileno())
        elif action == "sparse":
            with open(request["name"], "xb") as target:
                target.truncate(request["maximum"])
            result["logical"] = Path(request["name"]).stat().st_size
            result["allocated"] = Path(request["name"]).stat().st_blocks * 512
        elif action == "clear_project":
            # Keep a relative path: the launcher enters a private child before
            # dropping UID; its administrative ancestors are intentionally 0700.
            path = Path(f"guarded-escape-attempt-{uuid.uuid4().hex}")
            path.mkdir()
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fields = list(struct.unpack("=5I8s", fcntl.ioctl(fd, 0x801C581F, bytes(28))))
                fields[0] &= ~0x200
                fields[3] = 0
                fcntl.ioctl(fd, 0x401C5820, struct.pack("=5I8s", *fields))
            finally:
                os.close(fd)
        else:
            raise AssertionError("Unknown fixture action")
    except OSError as exc:
        result["errno"] = exc.errno
    result["finished"] = time.monotonic()
    print(json.dumps(result), flush=True)


def start_worker(root: Path, uid: int, request: dict):
    command = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups",
               "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs",
               sys.executable, str(Path(__file__).resolve()), "worker"]
    process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                               "PYTHONPATH": os.environ["PYTHONPATH"]})
    assert process.stdout.readline().strip() == "READY"
    return process, request


def finish_worker(pair):
    process, request = pair
    output, error = process.communicate(json.dumps(request) + "\n", timeout=180)
    assert process.returncode == 0, error
    return json.loads(output)


def run(root, uid, **request):
    return finish_worker(start_worker(root, uid, request))


def child(root: Path, name: str, uid: int):
    path = root / name
    path.mkdir(mode=0o700)
    os.chown(path, uid, uid)
    return path


def main(mount: Path, control: Path, phase: str):
    backend = XfsProjectStorage(mount, control / "registry.sqlite3")
    limit = 16 * 1024**2
    if phase == "security":
        first = backend.snapshot("tenant-test", "owner-a", limit)
        second = backend.snapshot("tenant-test", "owner-b", limit)
        workspace = first.root / "workspace"
        denied = run(workspace, 20001, action="read", name=str(second.root / "workspace" / "independent.bin"))
        assert denied["errno"] == errno.EACCES, denied
        escape = run(workspace, 20001, action="clear_project")
        assert escape["errno"] == errno.EPERM, escape
        alias = control.parent / "data-alias"
        alias.mkdir(mode=0o700)
        subprocess.run(["mount", "--bind", str(mount), str(alias)], check=True)
        try:
            try:
                XfsProjectStorage(mount, alias / "forbidden-registry.sqlite3")
            except QuotaUnavailable as exc:
                assert "filesystem" in str(exc)
            else:
                raise AssertionError("Aliased owner filesystem accepted for control state")
            assert not (alias / "forbidden-registry.sqlite3").exists()
        finally:
            subprocess.run(["umount", str(alias)], check=True)
            alias.rmdir()
        print(json.dumps({"case": "cross-owner-and-unprivileged-project-change-denied-control-alias-rejected",
                          "cross_owner_errno": denied["errno"], "escape_errno": escape["errno"]}), flush=True)
        return
    if phase == "remount":
        snapshot = backend.snapshot("tenant-test", "owner-a", limit)
        assert snapshot.hard_enforced and snapshot.used_allocated_bytes > 0
        root = snapshot.root / "workspace"
        result = run(root, 20001, action="read", name="retained.txt")
        assert result["sha256"] == hashlib.sha256(b"retained-artifact\n").hexdigest()
        print(json.dumps({"case": "remount-retains-policy-data-and-registry", "project_id": snapshot.project_id,
                          "used": snapshot.used_allocated_bytes}), flush=True)
        return

    first = backend.provision("tenant-test", "owner-a", limit)
    second = backend.provision("tenant-test", "owner-b", limit)
    assert first.project_id != second.project_id
    workspace = child(first.root, "workspace", 20001)
    home = child(first.root, "home", 20003)
    other = child(second.root, "workspace", 20002)
    retained = workspace / "retained.txt"
    retained.write_bytes(b"retained-artifact\n")
    os.chown(retained, 20001, 20001)
    # Start both before releasing their stdin gates; communicate is collected later.
    pairs = [start_worker(workspace, 20001, {"action": "fill", "name": "large.bin", "maximum": limit * 2}),
             start_worker(home, 20003, {"action": "fill", "name": "large.bin", "maximum": limit * 2})]
    for process, request in pairs:
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.close()
        process.stdin = None
    results = []
    for process, _ in pairs:
        output, error = process.communicate(timeout=60)
        assert process.returncode == 0, error
        results.append(json.loads(output))
    # XFS project (directory-tree) quotas deliberately report ENOSPC, unlike
    # user/group EDQUOT: Linux fs/xfs/xfs_trans_dquot.c:xfs_trans_dqresv.
    assert all(result["errno"] == errno.ENOSPC for result in results), results
    used = backend.snapshot("tenant-test", "owner-a", limit).used_allocated_bytes
    assert 0 < used <= limit
    assert max(result["started"] for result in results) < min(result["finished"] for result in results)
    assert os.statvfs(mount).f_bavail * os.statvfs(mount).f_frsize > limit * 2
    print(json.dumps({"case": "same-owner-concurrent-native-quota-enospc", "used": used, "limit": limit,
                      "writers": results}), flush=True)
    read = run(workspace, 20001, action="read", name="retained.txt")
    assert read["sha256"] == hashlib.sha256(b"retained-artifact\n").hexdigest()
    copy_root, copy_uid = (workspace, 20001) if results[0]["written"] else (home, 20003)
    copied = run(copy_root, copy_uid, action="copy", source="large.bin", name="copy.bin")
    assert copied["errno"] == errno.ENOSPC, copied
    other_write = run(other, 20002, action="fill", name="independent.bin", maximum=1024**2)
    assert other_write["errno"] is None and other_write["written"] == 1024**2
    with sqlite3.connect(control / "registry.sqlite3") as conn:
        conn.execute("CREATE TABLE proof_control_live (value TEXT)")
        conn.execute("INSERT INTO proof_control_live VALUES ('control-write-at-owner-exhaustion')")
    print(json.dumps({"case": "exhaustion-read-copy-refusal-other-owner-control-live", "copy_errno": copied["errno"]}), flush=True)
    for root, uid in ((workspace, 20001), (home, 20003)):
        if (root / "large.bin").exists():
            assert run(root, uid, action="delete", name="large.bin")["errno"] is None
    if (copy_root / "copy.bin").exists():
        assert run(copy_root, copy_uid, action="delete", name="copy.bin")["errno"] is None
    assert run(workspace, 20001, action="fill", name="recovered.bin", maximum=32768)["errno"] is None
    sparse = run(workspace, 20001, action="sparse", name="sparse.bin", maximum=limit * 8)
    assert sparse["errno"] is None and sparse["allocated"] == 0
    print(json.dumps({"case": "delete-recovery-and-separate-logical-length", "sparse": sparse}), flush=True)

    # Actual 5e9 policy and native boundary, not just a field-value test.
    decimal = backend.provision("tenant-test", "owner-decimal", 5_000_000_000)
    decimal_workspace = child(decimal.root, "workspace", 20004)
    exact = run(decimal_workspace, 20004, action="fill", name="five-gb.bin", maximum=5_004_194_304)
    actual = backend.snapshot("tenant-test", "owner-decimal", 5_000_000_000)
    assert exact["errno"] == errno.ENOSPC, exact
    assert exact["written"] > 4_900_000_000
    assert actual.limit_bytes == 5_000_000_000
    assert actual.kernel_hard_limit_bytes == 4_999_999_488
    assert 4_900_000_000 < actual.used_allocated_bytes <= actual.kernel_hard_limit_bytes
    print(json.dumps({"case": "exact-decimal-five-gb-native-boundary", "limit": actual.limit_bytes,
                      "kernel_limit": actual.kernel_hard_limit_bytes,
                      "used": actual.used_allocated_bytes, "writer": exact}), flush=True)
    assert run(decimal_workspace, 20004, action="delete", name="five-gb.bin")["errno"] is None
    assert run(decimal_workspace, 20004, action="fill", name="recovered.bin", maximum=32768)["errno"] is None
    print(json.dumps({"case": "five-gb-cleanup-and-recovery"}), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        worker()
    else:
        main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
