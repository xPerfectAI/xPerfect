"""Real local filesystem evidence for journaled move, removal, and Undo."""

from pathlib import Path

import pytest

from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import (
    FileAdmissionError,
    WorkspaceFiles,
    logical_bytes,
)
from workers_projects_runtime import workspace_file_mutations as mutations


@pytest.fixture
def workspace(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner", "Files", "Keep exact files", "codex-cli", tenant_id="tenant"
    )
    worker = store.create_worker(
        project["project_id"],
        "owner",
        "Files",
        "operator",
        "codex-cli",
        "openclaw",
        "codex-cli",
        "stub/codex-cli",
        tenant_id="tenant",
    )
    root = tmp_path / "workspace"
    root.mkdir()
    store.update_worker(worker["worker_id"], workspace_dir=str(root))
    files = WorkspaceFiles(store)
    yield files, worker["worker_id"], root
    store.close()


def listing(workspace, directory=""):
    files, worker, _ = workspace
    return {
        row["name"]: row
        for row in files.list_files(worker, "tenant", "owner", directory=directory)[
            "items"
        ]
    }


def move(workspace, entry, path):
    files, worker, _ = workspace
    return files.mutate(
        worker,
        "tenant",
        "owner",
        entry["file_id"],
        revision=entry["revision"],
        path=path,
    )


def remove(workspace, entry):
    files, worker, _ = workspace
    return files.mutate(
        worker,
        "tenant",
        "owner",
        entry["file_id"],
        revision=entry["revision"],
        delete=True,
    )


def undo(workspace, receipt):
    files, worker, _ = workspace
    return files.undo(worker, "tenant", "owner", receipt["file_id"], receipt["undo_id"])


def test_file_move_keeps_id_content_and_has_no_copy(workspace):
    files, worker, root = workspace
    (root / "first.txt").write_bytes(b"exact bytes")
    (root / "folder").mkdir()
    entry = listing(workspace)["first.txt"]
    inode = (root / "first.txt").stat().st_ino
    move(workspace, entry, "folder/renamed.txt")
    assert not (root / "first.txt").exists()
    assert (root / "folder/renamed.txt").read_bytes() == b"exact bytes"
    assert (root / "folder/renamed.txt").stat().st_ino == inode
    renamed = listing(workspace, "folder")["renamed.txt"]
    assert renamed["file_id"] == entry["file_id"]
    assert logical_bytes([root]) == len(b"exact bytes")


def test_folder_move_updates_all_indexed_descendant_ids(workspace):
    _, _, root = workspace
    (root / "folder/nested/empty").mkdir(parents=True)
    (root / "folder/nested/file.txt").write_bytes(b"data")
    folder = listing(workspace)["folder"]
    nested = listing(workspace, "folder")["nested"]
    child = listing(workspace, "folder/nested")["file.txt"]
    move(workspace, folder, "renamed")
    assert listing(workspace)["renamed"]["file_id"] == folder["file_id"]
    assert listing(workspace, "renamed")["nested"]["file_id"] == nested["file_id"]
    assert (
        listing(workspace, "renamed/nested")["file.txt"]["file_id"] == child["file_id"]
    )
    assert (root / "renamed/nested/empty").is_dir()


@pytest.mark.parametrize("folder", [False, True])
def test_removal_survives_reload_and_undo_keeps_bytes_and_ids(workspace, folder):
    files, worker, root = workspace
    if folder:
        (root / "selected").mkdir()
        (root / "selected/child").write_bytes(b"keep me")
        child = listing(workspace, "selected")["child"]
    else:
        (root / "selected").write_bytes(b"keep me")
    entry = listing(workspace)["selected"]
    receipt = remove(workspace, entry)
    assert not (root / "selected").exists()
    assert listing(workspace) == {}
    assert logical_bytes([root]) == len(b"keep me")
    fresh = WorkspaceFiles(Store(files.store.db_path))
    try:
        trash = fresh.trash(worker, "tenant", "owner")["items"]
        assert len(trash) == 1
        assert trash[0]["undo_id"] == receipt["undo_id"]
        assert trash[0]["is_dir"] is folder
        result = fresh.undo(
            worker, "tenant", "owner", entry["file_id"], receipt["undo_id"]
        )
        assert result["state"] == "restored"
        assert (
            fresh.undo(worker, "tenant", "owner", entry["file_id"], receipt["undo_id"])
            == result
        )
        assert fresh.trash(worker, "tenant", "owner")["items"] == []
    finally:
        fresh.store.close()
    assert listing(workspace)["selected"]["file_id"] == entry["file_id"]
    if folder:
        assert listing(workspace, "selected")["child"]["file_id"] == child["file_id"]
        assert (root / "selected/child").read_bytes() == b"keep me"
    else:
        assert (root / "selected").read_bytes() == b"keep me"


