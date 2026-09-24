from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.store import Store


def source_state(tmp_path: Path):
    database = tmp_path / "source" / "runtime.sqlite"
    database.parent.mkdir(mode=0o700)
    store = Store(str(database))
    control = ControlPlaneStore(str(database))
    project = store.create_project("owner-a", "Keep my work", "Finish the report", "codex-cli")
    workspace = database.parent / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("Useful finished work\n")
    (workspace / ".env").write_text("SYNTHETIC_SECRET=never-export\n")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "auth.json").write_text('{"token":"never-export"}')
    worker = store.create_worker(
        project["project_id"], "owner-a", "Writer", "Help finish", "codex-cli", "codex",
        "codex-cli", "configured-model", execution_mode="host",
        bootstrap_bundle={"project_definition": "Finish the report", "mcp_servers": {"secret": "never-export"}},
    )
    store.update_worker(worker["worker_id"], workspace_dir=str(workspace), state="paused", pid=123,
                        gateway_token="never-export", session_key="old-live-session")
    account = control.create_provider_account(
        tenant_id="local", owner_id="owner-a", provider="codex", label="Account", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://private-account", status="ready",
    )
    return database, store, worker, account


def test_native_capture_preserves_identity_files_and_requires_reconnect(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state

    database, source, worker, account = source_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    report = capture_state(database, snapshot)
    assert report["workers"] == 1
    assert report["workspace_files"] == 1
    assert source.get_worker(worker["worker_id"])["gateway_token"] == "never-export"
    assert not any(b"never-export" in path.read_bytes() for path in snapshot.rglob("*") if path.is_file())

    final_root = tmp_path / "restored"
    prepare_restored_state(snapshot, final_root)
    restored = Store(str(snapshot / "runtime.sqlite"))
    result = restored.get_worker(worker["worker_id"])
    assert result["project_id"] == worker["project_id"]
    assert result["state"] == "paused"
    assert result["pid"] is None
    assert not result["gateway_token"] and not result["session_key"]
    assert json.loads(result["bootstrap_bundle_json"]) == {"project_definition": "Finish the report"}
    relative = Path(result["workspace_dir"]).relative_to(final_root)
    assert (snapshot / relative / "report.md").read_text() == "Useful finished work\n"
    restored_account = ControlPlaneStore(str(snapshot / "runtime.sqlite")).get_provider_account(
        account_id=account["account_id"], tenant_id="local", owner_id="owner-a",
    )
    assert restored_account["status"] == "action_required"


def test_native_capture_preserves_executable_work_and_clears_existing_compute_contract(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    from workers_projects_runtime.store import COMPUTE_OPERATION_CLEAR_FIELDS

    database, store, worker, _ = source_state(tmp_path)
    script = database.parent / "workspace" / "build.sh"
    script.write_text("#!/bin/sh\nprintf useful\\n\n")
    script.chmod(0o700)
    store.update_worker(worker["worker_id"], compute_release_token="stale", compute_release_container_id="old-container", control_url="http://old-control")
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    prepare_restored_state(snapshot, tmp_path / "destination")
    restored = Store(str(snapshot / "runtime.sqlite")).get_worker(worker["worker_id"])
    assert all(restored[key] == value for key, value in COMPUTE_OPERATION_CLEAR_FIELDS.items())
    assert restored["control_url"] is None
    captured_script = next(snapshot.rglob("build.sh"))
    assert captured_script.stat().st_mode & 0o777 == 0o700


def test_native_capture_rejects_workspace_escape_and_cleans_partial_output(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, _, _, _ = source_state(tmp_path)
    (database.parent / "workspace" / "escape").symlink_to(tmp_path / "outside")
    destination = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="unsafe workspace symlink"):
        capture_state(database, destination)
    assert not destination.exists()


def test_native_capture_rejects_unreviewed_future_database_without_touching_source(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, _, _, _ = source_state(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE glasshive_schema_versions SET version = 999 WHERE component = 'runtime_store'")
    with pytest.raises(ValueError, match="schema"):
        capture_state(database, tmp_path / "snapshot")


def test_native_schema_accepts_only_complete_owner_compositions(tmp_path):
    from workers_projects_runtime.coordinator import CoordinatorService
    from workers_projects_runtime.native_continuity import _require_schema
    from workers_projects_runtime.worker_configuration import WorkerConfiguration

    class Provider:
        pass

    for optional in ((), ("coordinator",), ("worker_configuration",), ("coordinator", "worker_configuration")):
        database = tmp_path / ("-".join(optional) or "base") / "runtime.sqlite"
        database.parent.mkdir(parents=True)
        store = Store(str(database))
        ControlPlaneStore(str(database))
        if "coordinator" in optional:
            CoordinatorService(store, object(), Provider())
        if "worker_configuration" in optional:
            WorkerConfiguration(store, object())
        with sqlite3.connect(database) as connection:
            _require_schema(connection)
        store.close()


def test_native_schema_rejects_weakened_table_check(tmp_path):
    from workers_projects_runtime.native_continuity import _require_schema

    database, store, _, _ = source_state(tmp_path)
    with sqlite3.connect(database) as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='execution_workspaces'"
        ).fetchone()[0]
        weakened = original.replace("CHECK(mode IN ('isolated', 'shared'))", "CHECK(1)")
        assert weakened != original
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='execution_workspaces'", (weakened,)
        )
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version={version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(ValueError, match="unreviewed schema shape"):
            _require_schema(connection)
    store.close()


def test_native_coordinator_projection_retains_intent_and_unicode_resume(tmp_path):
    from types import SimpleNamespace

    from workers_projects_runtime.coordinator import CoordinatorConfig, CoordinatorService, Goal
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state

    class Provider:
        def _model(self, _):
            return SimpleNamespace(effort_choices=("high",))

    database, source, _, _ = source_state(tmp_path)
    coordinator = CoordinatorService(source, SimpleNamespace(), Provider())
    secret = "SYNTHETIC_G8_COORDINATOR_TYPED_CREDENTIAL"
    config = CoordinatorConfig(
        model="synthetic-model", effort="high",
        bootstrap_bundle={
            "project_definition": "Retain the goal",
            "env": {"API_KEY": secret},
            "claude_project_mcp": {"mcpServers": {"synthetic": {
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer " + secret},
            }}},
        },
    )
    identity = coordinator.create("local", "owner-a", config)["conversation_id"]
    coordinator.accept_turn("local", "owner-a", identity, "türn", "Keep this exact text",
                            [Goal(id="göal", text="Preserve this goal")])
    with source._connect() as connection:
        connection.execute(
            "UPDATE coordinator_turns SET payload_json=? WHERE conversation_id=? AND turn_id=?",
            (json.dumps({"messages": [{"content": "Keep this exact text"}],
                         "metadata": {"bootstrap_bundle": config.bootstrap_bundle}}),
             identity, "türn"),
        )
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    prepare_restored_state(snapshot, tmp_path / "restored")
    restored = Store(str(snapshot / "runtime.sqlite"))
    with restored._connect() as connection:
        saved = connection.execute(
            "SELECT config_json,restore_hold_set_hash FROM coordinator_conversations WHERE conversation_id=?",
            (identity,),
        ).fetchone()
        payload = connection.execute(
            "SELECT payload_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
            (identity, "türn"),
        ).fetchone()[0]
    assert secret not in saved[0] and secret not in payload
    assert json.loads(saved[0])["bootstrap_bundle"]["project_definition"] == "Retain the goal"
    assert json.loads(saved[0])["bootstrap_bundle"]["claude_project_mcp"]["mcpServers"]["synthetic"]["url"] == "https://example.invalid/mcp"
    assert json.loads(payload)["messages"][0]["content"] == "Keep this exact text"
    resumed = CoordinatorService(restored, SimpleNamespace(), Provider())
    resumed.resume_restored("local", "owner-a", identity, saved[1],
                            approved_turn_ids=["türn"], approved_goal_ids=["göal"])
    assert secret.encode() not in (snapshot / "runtime.sqlite").read_bytes()
    restored.close()
    source.close()


def test_native_files_capture_retains_ready_version_and_rejects_live_transfer(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, check_quiescent, prepare_restored_state
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    database, source, _, _ = source_state(tmp_path)
    files = WorkspaceFiles(source)
    upload = files.create_upload("local", "owner-a", draft_id="durable-draft", name="brief.txt",
                                 size_bytes=3, idempotency_key="upload")

    async def chunks():
        yield b"abc"

    ready = asyncio.run(files.receive("local", "owner-a", upload["upload_id"], chunks()))
    snapshot = tmp_path / "snapshot"
    report = capture_state(database, snapshot)
    assert report["managed_files"] == 1 and report["managed_bytes"] == 3
    prepare_restored_state(snapshot, tmp_path / "restored")
    restored = Store(str(snapshot / "runtime.sqlite"))
    restored_files = WorkspaceFiles(restored)
    assert restored_files.get_upload("local", "owner-a", upload["upload_id"])["state"] == "ready"
    assert (restored_files._owner_root("local", "owner-a") / f"{upload['upload_id']}.blob").read_bytes() == b"abc"
    restored.close()

    in_flight = files.create_upload("local", "owner-a", draft_id="second-draft",
                                    name="pending.txt", size_bytes=1, idempotency_key="pending")
    with source._connect() as connection:
        connection.execute(
            "UPDATE workspace_file_uploads SET state='receiving',lease_id='synthetic-live-lease',lease_until=? WHERE upload_id=?",
            (time.time() + 120, in_flight["upload_id"]),
        )
    with pytest.raises(ValueError, match="Files transfers must settle"):
        check_quiescent(database)
    source.close()


def test_native_files_capture_and_prepare_reject_missing_ready_blob(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    database, source, _, _ = source_state(tmp_path)
    files = WorkspaceFiles(source)
    upload = files.create_upload("local", "owner-a", draft_id="draft", name="source.txt",
                                 size_bytes=3, idempotency_key="key")

    async def chunks():
        yield b"abc"

    asyncio.run(files.receive("local", "owner-a", upload["upload_id"], chunks()))
    original = files._owner_root("local", "owner-a") / f"{upload['upload_id']}.blob"
    original.unlink()
    with pytest.raises(ValueError, match="missing or unsettled payloads"):
        capture_state(database, tmp_path / "missing-snapshot")
    assert not (tmp_path / "missing-snapshot").exists()
    original.write_bytes(b"abc")
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    captured = next(snapshot.rglob(f"{upload['upload_id']}.blob"))
    captured.write_bytes(b"abd")
    with pytest.raises(ValueError, match="digest disagrees"):
        prepare_restored_state(snapshot, tmp_path / "restored")
    source.close()


@pytest.mark.parametrize("placement", ["common", "member_private"])
def test_native_shared_capture_preserves_placement_and_distinct_members(tmp_path, monkeypatch, placement):
    from workers_projects_runtime import native_continuity as continuity

    database, store, _, _ = source_state(tmp_path)
    project = store.create_project("owner-a", "Shared work", "Keep one shared report", "codex-cli")
    workspace = store.create_execution_workspace(
        project_id=project["project_id"], tenant_id="local", owner_id="owner-a",
        execution_mode="docker", file_placement=placement,
    )
    data = tmp_path / "shared-data"
    control = data / "workspace-control"
    control.mkdir(parents=True)
    monkeypatch.setenv("XPERFECT_SHARED_VOLUME_ROOT", str(data))
    monkeypatch.setattr(continuity, "_shared_process_absent", lambda box: True)
    members = []
    for name in ("Writer", "Editor"):
        worker = store.create_worker(
            project["project_id"], "owner-a", name, "Keep report", "codex-cli", "codex",
            "codex-cli", "configured-model", workspace_id=workspace["workspace_id"],
        )
        with store._connect() as connection:
            box = continuity._shared_box(connection, worker, data_root=data, control_root=control)
        paths = box.paths()
        paths["workspace_dir"].mkdir(parents=True, exist_ok=True)
        paths["state_dir"].mkdir(parents=True, exist_ok=True)
        store.update_worker(worker["worker_id"], state="paused", state_dir=str(paths["state_dir"]),
                            workspace_dir=str(paths["workspace_dir"]))
        if placement == "member_private":
            (paths["workspace_dir"] / "report.txt").write_text(name)
        members.append(worker)
    if placement == "common":
        (data / "execution_workspaces" / workspace["workspace_id"] / "common" / "report.txt").write_text("shared")
    snapshot = tmp_path / "snapshot"
    report = continuity.capture_state(database, snapshot)
    assert report["workspace_files"] == (2 if placement == "common" else 3)
    restored = Store(str(snapshot / "runtime.sqlite"))
    paths = [Path(restored.get_worker(member["worker_id"])["workspace_dir"]) for member in members]
    assert (paths[0] == paths[1]) is (placement == "common")
    assert [(snapshot / path / "report.txt").read_text() for path in paths] == (
        ["shared", "shared"] if placement == "common" else ["Writer", "Editor"]
    )
    restored.close()
    target_data = tmp_path / "destination-data"
    target_control = tmp_path / "destination-control"
    continuity.prepare_restored_state(snapshot, tmp_path / "destination", data_root=target_data,
                                      control_root=target_control)
    restored = Store(str(snapshot / "runtime.sqlite"))
    rebound = [Path(restored.get_worker(member["worker_id"])["workspace_dir"]) for member in members]
    assert (rebound[0] == rebound[1]) is (placement == "common")
    assert rebound[0].is_relative_to(target_data)
    assert all(Path(restored.get_worker(member["worker_id"])["state_dir"]).is_relative_to(target_control)
               for member in members)
    monkeypatch.setattr(continuity, "_shared_process_absent", lambda box: False)
    with pytest.raises(ValueError, match="Native workspace process absence is unproved"):
        continuity.check_quiescent(database)
    monkeypatch.setattr(continuity, "_shared_process_absent", lambda box: True)
    store.update_worker(members[0]["worker_id"], workspace_dir=str(tmp_path / "foreign-owner"))
    with pytest.raises(ValueError, match="recorded Native path differs"):
        continuity.check_quiescent(database)
    restored.close()
    store.close()


def test_native_quota_capture_requires_matching_target_owner_binding(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from workers_projects_runtime import native_continuity as continuity
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    database = tmp_path / "source" / "runtime.sqlite"
    database.parent.mkdir()
    store = Store(str(database))
    ControlPlaneStore(str(database))
    project = store.create_project("owner-a", "Quota work", "Preserve work", "codex-cli")
    workspace = store.create_execution_workspace(
        project_id=project["project_id"], tenant_id="local", owner_id="owner-a",
        execution_mode="docker", file_placement="common",
    )
    worker = store.create_worker(
        project["project_id"], "owner-a", "Writer", "Write", "codex-cli", "codex",
        "codex-cli", "configured-model", workspace_id=workspace["workspace_id"],
    )
    digest = __import__("hashlib").sha256(b"local\0owner-a").hexdigest()
    source_data = tmp_path / "quota-data"
    source_owner = source_data / "owners" / digest
    source_owner.mkdir(parents=True)
    registry = tmp_path / "quota-control" / "registry.sqlite"
    control = registry.parent / "runtime-storage" / "workspaces"
    control.mkdir(parents=True)
    monkeypatch.setenv("XPERFECT_STORAGE_ROOT", str(source_data))
    monkeypatch.setenv("XPERFECT_STORAGE_REGISTRY_PATH", str(registry))
    current_owner = [source_owner]
    monkeypatch.setattr(continuity, "_quota_backend", lambda: SimpleNamespace(
        snapshot=lambda tenant, owner, limit: SimpleNamespace(root=current_owner[0])
    ))
    monkeypatch.setattr(continuity, "_shared_process_absent", lambda box: True)
    with store._connect() as connection:
        box = continuity._shared_box(connection, worker, data_root=source_owner, control_root=control)
    paths = box.paths()
    paths["workspace_dir"].mkdir(parents=True)
    paths["state_dir"].mkdir(parents=True)
    (paths["workspace_dir"] / "report.txt").write_text("quota work")
    store.update_worker(worker["worker_id"], state="paused", workspace_dir=str(paths["workspace_dir"]),
                        state_dir=str(paths["state_dir"]))
    files = WorkspaceFiles(store)
    upload = files.create_upload("local", "owner-a", draft_id="draft", name="data.txt",
                                 size_bytes=3, idempotency_key="quota-upload")

    async def chunks():
        yield b"abc"

    asyncio.run(files.receive("local", "owner-a", upload["upload_id"], chunks()))
    original = files._owner_root("local", "owner-a") / f"{upload['upload_id']}.blob"
    managed = source_owner / "managed-files"
    managed.mkdir()
    original.replace(managed / original.name)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO workspace_file_policies VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,owner_id) "
            "DO UPDATE SET storage_limit_bytes=excluded.storage_limit_bytes",
            ("local", "owner-a", 4096, None, None, None),
        )
        connection.execute("UPDATE workspace_file_storage_config SET quota_required=1")
    snapshot = tmp_path / "snapshot"
    result = continuity.capture_state(database, snapshot)
    assert result["managed_files"] == 1
    assert (snapshot / "data" / "owners" / digest / "managed-files" / original.name).read_bytes() == b"abc"
    target_data = tmp_path / "target-data"
    target_owner = target_data / "owners" / digest
    current_owner[0] = source_owner
    with pytest.raises(ValueError, match="target owner root differs"):
        continuity.prepare_restored_state(snapshot, tmp_path / "target", data_root=target_data,
                                          control_root=tmp_path / "target-control")
    current_owner[0] = target_owner
    continuity.prepare_restored_state(snapshot, tmp_path / "target", data_root=target_data,
                                      control_root=tmp_path / "target-control")
    rebound = Store(str(snapshot / "runtime.sqlite")).get_worker(worker["worker_id"])
    assert Path(rebound["workspace_dir"]).is_relative_to(target_owner)
    store.close()


def test_native_packaged_local_capture_maps_data_and_control_volumes(tmp_path, monkeypatch):
    from workers_projects_runtime import native_continuity as continuity
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    control = tmp_path / "package-control"
    data = tmp_path / "package-data"
    (control / "workspaces").mkdir(parents=True)
    data.mkdir()
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "local-linux")
    monkeypatch.setenv("XPERFECT_SHARED_VOLUME_ROOT", str(data))
    monkeypatch.setenv("XPERFECT_CONTROL_ROOT", str(control))
    monkeypatch.setattr(continuity, "_shared_process_absent", lambda box: True)
    database = control / "runtime.sqlite"
    store = Store(str(database))
    ControlPlaneStore(str(database))
    project = store.create_project("owner-a", "Package", "Keep work", "codex-cli")
    worker = store.create_worker(
        project["project_id"], "owner-a", "Writer", "Write", "codex-cli", "codex",
        "codex-cli", "configured-model", execution_mode="docker",
    )
    with store._connect() as connection:
        box = continuity._shared_box(connection, worker, data_root=data, control_root=control / "workspaces")
    paths = box.paths()
    paths["workspace_dir"].mkdir(parents=True)
    paths["state_dir"].mkdir(parents=True)
    (paths["workspace_dir"] / "report.txt").write_text("packaged work")
    store.update_worker(worker["worker_id"], state="paused", workspace_dir=str(paths["workspace_dir"]),
                        state_dir=str(paths["state_dir"]))
    files = WorkspaceFiles(store)
    upload = files.create_upload("local", "owner-a", draft_id="draft", name="source.txt",
                                 size_bytes=3, idempotency_key="packaged-file")

    async def chunks():
        yield b"abc"

    asyncio.run(files.receive("local", "owner-a", upload["upload_id"], chunks()))
    snapshot = tmp_path / "snapshot"
    result = continuity.capture_state(database, snapshot)
    assert result["workspace_files"] == 1 and result["managed_files"] == 1
    relative = Path(Store(str(snapshot / "runtime.sqlite")).get_worker(worker["worker_id"])["workspace_dir"])
    assert relative.parts[0] == "data"
    assert (snapshot / relative / "report.txt").read_text() == "packaged work"
    digest = __import__("hashlib").sha256(b"local\0owner-a").hexdigest()
    assert (snapshot / "data" / "managed-files" / digest / f"{upload['upload_id']}.blob").read_bytes() == b"abc"
    target_data, target_control = tmp_path / "target-data", tmp_path / "target-control"
    continuity.prepare_restored_state(snapshot, tmp_path / "target", data_root=target_data,
                                      control_root=target_control)
    rebound = Store(str(snapshot / "runtime.sqlite")).get_worker(worker["worker_id"])
    assert Path(rebound["workspace_dir"]).is_relative_to(target_data)
    assert Path(rebound["state_dir"]).is_relative_to(target_control)
    store.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_native_completed_file_projection_rebinds_without_old_inode_authority(tmp_path, legacy):
    from workers_projects_runtime.native_continuity import capture_state, check_quiescent
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    database, store, worker, _ = source_state(tmp_path)
    files = WorkspaceFiles(store)
    upload = files.create_upload("local", "owner-a", draft_id="draft", name="file.txt",
                                 size_bytes=3, idempotency_key="upload")

    async def chunks():
        yield b"abc"

    asyncio.run(files.receive("local", "owner-a", upload["upload_id"], chunks()))
    source = files._owner_root("local", "owner-a") / f"{upload['upload_id']}.blob"
    workspace = Path(store.get_worker(worker["worker_id"])["workspace_dir"])
    digest = __import__("hashlib").sha256(b"local\0owner-a").hexdigest()
    projection = "prj_" + "a" * 32
    metadata = workspace.stat()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO workspace_file_projection_targets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (projection, "local", "owner-a", str(source), str(workspace), "file.txt",
             metadata.st_dev, metadata.st_ino, upload["upload_id"], "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
             3, worker["worker_id"]),
        )
    with pytest.raises(ValueError, match="projection must settle"):
        check_quiescent(database)
    receipt = (source.parent / f"{projection}.receipt" if legacy else
               database.parent / "file-control" / digest / f"{projection}.receipt")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.touch(mode=0o600)
    if legacy:
        with store._connect() as connection:
            connection.execute("UPDATE workspace_file_storage_config SET receipt_migration_at=?",
                               (time.time() + 60,))
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    with sqlite3.connect(snapshot / "runtime.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_file_projection_targets").fetchone()[0] == 0
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_file_projection_targets").fetchone()[0] == 1
    store.close()


def test_native_files_trash_undo_requires_verified_target_binding(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    from workers_projects_runtime.workspace_file_continuity import bind_restored_deletes
    from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles

    database, source, worker, _ = source_state(tmp_path)
    files = WorkspaceFiles(source)
    entry = next(item for item in files.list_files(worker["worker_id"], "local", "owner-a")["items"]
                 if item["name"] == "report.md")
    receipt = files.mutate(worker["worker_id"], "local", "owner-a", entry["file_id"],
                           revision=entry["revision"], delete=True)
    snapshot = tmp_path / "snapshot"
    report = capture_state(database, snapshot)
    assert report["file_deletes_needing_target_binding"] == 1
    restored = Store(str(snapshot / "runtime.sqlite"))
    relative = Path(restored.get_worker(worker["worker_id"])["workspace_dir"])
    restored.close()
    destination = tmp_path / "destination"
    prepare_restored_state(snapshot, destination)
    restored = Store(str(snapshot / "runtime.sqlite"))
    (destination / relative).parent.mkdir(parents=True)
    shutil.copytree(snapshot / relative, destination / relative)
    target_files = WorkspaceFiles(restored)
    with pytest.raises(FileAdmissionError, match="Removed file changed"):
        target_files.undo(worker["worker_id"], "local", "owner-a", receipt["file_id"], receipt["undo_id"])
    assert bind_restored_deletes(restored) == 1
    assert bind_restored_deletes(restored) == 0
    assert target_files.undo(worker["worker_id"], "local", "owner-a", receipt["file_id"], receipt["undo_id"])["state"] == "restored"
    assert (destination / relative / "report.md").read_text() == "Useful finished work\n"
    restored.close()
    source.close()


@pytest.mark.parametrize("case", ["ordinary_empty", "deleted_empty", "deleted_nested_empty"])
def test_native_files_preserves_empty_folders_and_folder_undo(tmp_path, case):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    from workers_projects_runtime.workspace_file_continuity import bind_restored_deletes
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    database, source, worker, _ = source_state(tmp_path)
    files = WorkspaceFiles(source)
    files.mkdir(worker["worker_id"], "local", "owner-a", "Folder")
    root = Path(source.get_worker(worker["worker_id"])["workspace_dir"])
    if case == "deleted_nested_empty":
        files.mkdir(worker["worker_id"], "local", "owner-a", "Folder/Empty")
        (root / "Folder" / "content.txt").write_text("folder content")
    deleted = None
    if case != "ordinary_empty":
        entry = next(item for item in files.list_files(worker["worker_id"], "local", "owner-a")["items"]
                     if item["name"] == "Folder")
        deleted = files.mutate(worker["worker_id"], "local", "owner-a", entry["file_id"],
                               revision=entry["revision"], delete=True)
    snapshot = tmp_path / "snapshot"
    report = capture_state(database, snapshot)
    assert report["workspace_directories"] >= (3 if case == "deleted_nested_empty" else 1)
    restored = Store(str(snapshot / "runtime.sqlite"))
    relative = Path(restored.get_worker(worker["worker_id"])["workspace_dir"])
    if case == "ordinary_empty":
        assert (snapshot / relative / "Folder").is_dir()
        assert not (snapshot / relative / ".codex").exists()
    else:
        with restored._connect() as connection:
            target_path = connection.execute(
                "SELECT target_path FROM workspace_file_operations WHERE operation_id=?",
                (deleted["undo_id"],),
            ).fetchone()[0]
        trash = snapshot / relative / target_path
        assert trash.is_dir()
        if case == "deleted_nested_empty":
            assert (trash / "Empty").is_dir()
            assert (trash / "content.txt").read_text() == "folder content"
        destination = tmp_path / "destination"
        restored.close()
        prepare_restored_state(snapshot, destination)
        restored = Store(str(snapshot / "runtime.sqlite"))
        (destination / relative).parent.mkdir(parents=True)
        shutil.copytree(snapshot / relative, destination / relative)
        assert bind_restored_deletes(restored) == 1
        assert WorkspaceFiles(restored).undo(
            worker["worker_id"], "local", "owner-a", deleted["file_id"], deleted["undo_id"]
        )["state"] == "restored"
        assert (destination / relative / "Folder").is_dir()
        if case == "deleted_nested_empty":
            assert (destination / relative / "Folder" / "Empty").is_dir()
    restored.close()
    source.close()


def test_native_capture_rejects_pending_projection(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, store, worker, account = source_state(tmp_path)
    now = time.time()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO provider_account_leases(lease_id,account_id,tenant_id,owner_id,lane,worker_id,run_id,acquired_at,heartbeat_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("lease-pending", account["account_id"], "local", "owner-a", "native", worker["worker_id"], "run-pending", now, now, now + 60),
        )
        connection.execute(
            "INSERT INTO provider_account_projections(lease_id,account_id,tenant_id,owner_id,worker_id,binding_json,metadata_json,state,recovery_token,recovery_expires_at,receipt_hash,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("lease-pending", account["account_id"], "local", "owner-a", worker["worker_id"], '{"host_path":"/private/secret"}', '{"token":"secret"}', "pending", "recovery-secret", now + 60, "", now, now),
        )
    with pytest.raises(ValueError, match="unfinished provider projection"):
        capture_state(database, tmp_path / "pending-snapshot")
    assert not (tmp_path / "pending-snapshot").exists()


def test_native_capture_clears_completed_projection_recovery_material(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, store, worker, account = source_state(tmp_path)
    now = time.time()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO provider_account_leases(lease_id,account_id,tenant_id,owner_id,lane,worker_id,run_id,acquired_at,heartbeat_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("lease-complete", account["account_id"], "local", "owner-a", "native", worker["worker_id"], "run-complete", now, now, now + 60),
        )
        connection.execute(
            "INSERT INTO provider_account_projections(lease_id,account_id,tenant_id,owner_id,worker_id,binding_json,metadata_json,state,recovery_token,recovery_expires_at,receipt_hash,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("lease-complete", account["account_id"], "local", "owner-a", worker["worker_id"], '{"host_path":"/private/secret"}', '{"token":"secret"}', "complete", "recovery-secret", now + 60, "receipt-1", now, now),
        )
    snapshot = tmp_path / "complete-snapshot"
    capture_state(database, snapshot)
    with sqlite3.connect(snapshot / "runtime.sqlite") as connection:
        row = connection.execute(
            "SELECT binding_json,metadata_json,recovery_token,recovery_expires_at,receipt_hash FROM provider_account_projections WHERE lease_id='lease-complete'"
        ).fetchone()
    assert row[2:4] == ("", None)
    assert row[4] == "receipt-1"
    assert "host_path" not in row[0] and "token" not in row[1]


def test_native_capture_does_not_confuse_api_shutdown_with_worker_quiescence(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, store, worker, _ = source_state(tmp_path)
    store.update_worker(worker["worker_id"], state="running")
    with pytest.raises(ValueError, match="worker compute must be quiesced"):
        capture_state(database, tmp_path / "snapshot")
    assert store.get_worker(worker["worker_id"])["state"] == "running"


def test_native_quiescence_is_read_only_and_uses_actual_session_state(tmp_path):
    from workers_projects_runtime.native_continuity import check_quiescent
    database, store, worker, _ = source_state(tmp_path)
    before = {str(path.relative_to(database.parent)): path.read_bytes() if path.is_file() else None for path in database.parent.rglob("*") if not path.name.endswith("-shm")}
    check_quiescent(database)
    # SQLite read locks update transient shared-memory read marks, not durable state.
    after = {str(path.relative_to(database.parent)): path.read_bytes() if path.is_file() else None for path in database.parent.rglob("*") if not path.name.endswith("-shm")}
    assert after == before
    state = database.parent / "host_codex_cli_runtime/workers" / worker["worker_id"] / "state"
    state.mkdir(parents=True)
    (state / "active_terminal_session.json").write_text("malformed")
    with pytest.raises(ValueError, match="unreadable"):
        check_quiescent(database)


@pytest.mark.parametrize("mutation", [
    "CREATE TABLE unknown_credentials (secret TEXT)",
    "CREATE TABLE sqliteXcredentials (secret TEXT)",
    "ALTER TABLE workers ADD COLUMN unknown_secret TEXT",
    "ALTER TABLE workers ADD COLUMN unknown_secret TEXT GENERATED ALWAYS AS ('synthetic') VIRTUAL",
])
def test_native_capture_rejects_unknown_shape_at_supported_ledger(tmp_path, mutation):
    from workers_projects_runtime.native_continuity import capture_state
    database, _, _, _ = source_state(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
    with pytest.raises(ValueError, match="unreviewed schema shape"):
        capture_state(database, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize("state", ["queued", "claimed", "admitted", "running", "settling", "paused", "needs_input"])
def test_native_continuity_rejects_unsettled_runs_even_when_api_and_worker_are_paused(tmp_path, state):
    from workers_projects_runtime.native_continuity import capture_state, check_quiescent
    database, store, worker, _ = source_state(tmp_path)
    run = store.create_run(worker["worker_id"], worker["project_id"], "Finish the work")
    store.update_run(run["run_id"], state=state)
    with pytest.raises(ValueError, match="active work must be quiesced"):
        capture_state(database, tmp_path / "snapshot")
    with pytest.raises(ValueError, match="active work must be quiesced"):
        check_quiescent(database)


def test_native_capture_rejects_missing_durable_workspace(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state
    database, _, _, _ = source_state(tmp_path)
    shutil.rmtree(database.parent / "workspace")
    with pytest.raises(ValueError, match="recorded workspace is missing"):
        capture_state(database, tmp_path / "snapshot")


def test_native_capture_preserves_task_constraints_and_user_disconnect_without_browser_state(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state
    database, store, worker, account = source_state(tmp_path)
    bundle = {"project_definition": "Finish the report", "developer_instructions": "Keep uncertainties visible", "agents_md": "Use the complete source", "viventium_constraint_source": {"version": 1, "instruction": "Do not send anything"}, "env": {"WPR_CLAUDE_CODE_EFFORT": "max", "API_KEY": "never-export"}, "callbacks": {"hmac_secret": "never-export"}}
    store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps(bundle))
    browser = database.parent / "workspace/browser-profile/Default/Local Storage/leveldb"
    browser.mkdir(parents=True)
    (browser / "000003.log").write_text("never-export")
    with store._connect() as connection:
        connection.execute("UPDATE provider_accounts SET status='disconnected', is_default=0 WHERE account_id=?", (account["account_id"],))
    target = tmp_path / "snapshot"
    capture_state(database, target)
    restored = Store(str(target / "runtime.sqlite"))
    saved = json.loads(restored.get_worker(worker["worker_id"])["bootstrap_bundle_json"])
    assert saved["developer_instructions"] == bundle["developer_instructions"]
    assert saved["agents_md"] == bundle["agents_md"]
    assert saved["viventium_constraint_source"] == bundle["viventium_constraint_source"]
    assert saved["env"] == {"WPR_CLAUDE_CODE_EFFORT": "max"}
    assert not any(b"never-export" in path.read_bytes() for path in target.rglob("*") if path.is_file())
    with restored._connect() as connection:
        assert connection.execute("SELECT status, is_default FROM provider_accounts WHERE account_id=?", (account["account_id"],)).fetchone()[:] == ("disconnected", 0)


def callback_history(tmp_path, *, delegated=True):
    database, store, worker, _ = source_state(tmp_path)
    if delegated:
        record = store.reserve_delegation(tenant_id="local", owner_id="owner-a", idempotency_key="native-portable-history",
            request_digest="synthetic-history", origin_ref="origin_native_history", title="Keep the report",
            goal="Finish the report with uncertainty intact", instruction="Keep the table and do not send anything",
            origin_surface="web", worker_name="Writer", worker_role="writer", profile="codex-cli",
            backend="codex-cli", runtime="codex-cli", model="configured-model", execution_mode="host", bootstrap_bundle={})
        worker = store.get_worker(record["worker_id"])
        store.update_worker(worker["worker_id"], state="paused")
        run_id = record["current_run_id"]
    else:
        run_id = store.create_run(worker["worker_id"], worker["project_id"], "Keep the table and do not send anything")["run_id"]
    callback = store.insert_callback_outbox_once(callback_id="callback-test", project_id=worker["project_id"],
        worker_id=worker["worker_id"], run_id=run_id, attempt_number=None, event_type="run.queued",
        url="https://callback.example.invalid/events", payload_json='{"callback_ts":1700000001,"result":"The report is ready; uncertainty remains"}')
    claimed = store.claim_pending_callback(callback["callback_id"])
    assert claimed
    token = claimed["delivery_lease_token"]
    store.mark_callback_pending(callback["callback_id"], lease_token=token,
        delivery_generation=claimed["delivery_generation"], attempts=1, payload_json=claimed["payload_json"], last_error="Retry after reconnect")
    store.add_event(worker["project_id"], worker["worker_id"], run_id, "callback.diagnostic", "Delivery attempt retained",
        payload={"receipt": {"deliveryLeaseToken": token}, "encodedReceipt": json.dumps({"lease": token}), "result": "Keep the report"})
    store.update_run(run_id, state="completed", output_text="The report is ready; uncertainty remains")
    return database, store, worker, run_id, token


@pytest.mark.parametrize("delegated", [True, False])
def test_native_capture_projects_callback_history_without_losing_meaning(tmp_path, delegated):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, store, worker, run_id, token = callback_history(tmp_path, delegated=delegated)
    with store._connect() as connection:
        original = "\n".join(connection.iterdump())
        identities = connection.execute("SELECT callback_trace_event_id, callback_id, run_sequence, status FROM callback_trace_events ORDER BY run_sequence").fetchall()
    before = store.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a") if delegated else None
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    assert not any(token.encode() in p.read_bytes() for p in snapshot.rglob("*") if p.is_file())
    with store._connect() as connection:
        assert "\n".join(connection.iterdump()) == original
    restored_root = tmp_path / "restored"
    prepare_restored_state(snapshot, restored_root)
    restored = Store(str(snapshot / "runtime.sqlite"))
    with restored._connect() as connection:
        rows = connection.execute("SELECT callback_trace_event_id, callback_id, run_sequence, status FROM callback_trace_events ORDER BY run_sequence").fetchall()
        assert [tuple(row) for row in rows[:len(identities)]] == [tuple(row) for row in identities]
        assert connection.execute("SELECT COUNT(*) FROM events WHERE event_type='callback.diagnostic'").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE callback_trace_events SET snapshot_json='{}'")
        if delegated:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute("DELETE FROM work_trace_events")
    assert restored.get_run(run_id)["output_text"] == "The report is ready; uncertainty remains"
    assert restored.claim_pending_callback("callback-test") is None
    assert restored.mark_callback_pending("callback-test", lease_token=token, delivery_generation=1,
        attempts=2, payload_json="{}", last_error="old authority") is None
    if delegated:
        after = restored.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a")
        assert after["traceability"]["continuityProjections"][0]["sourceWorkHeadSha256"] == before["traceability"]["integrity"]["headSha256"]
        assert after["traceability"]["promptLayers"] == before["traceability"]["promptLayers"]
        assert [row["status"] for row in after["callbackDeliveries"][:3]] == ["pending", "delivering", "pending"]
    # The copy is a valid later source, rather than a one-use exception to import validation.
    shutil.move(snapshot, restored_root)
    restored.close()
    second = tmp_path / "second-snapshot"
    capture_state(restored_root / "runtime.sqlite", second)
    assert not any(token.encode() in p.read_bytes() for p in second.rglob("*") if p.is_file())


@pytest.mark.parametrize("table", ["callback_trace_events", "work_trace_events"])
def test_native_capture_rejects_tampered_history_before_projection(tmp_path, table):
    from workers_projects_runtime.native_continuity import capture_state
    database, store, _, _, _ = callback_history(tmp_path)
    trigger = f"{table}_append_only_update"
    column = "snapshot_json" if table == "callback_trace_events" else "payload_json"
    with store._connect() as connection:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (trigger,)).fetchone()[0]
        connection.execute(f'DROP TRIGGER "{trigger}"')
        connection.execute(f'UPDATE "{table}" SET "{column}"=?', ('{"changed":"untrusted history"}',))
        connection.execute(sql)
        original = "\n".join(connection.iterdump())
    snapshot = tmp_path / "snapshot"
    with pytest.raises((RuntimeError, ValueError), match="integrity"):
        capture_state(database, snapshot)
    assert not snapshot.exists()
    with store._connect() as connection:
        assert "\n".join(connection.iterdump()) == original


@pytest.mark.parametrize("tamper", ["provenance", "provenance_unlinked", "token_copy", "callback"])
def test_native_import_rejects_tampered_portable_history(tmp_path, tamper):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, _, _, _, token = callback_history(tmp_path)
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    staged = Store(str(snapshot / "runtime.sqlite"))
    with staged._connect() as connection:
        if tamper == "provenance":
            connection.execute("UPDATE events SET payload_json=json_set(payload_json, '$.exportedCallbackHeadSha256', ?) WHERE event_type='continuity.projected'", ("sha256:" + "0" * 64,))
        elif tamper == "provenance_unlinked":
            connection.execute("DELETE FROM events WHERE event_type='continuity.projected'")
        elif tamper == "token_copy":
            connection.execute("UPDATE events SET payload_json=? WHERE event_type='callback.diagnostic'", (json.dumps({"nested": {"copy": token}}),))
        else:
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='callback_trace_events_append_only_update'").fetchone()[0]
            connection.execute("DROP TRIGGER callback_trace_events_append_only_update")
            connection.execute("UPDATE callback_trace_events SET authority_sha256=?", ("sha256:" + "0" * 64,))
            connection.execute(sql)
    staged.close()
    target = tmp_path / "must-not-exist"
    with pytest.raises((RuntimeError, ValueError), match="provenance|credential|integrity"):
        prepare_restored_state(snapshot, target)
    assert not target.exists()


@pytest.mark.parametrize("location", ["value", "key", "encoded_string"])
def test_native_capture_redacts_json_encoded_credential_copies(tmp_path, location):
    from workers_projects_runtime.native_continuity import capture_state
    database, store, _, _, token = callback_history(tmp_path)
    escaped = "".join("\\u%04x" % ord(char) for char in token)
    payload = ('{"' + escaped + '":"Keep the report"}' if location == "key" else
               '{"copy":"' + escaped + '","result":"Keep the report"}')
    if location == "encoded_string":
        payload = json.dumps({"encodedReceipt": payload})
    with store._connect() as connection:
        connection.execute("UPDATE events SET payload_json=? WHERE event_type='callback.diagnostic'", (payload,))
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    with sqlite3.connect(snapshot / "runtime.sqlite") as connection:
        exported = connection.execute("SELECT payload_json FROM events WHERE event_type='callback.diagnostic'").fetchone()[0]
    assert token not in str(json.loads(exported))
    if location == "encoded_string":
        assert token not in str(json.loads(json.loads(exported)["encodedReceipt"]))
    assert "Keep the report" in exported


@pytest.mark.parametrize("location", ["value", "key", "encoded_string", "workspace"])
def test_native_import_rejects_encoded_credential_copies(tmp_path, location):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, _, _, _, token = callback_history(tmp_path)
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    escaped = "".join("\\u%04x" % ord(char) for char in token)
    payload = ('{"' + escaped + '":"Keep the report"}' if location == "key" else
               '{"copy":"' + escaped + '","result":"Keep the report"}')
    if location == "encoded_string":
        payload = json.dumps({"encodedReceipt": payload})
    if location == "workspace":
        (snapshot / "receipt.json").write_text(payload)
    else:
        with sqlite3.connect(snapshot / "runtime.sqlite") as connection:
            connection.execute("UPDATE events SET payload_json=? WHERE event_type='callback.diagnostic'", (payload,))
    with pytest.raises(ValueError, match="credential"):
        prepare_restored_state(snapshot, tmp_path / "must-not-exist")


def test_native_reexport_preserves_prior_projection_after_new_callback_history(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, _, worker, run_id, token = callback_history(tmp_path)
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    restored_root = tmp_path / "restored"
    prepare_restored_state(snapshot, restored_root)
    shutil.move(snapshot, restored_root)
    restored = Store(str(restored_root / "runtime.sqlite"))
    before = restored.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a")
    prior_projections = before["traceability"]["continuityProjections"]
    callback = restored.insert_callback_outbox_once(callback_id="callback-after-restore", project_id=worker["project_id"],
        worker_id=worker["worker_id"], run_id=run_id, attempt_number=None, event_type="run.queued",
        url="https://callback.example.invalid/events", payload_json='{"callback_ts":1700000002,"result":"The preserved report is available"}')
    claimed = restored.claim_pending_callback(callback["callback_id"])
    new_token = claimed["delivery_lease_token"]
    restored.mark_callback_pending(callback["callback_id"], lease_token=new_token,
        delivery_generation=claimed["delivery_generation"], attempts=1,
        payload_json=claimed["payload_json"], last_error="Retry after reconnect")
    second = tmp_path / "second"
    capture_state(restored_root / "runtime.sqlite", second)
    prepare_restored_state(second, tmp_path / "second-restored")
    imported = Store(str(second / "runtime.sqlite"))
    after = imported.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a")
    assert after["traceability"]["continuityProjections"][:-1] == prior_projections
    assert len(after["traceability"]["continuityProjections"]) == 2
    assert after["traceability"]["promptLayers"] == before["traceability"]["promptLayers"]
    assert imported.get_run(run_id)["output_text"] == "The report is ready; uncertainty remains"
    assert all(credential.encode() not in path.read_bytes() for path in second.rglob("*") if path.is_file()
               for credential in (token, new_token))


def test_native_capture_rejects_unhandled_credential_copy_without_losing_source(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state
    database, source, _, run_id, token = callback_history(tmp_path)
    # Do not silently rewrite user task/result text to make a portability check pass.
    source.update_run(run_id, output_text=json.dumps({"diagnostic": token, "result": "Keep the report"}))
    with source._connect() as connection:
        before = "\n".join(connection.iterdump())
    output = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="credential"):
        capture_state(database, output)
    assert not output.exists()
    with source._connect() as connection:
        assert "\n".join(connection.iterdump()) == before


# -- databases from earlier releases: same definitions, older layout ----------

# Exactly as an earlier packaged release created it (read from a deployed package).
LEGACY_COORDINATOR_TURNS = """CREATE TABLE coordinator_turns (
 conversation_id TEXT NOT NULL REFERENCES coordinator_conversations(conversation_id),
 turn_id TEXT NOT NULL, message TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
 blocker TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '',
 response_json TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'interactive', created_at TEXT NOT NULL,
 restore_hold INTEGER NOT NULL DEFAULT 0, retry_after_at TEXT NOT NULL DEFAULT '', retry_attempts INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(conversation_id, turn_id)
)"""


def _owners(database):
    from workers_projects_runtime.coordinator import CoordinatorService
    from workers_projects_runtime.worker_configuration import WorkerConfiguration
    store = Store(str(database))
    ControlPlaneStore(str(database))
    CoordinatorService(store, object(), object())
    WorkerConfiguration(store, object())
    return store


def _owner_ddl(tmp_path, table):
    reference = tmp_path / "reference" / "runtime.sqlite"
    if not reference.exists():
        reference.parent.mkdir()
        _owners(reference).close()
    with sqlite3.connect(reference) as connection:
        return connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]


def _columns(database, table):
    with sqlite3.connect(database) as connection:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def legacy_state(tmp_path, *, coordinator_turns=LEGACY_COORDINATOR_TURNS, extra=()):
    """Tables created before a column existed, then extended in place by the owners' migrations."""
    import re
    database = tmp_path / "legacy" / "runtime.sqlite"
    database.parent.mkdir(mode=0o700)
    with sqlite3.connect(database) as connection:
        for table in ("projects", "workers"):
            current = _owner_ddl(tmp_path, table)
            older = re.sub(r"\s*origin_scope_json TEXT NOT NULL DEFAULT '\{\}',", "", current, count=1)
            assert older != current
            connection.execute(older)
        connection.execute(coordinator_turns)
        for statement in extra:
            connection.execute(statement)
    store = _owners(database)
    for table in ("projects", "workers", "coordinator_turns"):
        assert _columns(database, table) != _columns(tmp_path / "reference" / "runtime.sqlite", table)
        if coordinator_turns == LEGACY_COORDINATOR_TURNS or table != "coordinator_turns":
            assert sorted(_columns(database, table)) == sorted(_columns(tmp_path / "reference" / "runtime.sqlite", table))
    return database, store


def test_a_database_from_an_earlier_release_is_reviewed_idle_and_captured(tmp_path):
    from workers_projects_runtime.native_continuity import (
        _require_schema, capture_state, check_quiescent, prepare_restored_state)
    database, store = legacy_state(tmp_path)
    project = store.create_project("owner-a", "Kept across releases", "Finish the report", "codex-cli")
    with sqlite3.connect(database) as connection:
        _require_schema(connection)
    check_quiescent(database)
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    prepare_restored_state(snapshot, tmp_path / "restored")
    restored = Store(str(snapshot / "runtime.sqlite"))
    assert restored.get_project(project["project_id"])["title"] == "Kept across releases"
    store.close()


@pytest.mark.parametrize("change", ["default", "not null", "extra column", "check", "positional order"])
def test_an_earlier_layout_with_any_other_difference_stays_unreviewed(tmp_path, change):
    from workers_projects_runtime.native_continuity import _require_schema, _table_definitions
    turns, extra = LEGACY_COORDINATOR_TURNS, ()
    if change == "default":
        turns = turns.replace("retry_attempts INTEGER NOT NULL DEFAULT 0", "retry_attempts INTEGER NOT NULL DEFAULT 1")
    elif change == "not null":
        turns = turns.replace("message TEXT NOT NULL,", "message TEXT,")
    elif change == "extra column":
        turns = turns.replace("PRIMARY KEY(", "unreviewed TEXT, PRIMARY KEY(")
    elif change == "check":
        turns = turns.replace("created_at TEXT NOT NULL,", "created_at TEXT NOT NULL CHECK(1),")
    else:
        # A table the runtime writes by position must keep its exact column order.
        prefix, parts, suffix = _table_definitions(_owner_ddl(tmp_path, "workspace_file_pins"))
        columns = [part for part in parts if not part.startswith(("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"))]
        constraints = [part for part in parts if part not in columns]
        extra = (prefix + "(" + ", ".join([*reversed(columns), *constraints]) + ")" + suffix,)
    assert turns != LEGACY_COORDINATOR_TURNS or extra
    if change == "positional order":
        database = tmp_path / "legacy" / "runtime.sqlite"
        database.parent.mkdir(mode=0o700)
        with sqlite3.connect(database) as connection:
            connection.execute(extra[0])
        store = _owners(database)
    else:
        database, store = legacy_state(tmp_path, coordinator_turns=turns)
    with sqlite3.connect(database) as connection:
        with pytest.raises(ValueError, match="unreviewed schema shape"):
            _require_schema(connection)
    store.close()


def test_active_work_in_a_database_from_an_earlier_release_is_not_idle(tmp_path):
    from workers_projects_runtime.native_continuity import check_quiescent
    database, store = legacy_state(tmp_path)
    project = store.create_project("owner-a", "Busy", "Finish the report", "codex-cli")
    worker = store.create_worker(project["project_id"], "owner-a", "Writer", "Help", "codex-cli", "codex",
                                 "codex-cli", "configured-model", execution_mode="host")
    store.create_run(worker["worker_id"], project["project_id"], "Finish the work")
    with pytest.raises(ValueError, match="active work must be quiesced"):
        check_quiescent(database)
    store.close()


def _sql_literals(path):
    """Every SQL string in a module, with adjacent literals joined and f-string holes marked."""
    import ast
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr):
            yield "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)


def test_every_table_written_by_position_keeps_its_exact_column_order(tmp_path):
    import re
    from workers_projects_runtime import native_continuity
    reference = tmp_path / "reference" / "runtime.sqlite"
    _owner_ddl(tmp_path, "projects")
    with sqlite3.connect(reference) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    writers, dynamic = set(), set()
    pattern = re.compile(r'(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+"?([\w{}]+)"?\s*(?:VALUES|SELECT)\b', re.I)
    for path in Path(native_continuity.__file__).parent.glob("*.py"):
        for text in _sql_literals(path):
            for table in pattern.findall(text):
                (dynamic if "{" in table else writers).add(table)
    assert not dynamic  # a positional write to a computed table name could not be checked
    assert writers & tables <= native_continuity._POSITIONAL_TABLES
    assert native_continuity._POSITIONAL_TABLES <= tables


def test_table_definitions_split_only_at_top_level_and_refuse_comments():
    from workers_projects_runtime.native_continuity import _table_definitions
    assert _table_definitions("CREATE TABLE t (a TEXT DEFAULT 'x, y' , b INT CHECK(b IN (1, 2)),\n PRIMARY KEY(a) )") == (
        "CREATE TABLE t ", ("a TEXT DEFAULT 'x, y'", "b INT CHECK(b IN (1,2))", "PRIMARY KEY(a)"), "")
    assert _table_definitions("CREATE TABLE t (a TEXT, -- note\n b INT)") is None
    assert _table_definitions("CREATE TABLE t (a TEXT") is None


# -- the release about to inherit state reviews state an earlier release wrote --

def predecessor_state(tmp_path, *changes, prepare=None):
    """A database an earlier release left: current owners, minus a column added since."""
    database, store = legacy_state(tmp_path)
    if prepare:
        prepare(store)
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE user_preferences DROP COLUMN grok_model")
        for change in changes:
            connection.execute(change)
    return database


def _dump(database):
    with sqlite3.connect(database) as connection:
        return list(connection.iterdump())


def test_the_incoming_release_reviews_an_earlier_state_without_changing_it(tmp_path):
    import subprocess, sys
    from workers_projects_runtime.native_continuity import check_quiescent
    database = predecessor_state(tmp_path)
    before = _dump(database)
    with pytest.raises(ValueError, match="unreviewed schema shape"):
        check_quiescent(database)
    check_quiescent(database, incoming=True)
    assert _dump(database) == before  # reviewed through a private copy only
    command = [sys.executable, "-m", "workers_projects_runtime.native_continuity", "quiescent", str(database)]
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert subprocess.run([*command, "--incoming"], capture_output=True).returncode == 0
    assert _dump(database) == before


@pytest.mark.parametrize("change", [
    "ALTER TABLE workers ADD COLUMN unknown_secret TEXT",
    "CREATE TABLE unknown_credentials (secret TEXT)",
    "UPDATE glasshive_schema_versions SET version = 999 WHERE component = 'runtime_store'",
    "INSERT INTO glasshive_schema_versions (component, version) VALUES ('unknown_component', 1)",
])
def test_the_incoming_release_still_refuses_unknown_state(tmp_path, change):
    from workers_projects_runtime.native_continuity import check_quiescent
    database = predecessor_state(tmp_path, change)
    with pytest.raises(ValueError, match="schema"):
        check_quiescent(database, incoming=True)


def test_the_incoming_release_still_refuses_a_weakened_constraint(tmp_path):
    from workers_projects_runtime.native_continuity import check_quiescent
    database = predecessor_state(tmp_path)
    with sqlite3.connect(database) as connection:
        original = connection.execute("SELECT sql FROM sqlite_master WHERE name='execution_workspaces'").fetchone()[0]
        weakened = original.replace("CHECK(mode IN ('isolated', 'shared'))", "CHECK(1)")
        assert weakened != original
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql=? WHERE name='execution_workspaces'", (weakened,))
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version={version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")
    with pytest.raises(ValueError, match="unreviewed schema shape"):
        check_quiescent(database, incoming=True)


def test_the_incoming_release_reads_idle_from_the_live_state(tmp_path):
    from workers_projects_runtime.native_continuity import check_quiescent

    def busy(store):
        project = store.create_project("owner-a", "Busy", "Finish the report", "codex-cli")
        worker = store.create_worker(project["project_id"], "owner-a", "Writer", "Help", "codex-cli", "codex",
                                     "codex-cli", "configured-model", execution_mode="host")
        store.create_run(worker["worker_id"], project["project_id"], "Finish the work")
    database = predecessor_state(tmp_path, prepare=busy)
    with pytest.raises(ValueError, match="active work must be quiesced"):
        check_quiescent(database, incoming=True)


@pytest.mark.parametrize("change", [
    "DROP TRIGGER IF EXISTS {trigger}",
    "DROP INDEX idx_projects_tenant_owner",
    "ALTER TABLE workspace_file_policies DROP COLUMN max_batch_bytes",
])
def test_the_incoming_release_accepts_only_added_columns(tmp_path, change):
    from workers_projects_runtime.native_continuity import check_quiescent
    database = predecessor_state(tmp_path)
    with sqlite3.connect(database) as connection:
        trigger = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchone()[0]
        connection.execute(change.format(trigger=trigger))
    with pytest.raises(ValueError, match="unreviewed schema shape"):
        check_quiescent(database, incoming=True)


def test_the_incoming_release_refuses_run_states_it_does_not_know(tmp_path):
    from workers_projects_runtime.native_continuity import check_quiescent

    def unknown(store):
        project = store.create_project("owner-a", "Older work", "Finish the report", "codex-cli")
        worker = store.create_worker(project["project_id"], "owner-a", "Writer", "Help", "codex-cli", "codex",
                                     "codex-cli", "configured-model", execution_mode="host")
        run = store.create_run(worker["worker_id"], project["project_id"], "Finish the work")
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("UPDATE runs SET state='dispatching' WHERE run_id=?", (run["run_id"],))
    database = predecessor_state(tmp_path, prepare=unknown)
    with pytest.raises(ValueError, match="run state this release does not know"):
        check_quiescent(database, incoming=True)


def test_the_incoming_release_refuses_state_too_large_for_its_scratch_space(tmp_path, monkeypatch):
    import shutil as shell
    from workers_projects_runtime import native_continuity
    database = predecessor_state(tmp_path)
    monkeypatch.setattr(native_continuity.shutil, "disk_usage", lambda path: shell._ntuple_diskusage(1, 0, 1))
    with pytest.raises(ValueError, match="too large to review"):
        native_continuity.check_quiescent(database, incoming=True)


def test_an_added_column_named_like_a_constraint_keyword_is_still_a_column():
    from workers_projects_runtime.native_continuity import _TABLE_CONSTRAINT
    assert not _TABLE_CONSTRAINT.match("checkpoint_at TEXT NOT NULL DEFAULT ''")
    assert not _TABLE_CONSTRAINT.match("unique_key TEXT")
    assert _TABLE_CONSTRAINT.match("UNIQUE (tenant_id, owner_id)")
    assert _TABLE_CONSTRAINT.match("PRIMARY KEY(a)") and _TABLE_CONSTRAINT.match("CHECK(mode IN ('a'))")


def _unpublished_projection(tmp_path, monkeypatch):
    """An earlier release's failed attach: binding committed, target registered, nothing published.

    The earlier release registered the target before checking the source signature. That
    order is reproduced here by removing only the newer pre-registration check, on a
    multi-user runtime whose deployment had no source signing key.
    """
    import hashlib
    from workers_projects_runtime import workspace_file_storage
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    database, store, worker, _ = source_state(tmp_path)
    files = WorkspaceFiles(store)
    payload = bytes(range(256)) * 257
    upload = files.create_upload("local", "owner-a", draft_id="draft", name="large.bin",
                                 size_bytes=len(payload), idempotency_key="upload")

    async def chunks():
        yield payload

    asyncio.run(files.receive("local", "owner-a", upload["upload_id"], chunks()))
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(files.private_root))
    monkeypatch.delenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", raising=False)
    with monkeypatch.context() as earlier:
        earlier.setattr(workspace_file_storage, "authorize_projection_source", lambda entry, worker: None)
        with pytest.raises(Exception):
            files.bind(worker["worker_id"], "local", "owner-a", [upload["upload_id"]], "same-request")
    with store._connect() as connection:
        row = dict(connection.execute("SELECT * FROM workspace_file_projection_targets").fetchone())
    digest = hashlib.sha256(b"local\0owner-a").hexdigest()
    return {"database": database, "store": store, "files": files, "worker": worker, "upload": upload,
            "payload": payload, "row": row, "workspace": Path(row["workspace_path"]),
            "control": database.parent / "file-control" / digest, "owner_root": Path(row["source_path"]).parent}


def _targets(store):
    with store._connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM workspace_file_projection_targets")]


def test_an_unpublished_target_is_retired_and_the_same_attach_then_publishes_exact_bytes(tmp_path, monkeypatch):
    import hashlib
    from workers_projects_runtime.native_continuity import check_quiescent, reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    store, database, row = state["store"], state["database"], state["row"]
    assert _targets(store) == [row] and not list(state["workspace"].rglob("large.bin"))
    with pytest.raises(ValueError, match="projection must settle"):
        check_quiescent(database)
    with store._connect() as connection:
        bindings = [dict(item) for item in connection.execute("SELECT * FROM workspace_file_bindings")]
        pins = [tuple(item) for item in connection.execute("SELECT * FROM workspace_file_pins")]
    report = reconcile_unpublished_projections(database)
    assert report["unsettled"] == 1 and report["applied"] is False
    assert report["targets"][0]["projection_id"] == row["projection_id"] and _targets(store) == [row]
    report = reconcile_unpublished_projections(database, apply=True)
    assert report["applied"] is True and _targets(store) == []
    with store._connect() as connection:
        assert [dict(item) for item in connection.execute("SELECT * FROM workspace_file_bindings")] == bindings
        assert [tuple(item) for item in connection.execute("SELECT * FROM workspace_file_pins")] == pins
    assert Path(row["source_path"]).read_bytes() == state["payload"]
    check_quiescent(database)
    # Upgraded: the deployment now has its source key; the same request publishes.
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-source-key")
    result = state["files"].bind(state["worker"]["worker_id"], "local", "owner-a",
                                 [state["upload"]["upload_id"]], "same-request")
    assert result["state"] == "available"
    published = state["workspace"] / row["relative_path"]
    assert hashlib.sha256(published.read_bytes()).hexdigest() == hashlib.sha256(state["payload"]).hexdigest()
    assert (state["control"] / f"{row['projection_id']}.receipt").is_file()
    check_quiescent(database)
    assert reconcile_unpublished_projections(database, apply=True) == {"unsettled": 0, "applied": False,
                                                                         "targets": []}
    state["store"].close()


@pytest.mark.parametrize("evidence,message", [
    ("owner_staging", "staged copy"),
    ("workspace_staging", "staged copy"),
    ("destination", "destination exists"),
    ("legacy_receipt", "receipt exists"),
    ("symlinked_parent", "path is unsafe"),
    ("manifest_differs", "accepted owner manifest"),
    ("manifest_missing", "accepted owner manifest"),
])
def test_anything_that_may_have_published_refuses_and_changes_nothing(tmp_path, monkeypatch, evidence, message):
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    row, workspace = state["row"], state["workspace"]
    staged = f".xperfect-transfer-{row['projection_id']}"
    destination = workspace / row["relative_path"]
    if evidence == "owner_staging":
        (state["owner_root"] / staged).write_bytes(b"part")
    elif evidence == "workspace_staging":
        (workspace / ".xperfect-file-staging").mkdir(exist_ok=True)
        (workspace / ".xperfect-file-staging" / staged).write_bytes(b"part")
    elif evidence == "destination":
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"partial")
    elif evidence == "legacy_receipt":
        (state["owner_root"] / f"{row['projection_id']}.receipt").touch(mode=0o600)
    elif evidence == "symlinked_parent":
        outside = tmp_path / "outside"
        outside.mkdir()
        top = Path(row["relative_path"]).parts[0]
        shutil.rmtree(workspace / top, ignore_errors=True)
        (workspace / top).symlink_to(outside, target_is_directory=True)
    else:
        with state["store"]._connect() as connection:
            if evidence == "manifest_missing":
                connection.execute("DELETE FROM workspace_file_bindings")
            else:
                (binding_id, manifest), = connection.execute(
                    "SELECT binding_id,manifest_json FROM workspace_file_bindings").fetchall()
                items = json.loads(manifest)
                items[0]["path"] = "elsewhere/large.bin"
                connection.execute("UPDATE workspace_file_bindings SET manifest_json=? WHERE binding_id=?",
                                   (json.dumps(items), binding_id))
    for apply in (False, True):
        with pytest.raises(ValueError, match=message):
            reconcile_unpublished_projections(state["database"], apply=apply)
    assert _targets(state["store"]) == [row]
    state["store"].close()


def test_a_projection_being_published_is_left_alone(tmp_path, monkeypatch):
    import fcntl
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    lock = state["control"] / f"{state['row']['projection_id']}.lock"
    with lock.open("a") as holder:  # the runtime holds this while it publishes
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="in use"):
            reconcile_unpublished_projections(state["database"], apply=True)
    assert _targets(state["store"]) == [state["row"]]
    state["store"].close()


def test_publication_that_starts_during_reconciliation_keeps_the_target(tmp_path, monkeypatch):
    from workers_projects_runtime import native_continuity
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    row = state["row"]
    lock = native_continuity._lock_projection

    def racing(target):
        descriptor = lock(target)
        # Between the first check and the lock, a publication staged its copy.
        (state["owner_root"] / f".xperfect-transfer-{row['projection_id']}").write_bytes(b"part")
        return descriptor
    monkeypatch.setattr(native_continuity, "_lock_projection", racing)
    with pytest.raises(ValueError, match="staged copy"):
        reconcile_unpublished_projections(state["database"], apply=True)
    assert _targets(state["store"]) == [row]
    state["store"].close()


def test_a_target_changed_after_it_was_checked_is_not_retired(tmp_path, monkeypatch):
    from workers_projects_runtime import native_continuity
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    lock = native_continuity._lock_projection

    def changing(target):
        descriptor = lock(target)
        with state["store"]._connect() as connection:
            connection.execute("UPDATE workspace_file_projection_targets SET scope_id='another-scope'")
        return descriptor
    monkeypatch.setattr(native_continuity, "_lock_projection", changing)
    with pytest.raises(ValueError, match="changed during reconciliation"):
        reconcile_unpublished_projections(state["database"], apply=True)
    assert [item["scope_id"] for item in _targets(state["store"])] == ["another-scope"]
    state["store"].close()


def test_one_target_that_may_be_published_keeps_every_target(tmp_path, monkeypatch):
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    row = dict(state["row"])
    second = {**row, "projection_id": "prj_" + "b" * 32, "relative_path": "inputs/second/large.bin"}
    with state["store"]._connect() as connection:
        connection.execute("INSERT INTO workspace_file_projection_targets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           tuple(second.values()))
        (binding_id, manifest), = connection.execute(
            "SELECT binding_id,manifest_json FROM workspace_file_bindings").fetchall()
        items = json.loads(manifest)
        items.append({**items[0], "projection_id": second["projection_id"], "path": second["relative_path"]})
        connection.execute("UPDATE workspace_file_bindings SET manifest_json=? WHERE binding_id=?",
                           (json.dumps(items), binding_id))
    destination = state["workspace"] / second["relative_path"]
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"published without its receipt")
    with pytest.raises(ValueError, match="destination exists"):
        reconcile_unpublished_projections(state["database"], apply=True)
    assert sorted(item["projection_id"] for item in _targets(state["store"])) == sorted(
        [row["projection_id"], second["projection_id"]])
    state["store"].close()


def test_a_runtime_that_can_authorize_sources_keeps_its_unsettled_targets(tmp_path, monkeypatch):
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    # With its key, a request already waiting for the projection lock could publish after a retire.
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-source-key")
    for apply in (False, True):
        with pytest.raises(ValueError, match="could still be published by this runtime"):
            reconcile_unpublished_projections(state["database"], apply=apply)
    assert _targets(state["store"]) == [state["row"]]
    state["store"].close()


def test_only_the_reviewed_set_is_retired(tmp_path, monkeypatch):
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    projection = state["row"]["projection_id"]
    with pytest.raises(ValueError, match="changed since they were reviewed"):
        reconcile_unpublished_projections(state["database"], apply=True, expect=[])
    with pytest.raises(ValueError, match="changed since they were reviewed"):
        reconcile_unpublished_projections(state["database"], apply=True, expect=[projection, "prj_" + "e" * 32])
    assert _targets(state["store"]) == [state["row"]]
    report = reconcile_unpublished_projections(state["database"], apply=True, expect=[projection])
    assert report["applied"] and _targets(state["store"]) == []
    state["store"].close()


@pytest.mark.parametrize("layout", ["packaged", "quota"])
def test_each_storage_layout_locks_and_checks_its_own_runtime_paths(tmp_path, monkeypatch, layout):
    import hashlib
    from types import SimpleNamespace
    from workers_projects_runtime import native_continuity
    from workers_projects_runtime.native_continuity import reconcile_unpublished_projections

    state = _unpublished_projection(tmp_path, monkeypatch)
    row, digest = state["row"], hashlib.sha256(b"local\0owner-a").hexdigest()
    if layout == "packaged":
        volume = tmp_path / "volume"
        monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "local-linux")
        monkeypatch.setenv("XPERFECT_SHARED_VOLUME_ROOT", str(volume))
        owner_root = volume / "managed-files" / digest
        control = state["database"].parent / "file-control" / digest
    else:
        quota_root = tmp_path / "owners" / digest
        registry = tmp_path / "control-storage" / "registry.json"
        (registry.parent / "runtime-storage" / "files").mkdir(parents=True)
        monkeypatch.setenv("XPERFECT_STORAGE_REGISTRY_PATH", str(registry))
        monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "5000000000")
        backend = SimpleNamespace(snapshot=lambda tenant, owner, limit: SimpleNamespace(root=str(quota_root)))
        monkeypatch.setattr(native_continuity, "_quota_backend", lambda: backend)
        owner_root = quota_root / "managed-files"
        control = registry.parent / "runtime-storage" / "files" / digest
        with state["store"]._connect() as connection:
            connection.execute("UPDATE workspace_file_storage_config SET quota_required=1")
    owner_root.mkdir(parents=True)
    source = owner_root / f"{row['upload_id']}.blob"
    with state["store"]._connect() as connection:
        connection.execute("UPDATE workspace_file_projection_targets SET source_path=?", (str(source),))
    staged = owner_root / f".xperfect-transfer-{row['projection_id']}"
    staged.write_bytes(b"part")
    with pytest.raises(ValueError, match="staged copy"):
        reconcile_unpublished_projections(state["database"], apply=True)
    staged.unlink()
    assert reconcile_unpublished_projections(state["database"], apply=True)["applied"]
    assert (control / f"{row['projection_id']}.lock").is_file() and _targets(state["store"]) == []
    state["store"].close()
