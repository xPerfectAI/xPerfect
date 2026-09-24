"""Offline, journaled G8 restore for the package's separate data/control volumes.

The caller must stop every package writer before invoking this module. The
transaction leaves old roots in private backups until an explicit commit;
rollback is available after a failed start or user-level check.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from typing import Callable
import uuid

_JOURNAL = ".g8-restore-journal.json"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def _trusted_root(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("Restore root must be an existing canonical directory")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("Restore root is not service-owned")
    return path


def _private_source(root: Path) -> None:
    root = _trusted_root(root)
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Restore archive is not private")
        if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError("Restore archive contains an unsafe item")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _known_target(path: Path, data_root: Path, control_root: Path) -> bool:
    if path in {
        data_root / "execution_workspaces", data_root / "managed-files",
        data_root / "provider-accounts", control_root / "runtime.db",
        control_root / "workspaces", control_root / "file-control",
        control_root / "storage" / "runtime-storage" / "workspaces",
        control_root / "workspace-file-locks",
        control_root / "storage" / "runtime-storage" / "files",
    }:
        return True
    try:
        relative = path.relative_to(data_root / "owners")
    except ValueError:
        return False
    return (len(relative.parts) == 2 and bool(_DIGEST.fullmatch(relative.parts[0]))
            and relative.parts[1] in {"execution_workspaces", "managed-files", "provider-accounts"})


def _read_journal(data_root: Path, control_root: Path) -> dict:
    path = control_root / _JOURNAL
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Restore journal identity is unsafe")
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("version") != 1 or
            value.get("phase") not in {"installing", "installed", "committing"} or
            not _TOKEN.fullmatch(str(value.get("id") or "")) or
            not isinstance(value.get("operations"), list)):
        raise ValueError("Restore journal is invalid")
    seen = set()
    for item in value["operations"]:
        if not isinstance(item, dict) or set(item) != {"target", "stage", "backup", "had_target"}:
            raise ValueError("Restore journal operation is invalid")
        target, stage, backup = (Path(item[key]) for key in ("target", "stage", "backup"))
        if (target in seen or not _known_target(target, data_root, control_root)
                or target.parent != target.parent.resolve(strict=True)
                or stage.parent != target.parent or backup.parent != target.parent
                or stage.name != f".g8-stage-{value['id']}-{target.name}"
                or backup.name != f".g8-backup-{value['id']}-{target.name}"
                or type(item["had_target"]) is not bool):
            raise ValueError("Restore journal target binding is invalid")
        seen.add(target)
    return value


def _remove(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Restore transaction path changed to a symlink")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _settle_database(path: Path) -> None:
    """Keep WAL bytes out of a cross-volume rename and its rollback."""
    if not path.exists():
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise ValueError("Restore database has orphaned SQLite sidecars")
        return
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Restore database path is unsafe")
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Restore database is invalid")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and checkpoint[0]:
            raise ValueError("Restore database has an active WAL writer")
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise ValueError("Restore database WAL could not settle")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError("Restore database sidecar remains active")


def _archive_layout(stage: Path, data_root: Path, control_root: Path) -> list[tuple[Path, Path]]:
    database = stage / "runtime.sqlite"
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        quota = bool(connection.execute(
            "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
        ).fetchone()[0])
        owners = {(str(row[0]), str(row[1])) for row in connection.execute(
            "SELECT DISTINCT tenant_id,owner_id FROM workers UNION SELECT DISTINCT tenant_id,owner_id FROM workspace_file_uploads"
        )}
        state_root = (control_root / "storage" / "runtime-storage" / "workspaces"
                      if quota else control_root / "workspaces")
        for workspace, state in connection.execute("SELECT workspace_dir,state_dir FROM workers"):
            if (not Path(workspace).is_relative_to(data_root)
                    or not Path(state).is_relative_to(state_root)):
                raise ValueError("Restore archive is not a packaged Native volume layout")
    unknown = {item.name for item in stage.iterdir()} - {"runtime.sqlite", "data", "control"}
    if unknown:
        raise ValueError(f"Restore archive contains an unknown top-level domain: {sorted(unknown)}")
    staged_data = stage / "data"
    staged_control = stage / "control"
    staged_data.mkdir(mode=0o700, exist_ok=True)
    staged_control.mkdir(mode=0o700, exist_ok=True)
    if quota:
        if any(item.name != "storage" for item in staged_control.iterdir()):
            raise ValueError("Restore archive contains an unknown quota control domain")
        storage = staged_control / "storage"
        storage.mkdir(mode=0o700, exist_ok=True)
        if any(item.name != "runtime-storage" for item in storage.iterdir()):
            raise ValueError("Restore archive contains an unknown quota storage domain")
        runtime_storage = storage / "runtime-storage"
        runtime_storage.mkdir(mode=0o700, exist_ok=True)
        if any(item.name != "workspaces" for item in runtime_storage.iterdir()):
            raise ValueError("Restore archive contains an unknown quota runtime domain")
        staged_workspaces = runtime_storage / "workspaces"
        target_workspaces = control_root / "storage" / "runtime-storage" / "workspaces"
    else:
        if any(item.name != "workspaces" for item in staged_control.iterdir()):
            raise ValueError("Restore archive contains an unknown control domain")
        staged_workspaces = staged_control / "workspaces"
        target_workspaces = control_root / "workspaces"
    operations: list[tuple[Path, Path]] = []
    if quota:
        if any(item.name != "owners" for item in staged_data.iterdir()):
            raise ValueError("Restore archive contains an unknown quota data domain")
        owners_root = staged_data / "owners"
        owners_root.mkdir(mode=0o700, exist_ok=True)
        expected = {hashlib.sha256(f"{tenant}\0{owner}".encode()).hexdigest() for tenant, owner in owners}
        if {item.name for item in owners_root.iterdir()} != expected:
            raise ValueError("Restore archive quota owners disagree with retained identities")
        for digest in sorted(expected):
            source_owner = owners_root / digest
            target_owner = _trusted_root(data_root / "owners" / digest)
            if any(item.name not in {"execution_workspaces", "managed-files"} for item in source_owner.iterdir()):
                raise ValueError("Restore archive contains an unknown owner data domain")
            for name in ("execution_workspaces", "managed-files", "provider-accounts"):
                source = source_owner / name
                source.mkdir(mode=0o700, exist_ok=True)
                operations.append((source, target_owner / name))
        file_control = control_root / "storage" / "runtime-storage" / "files"
    else:
        if any(item.name not in {"execution_workspaces", "managed-files"} for item in staged_data.iterdir()):
            raise ValueError("Restore archive contains an unknown data domain")
        for name in ("execution_workspaces", "managed-files", "provider-accounts"):
            source = staged_data / name
            source.mkdir(mode=0o700, exist_ok=True)
            operations.append((source, data_root / name))
        file_control = control_root / "file-control"
    for source, target in (
        (staged_workspaces, target_workspaces),
        (stage / "empty-file-control", file_control),
        (stage / "empty-workspace-file-locks", control_root / "workspace-file-locks"),
    ):
        source.mkdir(mode=0o700, exist_ok=True)
        _trusted_root(target.parent)
        operations.append((source, target))
    operations.append((database, control_root / "runtime.db"))
    return operations


def rollback_offline(*, data_root: Path, control_root: Path, assert_stopped: Callable[[], None]) -> None:
    data_root, control_root = _trusted_root(data_root), _trusted_root(control_root)
    assert_stopped()
    journal = _read_journal(data_root, control_root)
    if journal["phase"] == "committing":
        raise ValueError("Restore cleanup already started; complete the commit")
    _settle_database(control_root / "runtime.db")
    for item in reversed(journal["operations"]):
        target, stage, backup = (Path(item[key]) for key in ("target", "stage", "backup"))
        if backup.exists():
            _remove(target)
            os.replace(backup, target)
            _fsync_directory(target.parent)
        elif not item["had_target"] and not stage.exists():
            _remove(target)
        _remove(stage)
    (control_root / _JOURNAL).unlink()
    _fsync_directory(control_root)


def commit_offline(*, data_root: Path, control_root: Path, assert_stopped: Callable[[], None]) -> None:
    data_root, control_root = _trusted_root(data_root), _trusted_root(control_root)
    assert_stopped()
    path = control_root / _JOURNAL
    journal = _read_journal(data_root, control_root)
    if journal["phase"] == "installing":
        raise ValueError("Restore installation is incomplete; roll it back")
    _settle_database(control_root / "runtime.db")
    journal["phase"] = "committing"
    _write_journal(path, journal)
    for item in journal["operations"]:
        _remove(Path(item["backup"]))
        _remove(Path(item["stage"]))
    path.unlink()
    _fsync_directory(control_root)


def restore_offline(*, archive: Path, data_root: Path, control_root: Path,
                    staging_parent: Path, assert_stopped: Callable[[], None],
                    after_swap: Callable[[int], None] | None = None) -> dict:
    """Install a portable G8 archive while all package writers remain stopped."""
    from workers_projects_runtime.native_continuity import prepare_restored_state
    from workers_projects_runtime.store import Store
    from workers_projects_runtime.workspace_file_continuity import bind_restored_deletes

    data_root, control_root = _trusted_root(data_root), _trusted_root(control_root)
    archive, staging_parent = _trusted_root(archive), _trusted_root(staging_parent)
    if (staging_parent.is_relative_to(data_root) or staging_parent.is_relative_to(control_root)
            or (control_root / _JOURNAL).exists()):
        raise ValueError("Restore staging or prior transaction is unsafe")
    assert_stopped()
    _settle_database(control_root / "runtime.db")
    _private_source(archive)
    transaction_id = uuid.uuid4().hex
    working = Path(tempfile.mkdtemp(prefix="g8-prepare-", dir=staging_parent))
    staged: list[Path] = []
    journal_written = False
    try:
        disposable = working / "archive"
        shutil.copytree(archive, disposable)
        _private_source(disposable)
        prepare_restored_state(disposable, control_root, data_root=data_root, control_root=control_root)
        operations = _archive_layout(disposable, data_root, control_root)
        assert_stopped()
        journal_ops = []
        for source, target in operations:
            if not _known_target(target, data_root, control_root):
                raise ValueError("Restore target is outside its owned volume domain")
            if target.is_symlink() or target.parent != target.parent.resolve(strict=True):
                raise ValueError("Restore target path is unsafe")
            temporary = target.with_name(f".g8-stage-{transaction_id}-{target.name}")
            backup = target.with_name(f".g8-backup-{transaction_id}-{target.name}")
            if temporary.exists() or backup.exists():
                raise ValueError("Restore target has an unrelated staging path")
            staged.append(temporary)
            if source.is_dir():
                shutil.copytree(source, temporary)
            else:
                shutil.copy2(source, temporary)
            journal_ops.append({"target": str(target), "stage": str(temporary),
                                "backup": str(backup), "had_target": target.exists()})
        journal = {"version": 1, "phase": "installing", "id": transaction_id,
                   "operations": journal_ops}
        _write_journal(control_root / _JOURNAL, journal)
        journal_written = True
        for index, item in enumerate(journal_ops):
            target, temporary, backup = (Path(item[key]) for key in ("target", "stage", "backup"))
            if item["had_target"]:
                os.replace(target, backup)
            os.replace(temporary, target)
            _fsync_directory(target.parent)
            if after_swap is not None:
                after_swap(index)
        installed = Store(str(control_root / "runtime.db"))
        try:
            bound = bind_restored_deletes(installed)
        finally:
            installed.close()
        _settle_database(control_root / "runtime.db")
        journal["phase"] = "installed"
        _write_journal(control_root / _JOURNAL, journal)
        return {"transaction_id": transaction_id, "trash_bound": bound,
                "operations": len(journal_ops), "awaiting_commit": True}
    except BaseException:
        if journal_written:
            rollback_offline(data_root=data_root, control_root=control_root,
                             assert_stopped=assert_stopped)
        else:
            for path in staged:
                _remove(path)
        raise
    finally:
        shutil.rmtree(working)


def _docker(endpoint: str, *arguments: str) -> str:
    result = subprocess.run(
        ["docker", "--host", endpoint, *arguments], capture_output=True, text=True,
        timeout=1800,
    )
    if result.returncode:
        # Docker stderr can contain operator paths or private archive names.
        raise RuntimeError("Offline package Docker operation failed: " + arguments[0])
    return result.stdout.strip()


def _package_receipt(path: Path, endpoint: str) -> dict:
    if not endpoint.startswith("unix:///"):
        raise ValueError("Choose an explicit local Unix Docker endpoint")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("Package receipt must be private and service-owned")
    if os.path.lexists(path.with_name(path.name + ".upgrade.json")):
        raise ValueError("An upgrade of this package is open; run upgrade-commit or upgrade-rollback first")
    receipt = json.loads(path.read_text())
    if (receipt.get("profile") not in {"local-linux", "hosted-xfs"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get("service_image") or ""))
            or set(receipt.get("containers") or {}) != {"runtime", "ui", "mcp"}
            or not {"data", "control"} <= set(receipt.get("volumes") or {})):
        raise ValueError("Package receipt is incomplete")
    for value in (*receipt["containers"].values(), *receipt["volumes"].values()):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(value)):
            raise ValueError("Package resource identity is invalid")
    if _docker(endpoint, "image", "inspect", "--format", "{{.Id}}", receipt["service_image"]) != receipt["service_image"]:
        raise ValueError("Exact package image is unavailable")
    return receipt


def _assert_package_stopped(endpoint: str, receipt: dict, *, helper: str = "") -> None:
    package_name = ""
    for volume in (receipt["volumes"]["data"], receipt["volumes"]["control"]):
        value = json.loads(_docker(endpoint, "volume", "inspect", "--format", "{{json .}}", volume))
        labeled = str((value.get("Labels") or {}).get("xperfect.package") or "")
        if value.get("Name") != volume or not labeled or (package_name and labeled != package_name):
            raise ValueError("Package volume identity is unavailable")
        package_name = labeled
    for role, container in receipt["containers"].items():
        value = json.loads(_docker(endpoint, "inspect", "--format", "{{json .}}", container))
        labels = value.get("Config", {}).get("Labels") or {}
        if (value.get("Id") != container or value.get("State", {}).get("Running") is not False
                or labels.get("xperfect.role") != role
                or labels.get("xperfect.package") != package_name):
            raise ValueError("Package service is not stopped at its recorded identity")
    for volume in (receipt["volumes"]["data"], receipt["volumes"]["control"]):
        running = set(_docker(endpoint, "ps", "--filter", "volume=" + volume,
                              "--format", "{{.ID}}").splitlines())
        if helper:
            running.discard(helper)
            running.discard(helper[:12])
        if running:
            raise ValueError("A package volume still has a running container")


def _hosted_quota_device(endpoint: str, receipt: dict) -> str:
    """Reuse the stopped runtime's exact XFS device for one restore helper."""
    data_volume = receipt["volumes"]["data"]
    volume = json.loads(_docker(endpoint, "volume", "inspect", "--format", "{{json .}}", data_volume))
    options = volume.get("Options") or {}
    mount_root = options.get("device")
    if (volume.get("Name") != data_volume or volume.get("Driver") != "local"
            or options.get("type") != "none" or options.get("o") != "bind"
            or not isinstance(mount_root, str) or mount_root == "/"
            or not mount_root.startswith("/")
            or os.path.normpath(mount_root) != mount_root):
        raise ValueError("Hosted restore data volume is not the recorded XFS bind root")

    runtime = json.loads(_docker(endpoint, "inspect", "--format", "{{json .}}",
                                 receipt["containers"]["runtime"]))
    host = runtime.get("HostConfig") or {}
    mounts = host.get("Mounts") or []
    devices = host.get("Devices") or []
    if (runtime.get("Id") != receipt["containers"]["runtime"]
            or not {"SYS_ADMIN", "CAP_SYS_ADMIN"}.intersection(host.get("CapAdd") or [])
            or not any(item.get("Type") == "volume" and item.get("Source") == data_volume
                       and item.get("Target") == "/data" and not item.get("ReadOnly")
                       for item in mounts if isinstance(item, dict))
            or len(devices) != 1 or not isinstance(devices[0], dict)):
        raise ValueError("Hosted restore runtime XFS authority is unavailable")
    device = devices[0].get("PathOnHost")
    if (not isinstance(device, str) or not re.fullmatch(r"/dev/[A-Za-z0-9._/-]+", device)
            or os.path.normpath(device) != device
            or devices[0].get("PathInContainer") != device
            or devices[0].get("CgroupPermissions") != "r"):
        raise ValueError("Hosted restore runtime XFS device is unavailable")
    return device