def test_restore_conflict_preserves_both_files_and_allows_later_undo(workspace):
    _, _, root = workspace
    (root / "selected").write_bytes(b"old")
    entry = listing(workspace)["selected"]
    receipt = remove(workspace, entry)
    (root / "selected").write_bytes(b"new")
    replacement = listing(workspace)["selected"]
    assert replacement["file_id"] != entry["file_id"]
    with pytest.raises(FileAdmissionError, match="already exists"):
        undo(workspace, receipt)
    assert (root / "selected").read_bytes() == b"new"
    move(workspace, replacement, "replacement")
    undo(workspace, receipt)
    assert (root / "selected").read_bytes() == b"old"
    assert (root / "replacement").read_bytes() == b"new"


@pytest.mark.parametrize("operation", ["move", "delete", "restore"])
def test_crash_after_atomic_rename_recovers_ledger_on_reload(
    workspace, monkeypatch, operation
):
    files, worker, root = workspace
    (root / "source").write_bytes(b"durable")
    entry = listing(workspace)["source"]
    receipt = remove(workspace, entry) if operation == "restore" else None
    original = mutations.FileMutations._apply_entries

    def crash(*args):
        raise RuntimeError("synthetic process interruption after rename")

    monkeypatch.setattr(mutations.FileMutations, "_apply_entries", crash)
    with pytest.raises(RuntimeError, match="interruption"):
        if operation == "restore":
            undo(workspace, receipt)
        elif operation == "delete":
            remove(workspace, entry)
        else:
            move(workspace, entry, "target")
    monkeypatch.setattr(mutations.FileMutations, "_apply_entries", original)
    recovered = listing(workspace)
    if operation == "delete":
        assert recovered == {}
        trash = files.trash(worker, "tenant", "owner")["items"]
        undo(workspace, trash[0])
        assert (root / "source").read_bytes() == b"durable"
    else:
        name = "source" if operation == "restore" else "target"
        assert recovered[name]["file_id"] == entry["file_id"]
        assert (root / name).read_bytes() == b"durable"
    with files.store._connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workspace_file_operations WHERE state='prepared'"
            ).fetchone()[0]
            == 0
        )


def test_crash_before_rename_replays_same_prepared_operation(workspace, monkeypatch):
    _, _, root = workspace
    (root / "source").write_bytes(b"durable")
    entry = listing(workspace)["source"]
    original = mutations._rename_no_replace
    monkeypatch.setattr(
        mutations,
        "_rename_no_replace",
        lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic crash")),
    )
    with pytest.raises(RuntimeError, match="synthetic crash"):
        move(workspace, entry, "target")
    assert (root / "source").exists()
    monkeypatch.setattr(mutations, "_rename_no_replace", original)
    assert listing(workspace)["target"]["file_id"] == entry["file_id"]
    assert not (root / "source").exists()


def test_duplicate_name_race_never_overwrites_destination(workspace, monkeypatch):
    _, _, root = workspace
    (root / "source").write_bytes(b"source")
    entry = listing(workspace)["source"]
    original = mutations._rename_no_replace

    def race(*args):
        (root / "target").write_bytes(b"other writer")
        original(*args)

    monkeypatch.setattr(mutations, "_rename_no_replace", race)
    with pytest.raises(FileAdmissionError, match="already exists"):
        move(workspace, entry, "target")
    assert (root / "source").read_bytes() == b"source"
    assert (root / "target").read_bytes() == b"other writer"


