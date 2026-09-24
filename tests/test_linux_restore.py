"""Disposable, stopped-package proof for separate Linux data/control volumes."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deployment.linux import restore as linux_restore
from workers_projects_runtime import native_continuity
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import WorkspaceFiles


@pytest.fixture
def packaged_archive(tmp_path, monkeypatch, request):
    source_data = tmp_path / "source-data"
    source_control = tmp_path / "source-control"
    source_data.mkdir(mode=0o700)
    (source_control / "workspaces").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "local-linux")
    monkeypatch.setenv("XPERFECT_SHARED_VOLUME_ROOT", str(source_data))
    monkeypatch.setenv("XPERFECT_CONTROL_ROOT", str(source_control))
    monkeypatch.setattr(native_continuity, "_shared_process_absent", lambda box: True)
    database = source_control / "runtime.sqlite"
    store = Store(str(database))
    ControlPlaneStore(str(database))
    project = store.create_project("owner", "Preserve", "Keep work", "codex-cli")
    worker = store.create_worker(project["project_id"], "owner", "Writer", "Write", "codex-cli",
                                 "codex", "codex-cli", "configured-model", execution_mode="docker")
    with store._connect() as connection:
        box = native_continuity._shared_box(connection, worker, data_root=source_data,
                                            control_root=source_control / "workspaces")
    paths = box.paths()
    paths["workspace_dir"].mkdir(parents=True)
    paths["state_dir"].mkdir(parents=True)
    (paths["workspace_dir"] / "report.txt").write_text("restored work")
    store.update_worker(worker["worker_id"], state="paused", state_dir=str(paths["state_dir"]),
                        workspace_dir=str(paths["workspace_dir"]))
    files = WorkspaceFiles(store)
    upload = files.create_upload("local", "owner", draft_id="draft", name="attachment.txt",
                                 size_bytes=3, idempotency_key="attachment")

    async def chunks():
        yield b"abc"

    asyncio.run(files.receive("local", "owner", upload["upload_id"], chunks()))
    deleted = None
    if getattr(request, "param", "") == "trash":
        entry = next(item for item in files.list_files(worker["worker_id"], "local", "owner")["items"]
                     if item["name"] == "report.txt")
        deleted = files.mutate(worker["worker_id"], "local", "owner", entry["file_id"],
                               revision=entry["revision"], delete=True)
    archive = tmp_path / "archive"
    native_continuity.capture_state(database, archive)
    store.close()
    target_data = tmp_path / "target-data"
    target_control = tmp_path / "target-control"
    target_data.mkdir(mode=0o700)
    target_control.mkdir(mode=0o700)
    (target_data / "execution_workspaces").mkdir(mode=0o700)
    (target_data / "execution_workspaces" / "old.txt").write_text("old data")
    previous = Store(str(target_control / "runtime.db"))
    previous.create_project("previous-owner", "Old control", "Keep prior state", "codex-cli")
    previous.close()
    (target_control / "config.json").write_text('{"retain":"package config"}')
    staging = tmp_path / "offline-staging"
    staging.mkdir(mode=0o700)
    monkeypatch.setenv("XPERFECT_SHARED_VOLUME_ROOT", str(target_data))
    monkeypatch.setenv("XPERFECT_CONTROL_ROOT", str(target_control))
    return archive, target_data, target_control, staging, worker, upload, deleted


def _restore(packaged_archive, *, after_swap=None):
    archive, data, control, staging, _, _, _ = packaged_archive
    return linux_restore.restore_offline(
        archive=archive, data_root=data, control_root=control, staging_parent=staging,
        assert_stopped=lambda: None, after_swap=after_swap,
    )


def test_offline_restore_installs_both_volumes_and_can_roll_back(packaged_archive):
    archive, data, control, _, worker, upload, _ = packaged_archive
    report = _restore(packaged_archive)
    assert report["awaiting_commit"] is True
    assert (control / ".g8-restore-journal.json").is_file()
    assert (control / "config.json").read_text() == '{"retain":"package config"}'
    assert not (data / "execution_workspaces" / "old.txt").exists()
    restored = Store(str(control / "runtime.db"))
    info = restored.get_worker(worker["worker_id"])
    assert (Path(info["workspace_dir"]) / "report.txt").read_text() == "restored work"
    assert next((data / "managed-files").rglob(f"{upload['upload_id']}.blob")).read_bytes() == b"abc"
    restored.close()
    linux_restore.rollback_offline(data_root=data, control_root=control, assert_stopped=lambda: None)
    assert (data / "execution_workspaces" / "old.txt").read_text() == "old data"
    with sqlite3.connect(control / "runtime.db") as connection:
        assert connection.execute("SELECT title FROM projects").fetchone()[0] == "Old control"
    assert not (control / ".g8-restore-journal.json").exists()
    assert archive.is_dir()


@pytest.mark.parametrize("failure_index", [0, 4])
def test_offline_restore_failure_rolls_back_every_volume(packaged_archive, failure_index):
    archive, data, control, staging, _, _, _ = packaged_archive

    def fault(index):
        if index == failure_index:
            raise RuntimeError("synthetic install interruption")

    with pytest.raises(RuntimeError, match="synthetic install interruption"):
        _restore(packaged_archive, after_swap=fault)
    assert (data / "execution_workspaces" / "old.txt").read_text() == "old data"
    with sqlite3.connect(control / "runtime.db") as connection:
        assert connection.execute("SELECT title FROM projects").fetchone()[0] == "Old control"
    assert not (control / ".g8-restore-journal.json").exists()
    assert not list(staging.iterdir())
    assert archive.is_dir()


def test_offline_restore_commit_removes_backup_after_install(packaged_archive):
    archive, data, control, _, worker, _, _ = packaged_archive
    report = _restore(packaged_archive)
    assert report["awaiting_commit"]
    linux_restore.commit_offline(data_root=data, control_root=control, assert_stopped=lambda: None)
    assert not (control / ".g8-restore-journal.json").exists()
    assert not list(data.glob(".g8-backup-*"))
    assert not list(control.glob(".g8-backup-*"))
    assert Store(str(control / "runtime.db")).get_worker(worker["worker_id"])
    assert archive.is_dir()


@pytest.mark.parametrize("packaged_archive", ["trash"], indirect=True)
def test_offline_restore_binds_trash_after_install_for_real_undo(packaged_archive):
    _, data, control, _, worker, _, deleted = packaged_archive
    report = _restore(packaged_archive)
    assert report["trash_bound"] == 1
    restored = Store(str(control / "runtime.db"))
    files = WorkspaceFiles(restored)
    assert files.undo(worker["worker_id"], "local", "owner", deleted["file_id"],
                      deleted["undo_id"])["state"] == "restored"
    workspace = Path(restored.get_worker(worker["worker_id"])["workspace_dir"])
    assert (workspace / "report.txt").read_text() == "restored work"
    restored.close()
    # The installed target is still rollback-capable until the caller commits.
    linux_restore.rollback_offline(data_root=data, control_root=control, assert_stopped=lambda: None)


@pytest.mark.parametrize("policy", ["owner", "deployment"])
def test_quota_restore_swaps_native_owner_state_and_supports_next_capture(tmp_path, monkeypatch, policy):
    source_data = tmp_path / "source-data"
    source_control = tmp_path / "source-control"
    target_data = tmp_path / "target-data"
    target_control = tmp_path / "target-control"
    staging = tmp_path / "staging"
    digest = hashlib.sha256(b"local\0owner").hexdigest()
    source_owner = source_data / "owners" / digest
    target_owner = target_data / "owners" / digest
    source_owner.mkdir(parents=True, mode=0o700)
    target_owner.mkdir(parents=True, mode=0o700)
    staging.mkdir(mode=0o700)
    source_native = source_control / "storage" / "runtime-storage" / "workspaces"
    target_native = target_control / "storage" / "runtime-storage" / "workspaces"
    source_native.mkdir(parents=True, mode=0o700)
    target_native.mkdir(parents=True, mode=0o700)
    (target_native / "old-state.txt").write_text("prior state")
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "local-linux")
    monkeypatch.setenv("XPERFECT_STORAGE_ROOT", str(source_data))
    monkeypatch.setenv("XPERFECT_STORAGE_REGISTRY_PATH", str(source_control / "storage" / "registry.sqlite"))
    monkeypatch.setattr(native_continuity, "_shared_process_absent", lambda box: True)
    current_owner = [source_owner]
    limits = []
    monkeypatch.setattr(native_continuity, "_quota_backend", lambda: SimpleNamespace(
        snapshot=lambda tenant, owner, limit: limits.append(limit) or SimpleNamespace(root=current_owner[0])
    ))
    source_db = source_control / "runtime.db"
    store = Store(str(source_db))
    ControlPlaneStore(str(source_db))
    project = store.create_project("owner", "Quota", "Preserve state", "codex-cli")
    workspace = store.create_execution_workspace(
        project_id=project["project_id"], tenant_id="local", owner_id="owner",
        execution_mode="docker", file_placement="common",
    )
    worker = store.create_worker(
        project["project_id"], "owner", "Writer", "Write", "codex-cli", "codex",
        "codex-cli", "configured-model", workspace_id=workspace["workspace_id"],
    )
    with store._connect() as connection:
        box = native_continuity._shared_box(
            connection, worker, data_root=source_owner, control_root=source_native,
        )
    paths = box.paths()
    paths["workspace_dir"].mkdir(parents=True)
    paths["state_dir"].mkdir(parents=True)
    (paths["workspace_dir"] / "retained.txt").write_text("quota work")
    store.update_worker(
        worker["worker_id"], state="paused", workspace_dir=str(paths["workspace_dir"]),
        state_dir=str(paths["state_dir"]),
    )
    with store._connect() as connection:
        if policy == "owner":
            connection.execute(
                "INSERT INTO workspace_file_policies VALUES (?,?,?,?,?,?)",
                ("local", "owner", 4096, None, None, None),
            )
        else:
            # The owner has no row of its own and uses the deployment limit.
            monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "4096")
        connection.execute("UPDATE workspace_file_storage_config SET quota_required=1")
    archive = tmp_path / "archive"
    native_continuity.capture_state(source_db, archive)
    archived_state = archive / "control" / "storage" / "runtime-storage" / "workspaces"
    assert archived_state.is_dir()
    assert not (archive / "control" / "workspaces").exists()
    store.close()
    previous = Store(str(target_control / "runtime.db"))
    previous.create_project("previous-owner", "Prior", "Keep prior", "codex-cli")
    previous.close()
    current_owner[0] = target_owner
    monkeypatch.setenv("XPERFECT_STORAGE_ROOT", str(target_data))
    monkeypatch.setenv("XPERFECT_STORAGE_REGISTRY_PATH", str(target_control / "storage" / "registry.sqlite"))
    report = linux_restore.restore_offline(
        archive=archive, data_root=target_data, control_root=target_control,
        staging_parent=staging, assert_stopped=lambda: None,
    )
    assert report["awaiting_commit"]
    assert not (target_native / "old-state.txt").exists()
    restored = Store(str(target_control / "runtime.db"))
    current = restored.get_worker(worker["worker_id"])
    assert Path(current["state_dir"]) == target_native / Path(paths["state_dir"]).relative_to(source_native)
    assert (Path(current["workspace_dir"]) / "retained.txt").read_text() == "quota work"
    native_continuity.check_quiescent(target_control / "runtime.db")
    reexport = tmp_path / "next-archive"
    native_continuity.capture_state(target_control / "runtime.db", reexport)
    assert (reexport / "control" / "storage" / "runtime-storage" / "workspaces").is_dir()
    restored.close()
    linux_restore.rollback_offline(
        data_root=target_data, control_root=target_control, assert_stopped=lambda: None,
    )
    assert (target_native / "old-state.txt").read_text() == "prior state"
    with sqlite3.connect(target_control / "runtime.db") as connection:
        assert connection.execute("SELECT title FROM projects").fetchone()[0] == "Prior"
    linux_restore.restore_offline(
        archive=archive, data_root=target_data, control_root=target_control,
        staging_parent=staging, assert_stopped=lambda: None,
    )
    linux_restore.commit_offline(
        data_root=target_data, control_root=target_control, assert_stopped=lambda: None,
    )
    assert not (target_control / ".g8-restore-journal.json").exists()
    assert not (target_native / "old-state.txt").exists()
    assert limits and set(limits) == {4096}


def test_quota_owner_without_own_policy_uses_only_the_passed_deployment_limit(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE workspace_file_policies (tenant_id, owner_id, storage_limit_bytes,"
                       " max_file_bytes, max_batch_files, max_batch_bytes)")
    limits = []
    backend = SimpleNamespace(snapshot=lambda tenant, owner, limit: limits.append(limit)
                              or SimpleNamespace(root="/data/owners/one"))
    for value in (None, "", "5e9", "-1", " 4096", "\u0664"):
        if value is None:
            monkeypatch.delenv("GLASSHIVE_OWNER_STORAGE_BYTES", raising=False)
        else:
            monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", value)
        with pytest.raises(ValueError, match="durable owner storage policy"):
            native_continuity._quota_owner_root(connection, backend, "local", "owner")
    assert limits == []
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "5000000000")
    assert native_continuity._quota_owner_root(connection, backend, "local", "owner") == Path("/data/owners/one")
    connection.execute("INSERT INTO workspace_file_policies VALUES ('local','owner',4096,NULL,NULL,NULL)")
    native_continuity._quota_owner_root(connection, backend, "local", "owner")
    assert limits == [5_000_000_000, 4096]


def test_restore_refuses_a_package_with_an_open_upgrade(tmp_path, monkeypatch):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    receipt.chmod(0o600)
    (tmp_path / "receipt.json.upgrade.json").write_text("{}")
    monkeypatch.setattr(linux_restore, "_docker", lambda *args: pytest.fail("Docker must not be touched"))
    with pytest.raises(ValueError, match="upgrade of this package is open"):
        linux_restore._package_receipt(receipt, "unix:///docker.sock")


def test_package_wrapper_rejects_running_service_before_helper(monkeypatch):
    receipt = {"containers": {"runtime": "a" * 64, "ui": "b" * 64, "mcp": "c" * 64},
               "volumes": {"data": "xperfect-test-data", "control": "xperfect-test-control"}}

    def docker(endpoint, *args):
        if args[:2] == ("volume", "inspect"):
            return json.dumps({"Name": args[-1], "Labels": {"xperfect.package": "xperfect-test"}})
        assert args[0] == "inspect"
        return json.dumps({"Id": "a" * 64, "State": {"Running": True},
                           "Config": {"Labels": {"xperfect.role": "runtime",
                                                  "xperfect.package": "xperfect-test"}}})

    monkeypatch.setattr(linux_restore, "_docker", docker)
    with pytest.raises(ValueError, match="service is not stopped"):
        linux_restore._assert_package_stopped("unix:///docker.sock", receipt)


def test_package_wrapper_uses_exact_stopped_volumes_and_disposable_helper(packaged_archive, tmp_path, monkeypatch):
    archive, _, _, _, _, _, _ = packaged_archive
    image = "sha256:" + "d" * 64
    containers = {"runtime": "a" * 64, "ui": "b" * 64, "mcp": "c" * 64}
    volumes = {"data": "xperfect-test-data", "control": "xperfect-test-control"}
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"profile": "local-linux", "service_image": image,
                                   "containers": containers, "volumes": volumes}))
    receipt.chmod(0o600)
    helper = "e" * 64
    calls = []
    started = [False]

    def docker(endpoint, *args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return image
        if args[0] == "inspect":
            role = next(role for role, identity in containers.items() if identity == args[-1])
            return json.dumps({"Id": containers[role], "State": {"Running": False},
                               "Config": {"Labels": {"xperfect.role": role,
                                                      "xperfect.package": "xperfect-test"}}})
        if args[:2] == ("volume", "inspect"):
            return json.dumps({"Name": args[-1], "Labels": {"xperfect.package": "xperfect-test"}})
        if args[0] == "ps":
            return helper[:12] if started[0] else ""
        if args[0] == "create":
            return helper
        if args[0] == "start":
            started[0] = True
            return helper
        if args[0] == "exec":
            return json.dumps({"transaction_id": "f" * 32, "awaiting_commit": True})
        if args[0] in {"cp", "rm"}:
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(linux_restore, "_docker", docker)
    result = linux_restore.run_package_restore(action="restore", endpoint="unix:///docker.sock",
                                               receipt_path=receipt, archive=archive)
    assert result["awaiting_commit"] is True
    create = next(args for args in calls if args[0] == "create")
    assert ("--network", "none") == create[create.index("--network"):create.index("--network") + 2]
    assert "SYS_ADMIN" not in create and "--device" not in create
    assert any("src=xperfect-test-data,dst=/data" in item for item in create)
    assert any("src=xperfect-test-control,dst=/control" in item for item in create)
    assert ("exec", helper, "chown", "-R", "0:0", "/tmp/g8-archive") in calls
    assert ("exec", helper, "chmod", "-R", "go-rwx", "/tmp/g8-archive") in calls
    assert ("exec", helper, "mkdir", "-m", "700", "/tmp/g8-working") in calls
    restore_command = next(args for args in calls if args[0] == "exec" and "/tmp/g8-restore.py" in args)
    assert restore_command[restore_command.index("--staging-parent") + 1] == "/tmp/g8-working"
    assert not any(args[0] in {"stop", "start"} and args[-1] in containers.values() for args in calls)
    assert calls[-1] == ("rm", "-f", helper)


def _hosted_runtime_inspect(identity, *, device="/dev/loop7", caps=None, mounts=None):
    return {"Id": identity, "State": {"Running": False},
            "Config": {"Labels": {"xperfect.role": "runtime", "xperfect.package": "xperfect-test"}},
            "HostConfig": {
                "CapAdd": ["CAP_CHOWN", "CAP_FOWNER", "CAP_DAC_OVERRIDE", "CAP_SYS_ADMIN"] if caps is None else caps,
                "Devices": [{"PathOnHost": device, "PathInContainer": device,
                             "CgroupPermissions": "r"}],
                "Mounts": [{"Type": "volume", "Source": "xperfect-test-data",
                            "Target": "/data", "ReadOnly": False}] if mounts is None else mounts,
            }}


def test_hosted_restore_helper_reuses_exact_runtime_quota_device_only_for_restore(
        packaged_archive, tmp_path, monkeypatch):
    archive = packaged_archive[0]
    image = "sha256:" + "d" * 64
    containers = {role: digit * 64 for role, digit in (("runtime", "a"), ("ui", "b"), ("mcp", "c"))}
    volumes = {"data": "xperfect-test-data", "control": "xperfect-test-control"}
    receipt = tmp_path / "hosted-receipt.json"
    receipt.write_text(json.dumps({"profile": "hosted-xfs", "service_image": image,
                                   "containers": containers, "volumes": volumes,
                                   "storage_limit_bytes": 5_000_000_000}))
    receipt.chmod(0o600)
    calls = []
    live_helper = [""]
    created = []

    def docker(endpoint, *args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return image
        if args[:2] == ("volume", "inspect"):
            return json.dumps({"Name": args[-1], "Driver": "local",
                               "Labels": {"xperfect.package": "xperfect-test"},
                               "Options": {"type": "none", "o": "bind",
                                           "device": "/srv/xperfect-data"} if args[-1] == volumes["data"] else {}})
        if args[0] == "inspect":
            role = next(role for role, identity in containers.items() if identity == args[-1])
            if role == "runtime":
                return json.dumps(_hosted_runtime_inspect(containers[role]))
            return json.dumps({"Id": containers[role], "State": {"Running": False},
                               "Config": {"Labels": {"xperfect.role": role,
                                                      "xperfect.package": "xperfect-test"}}})
        if args[0] == "ps":
            return live_helper[0][:12]
        if args[0] == "create":
            identity = format(len(created) + 1, "x") * 64
            created.append(args)
            return identity
        if args[0] == "start":
            live_helper[0] = args[1]
            return args[1]
        if args[0] == "exec":
            return json.dumps({"awaiting_commit": True}) if "/tmp/g8-restore.py" in args else ""
        if args[0] == "rm":
            assert args[-1] == live_helper[0]
            live_helper[0] = ""
            return ""
        if args[0] == "cp":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(linux_restore, "_docker", docker)
    for action in ("restore", "rollback", "restore", "commit"):
        result = linux_restore.run_package_restore(
            action=action, endpoint="unix:///docker.sock", receipt_path=receipt,
            archive=archive if action == "restore" else None,
        )
        assert (result is not None) == (action == "restore")
        assert live_helper[0] == ""
    assert len(created) == 4
    helper_runs = [call for call in calls if call[0] == "exec" and "/tmp/g8-restore.py" in call]
    assert [("--env", "GLASSHIVE_OWNER_STORAGE_BYTES=5000000000") in zip(run, run[1:])
            for run in helper_runs] == [True, False, True, False]
    for index, create in enumerate(created):
        assert create[create.index("--network") + 1] == "none"
        assert "--privileged" not in create
        assert "--cap-drop" in create and create[create.index("--cap-drop") + 1] == "ALL"
        if index in (0, 2):
            assert ("--cap-add", "SYS_ADMIN", "--device", "/dev/loop7:/dev/loop7:r") == (
                create[create.index("SYS_ADMIN") - 1:create.index("SYS_ADMIN") + 3])
        else:
            assert "SYS_ADMIN" not in create and "--device" not in create


@pytest.mark.parametrize("invalid", ["bind", "cap", "device", "device_path", "mount",
                                    "container_path", "device_write", "limit"])
def test_hosted_restore_rejects_unbound_quota_authority_before_helper_create(
        packaged_archive, tmp_path, monkeypatch, invalid):
    image = "sha256:" + "d" * 64
    containers = {role: digit * 64 for role, digit in (("runtime", "a"), ("ui", "b"), ("mcp", "c"))}
    receipt = tmp_path / "hosted-receipt.json"
    recorded = {"profile": "hosted-xfs", "service_image": image, "containers": containers,
                "volumes": {"data": "xperfect-test-data", "control": "xperfect-test-control"},
                "storage_limit_bytes": 5_000_000_000}
    if invalid == "limit":
        recorded["storage_limit_bytes"] = True
    receipt.write_text(json.dumps(recorded))
    receipt.chmod(0o600)
    calls = []

    def docker(endpoint, *args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return image
        if args[:2] == ("volume", "inspect"):
            options = {"type": "none", "o": "bind", "device": "/srv/xperfect-data"}
            if invalid == "bind" and args[-1] == "xperfect-test-data":
                options["device"] = "relative/data"
            return json.dumps({"Name": args[-1], "Driver": "local",
                               "Labels": {"xperfect.package": "xperfect-test"}, "Options": options})
        if args[0] == "inspect":
            role = next(role for role, identity in containers.items() if identity == args[-1])
            if role == "runtime":
                runtime = _hosted_runtime_inspect(containers[role],
                    device="/dev/loop7:other" if invalid == "device_path" else "/dev/loop7",
                    caps=["CHOWN"] if invalid == "cap" else None,
                    mounts=[] if invalid == "mount" else None)
                if invalid == "device":
                    runtime["HostConfig"]["Devices"] = []
                elif invalid == "container_path":
                    runtime["HostConfig"]["Devices"][0]["PathInContainer"] = "/dev/other"
                elif invalid == "device_write":
                    runtime["HostConfig"]["Devices"][0]["CgroupPermissions"] = "rwm"
                return json.dumps(runtime)
            return json.dumps({"Id": containers[role], "State": {"Running": False},
                               "Config": {"Labels": {"xperfect.role": role,
                                                      "xperfect.package": "xperfect-test"}}})
        if args[0] == "ps":
            return ""
        raise AssertionError("No helper mutation is allowed before hosted authority proof")

    monkeypatch.setattr(linux_restore, "_docker", docker)
    with pytest.raises(ValueError, match="Hosted restore"):
        linux_restore.run_package_restore(action="restore", endpoint="unix:///docker.sock",
            receipt_path=receipt, archive=packaged_archive[0])
    assert not any(call[0] == "create" for call in calls)