def _hosted_owner_storage_limit(receipt: dict) -> str:
    """The recorded deployment limit that owners without their own policy use."""
    limit = receipt.get("storage_limit_bytes")
    if type(limit) is not int or limit <= 0:
        raise ValueError("Hosted restore deployment storage limit is unavailable")
    return str(limit)


def run_package_restore(*, action: str, endpoint: str, receipt_path: Path,
                        archive: Path | None = None) -> dict | None:
    """Use one disposable helper container after exact stopped-container proof."""
    if action not in {"restore", "rollback", "commit"}:
        raise ValueError("Unknown package restore action")
    receipt = _package_receipt(receipt_path, endpoint)
    if action == "restore":
        if archive is None:
            raise ValueError("A private archive is required")
        _private_source(_trusted_root(archive))
    _assert_package_stopped(endpoint, receipt)
    quota_device = (_hosted_quota_device(endpoint, receipt)
                    if receipt["profile"] == "hosted-xfs" and action == "restore" else "")
    owner_limit = _hosted_owner_storage_limit(receipt) if quota_device else ""
    helper_name = "xperfect-g8-restore-" + uuid.uuid4().hex[:16]
    helper = ""
    try:
        create = ["create", "--name", helper_name, "--network", "none",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
            "--cap-add", "DAC_OVERRIDE"]
        if quota_device:
            create += ["--cap-add", "SYS_ADMIN", "--device", f"{quota_device}:{quota_device}:r"]
        create += [
            "--mount", f"type=volume,src={receipt['volumes']['data']},dst=/data,volume-nocopy",
            "--mount", f"type=volume,src={receipt['volumes']['control']},dst=/control,volume-nocopy",
            "--entrypoint", "/bin/sleep", receipt["service_image"], "3600",
        ]
        helper = _docker(endpoint, *create)
        if not re.fullmatch(r"[0-9a-f]{64}", helper):
            raise ValueError("Restore helper identity is invalid")
        _docker(endpoint, "start", helper)
        _assert_package_stopped(endpoint, receipt, helper=helper)
        _docker(endpoint, "cp", str(Path(__file__).resolve()), helper + ":/tmp/g8-restore.py")
        if action == "restore":
            _docker(endpoint, "cp", str(archive), helper + ":/tmp/g8-archive")
            # Docker Desktop can retain the host UID during cp. The helper runs
            # as service root, so rebind the already-validated private tree.
            _docker(endpoint, "exec", helper, "chown", "-R", "0:0", "/tmp/g8-archive")
            _docker(endpoint, "exec", helper, "chmod", "-R", "go-rwx", "/tmp/g8-archive")
            _docker(endpoint, "exec", helper, "mkdir", "-m", "700", "/tmp/g8-working")
        environment = [
            "XPERFECT_OFFLINE_RESTORE_STOPPED=1",
            "XPERFECT_EXECUTION_PROFILE=" + receipt["profile"],
            "XPERFECT_SHARED_VOLUME_ROOT=/data", "XPERFECT_CONTROL_ROOT=/control",
        ]
        if receipt["profile"] == "hosted-xfs":
            environment += ["XPERFECT_STORAGE_ROOT=/data",
                            "XPERFECT_STORAGE_REGISTRY_PATH=/control/storage/registry.sqlite3"]
        if owner_limit:
            environment.append("GLASSHIVE_OWNER_STORAGE_BYTES=" + owner_limit)
        command = ["exec"]
        for item in environment:
            command.extend(("--env", item))
        command.extend((helper, "/opt/xperfect/venvs/runtime/bin/python", "/tmp/g8-restore.py", action))
        if action == "restore":
            command.extend(("/tmp/g8-archive", "--staging-parent", "/tmp/g8-working"))
        command.extend(("--data-root", "/data", "--control-root", "/control"))
        output = _docker(endpoint, *command)
        _assert_package_stopped(endpoint, receipt, helper=helper)
        return json.loads(output) if action == "restore" else None
    finally:
        if helper:
            try:
                _docker(endpoint, "rm", "-f", helper)
            except RuntimeError:
                # A failed helper removal is an operator-visible state to inspect.
                raise RuntimeError("Offline restore helper cleanup failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--staging-parent", type=Path, required=True)
    for action in (restore, commands.add_parser("rollback"), commands.add_parser("commit")):
        action.add_argument("--data-root", type=Path, required=True)
        action.add_argument("--control-root", type=Path, required=True)
    for action in ("package-restore", "package-rollback", "package-commit"):
        package = commands.add_parser(action)
        package.add_argument("--docker-host", required=True)
        package.add_argument("--receipt", required=True, type=Path)
        if action == "package-restore":
            package.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.action.startswith("package-"):
        result = run_package_restore(action=args.action.removeprefix("package-"),
                                     endpoint=args.docker_host, receipt_path=args.receipt,
                                     archive=getattr(args, "archive", None))
        if result is not None:
            print(json.dumps(result, sort_keys=True))
        return
    # The package operator runs this process only after a Docker inventory proves
    # every service and Native box sharing these volumes is stopped. The function
    # API requires the same proof callback; the CLI requires an explicit marker.
    if os.environ.get("XPERFECT_OFFLINE_RESTORE_STOPPED") != "1":
        raise ValueError("Offline package stop proof is required")
    stopped = lambda: None
    if args.action == "restore":
        result = restore_offline(archive=args.archive, data_root=args.data_root,
                                 control_root=args.control_root, staging_parent=args.staging_parent,
                                 assert_stopped=stopped)
        print(json.dumps(result, sort_keys=True))
    elif args.action == "rollback":
        rollback_offline(data_root=args.data_root, control_root=args.control_root,
                         assert_stopped=stopped)
    else:
        commit_offline(data_root=args.data_root, control_root=args.control_root,
                       assert_stopped=stopped)


if __name__ == "__main__":
    main()