@pytest.mark.parametrize("folder", [False, True])
def test_stale_revision_rejects_file_or_nested_folder_change(workspace, folder):
    _, _, root = workspace
    if folder:
        (root / "source/nested").mkdir(parents=True)
        target = root / "source/nested/file"
    else:
        target = root / "source"
    target.write_bytes(b"before")
    entry = listing(workspace)["source"]
    target.write_bytes(b"after")
    with pytest.raises(FileAdmissionError, match="changed"):
        remove(workspace, entry)
    assert target.read_bytes() == b"after"


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "private"])
def test_folder_mutation_rejects_unsafe_descendants(workspace, unsafe):
    _, _, root = workspace
    (root / "folder").mkdir()
    (root / "outside").write_bytes(b"private")
    if unsafe == "symlink":
        (root / "folder/linked").symlink_to(root / "outside")
    elif unsafe == "hardlink":
        import os

        os.link(root / "outside", root / "folder/linked")
    else:
        (root / "folder/.git").mkdir()
    entry = listing(workspace)["folder"]
    with pytest.raises(FileAdmissionError):
        remove(workspace, entry)
    assert (root / "outside").read_bytes() == b"private"
    assert (root / "folder").exists()


def test_undo_is_owner_scoped_and_trash_cannot_be_addressed_as_files(workspace):
    files, worker, root = workspace
    (root / "source").write_bytes(b"private")
    entry = listing(workspace)["source"]
    receipt = remove(workspace, entry)
    with pytest.raises(FileAdmissionError, match="not found"):
        files.undo(
            worker, "tenant", "another-owner", entry["file_id"], receipt["undo_id"]
        )
    with pytest.raises(FileAdmissionError, match="private"):
        files.list_files(worker, "tenant", "owner", directory=mutations.TRASH)


def test_unsupported_exclusive_rename_is_truthful_and_preserves_source(
    workspace, monkeypatch
):
    _, _, root = workspace
    (root / "source").write_bytes(b"safe")
    entry = listing(workspace)["source"]
    monkeypatch.setattr(mutations.ctypes, "CDLL", lambda *args, **kwargs: object())
    with pytest.raises(FileAdmissionError) as error:
        move(workspace, entry, "target")
    assert error.value.status_code == 503
    assert (root / "source").read_bytes() == b"safe"
    assert not (root / "target").exists()


def test_active_run_blocks_mutations_and_keeps_files(workspace):
    files, worker, root = workspace
    (root / "source").write_bytes(b"busy")
    entry = listing(workspace)["source"]
    info = files.store.get_worker(worker)
    run = files.store.create_run(worker, info["project_id"], "Native work")
    claimed = files.store.claim_next_queued_run(
        worker, executor_id="synthetic-file-test"
    )
    assert claimed and claimed["run_id"] == run["run_id"]
    assert files.store.get_active_run(worker)
    with pytest.raises(FileAdmissionError, match="editing"):
        remove(workspace, entry)
    assert (root / "source").read_bytes() == b"busy"


def test_folder_cannot_move_into_itself(workspace):
    _, _, root = workspace
    (root / "folder/child").mkdir(parents=True)
    entry = listing(workspace)["folder"]
    with pytest.raises(FileAdmissionError, match="inside itself"):
        move(workspace, entry, "folder/child/moved")
    assert (root / "folder/child").is_dir()


def test_restore_api_and_read_only_authority(workspace):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from workers_projects_runtime.workspace_file_api import (
        install_workspace_file_routes,
    )

    files, worker, root = workspace
    (root / "source").write_bytes(b"safe")
    entry = listing(workspace)["source"]
    receipt = remove(workspace, entry)
    context = SimpleNamespace(
        auth_mode="local",
        role="viewer",
        tenant_id="tenant",
        owner_id="owner",
        enterprise=False,
    )
    app = FastAPI()
    install_workspace_file_routes(
        app, files, lambda request: context, lambda worker_id, request: None
    )
    client = TestClient(app)
    url = f"/v1/workers/{worker}/files/{entry['file_id']}/restore"
    assert client.post(url, json={"undo_id": receipt["undo_id"]}).status_code == 403
    assert not (root / "source").exists()
    context.role = "owner"
    trash = client.get(f"/v1/workers/{worker}/files/trash")
    assert trash.status_code == 200
    assert trash.json()["items"][0]["undo_id"] == receipt["undo_id"]
    result = client.post(url, json={"undo_id": receipt["undo_id"]})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "restored"
    assert (root / "source").read_bytes() == b"safe"
