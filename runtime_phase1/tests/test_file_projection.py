import asyncio
import json
from pathlib import Path

import pytest

from workers_projects_runtime.bootstrap import (
    _write_project_files,
    refresh_project_runtime_files_for_worker,
    resolve_bootstrap_source_path,
)
from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles
from test_file_drafts import TENANT, OWNER, chunks, worker


def test_cold_warm_projection_preserves_nested_exact_inputs_and_later_edits(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store)
    workspace = tmp_path / "workspace"
    worker_id = worker(store, workspace)
    receipt = files.create_upload(
        TENANT,
        OWNER,
        draft_id="d",
        name="data.bin",
        relative_path="Folder/data.bin",
        size_bytes=4,
        idempotency_key="nested",
    )
    asyncio.run(files.receive(TENANT, OWNER, receipt["upload_id"], chunks(b"\x00one")))
    result = files.bind(worker_id, TENANT, OWNER, [receipt["upload_id"]], "bind")
    assert result["state"] == "accepted"
    assert files.storage(TENANT, OWNER)["reserved_bytes"] == 4
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    saved = store.get_worker(worker_id)
    bundle = json.loads(saved["bootstrap_bundle_json"])
    _write_project_files(home, workspace, bundle, saved, None, None)
    target = workspace / result["items"][0]["path"]
    assert target.read_bytes() == b"\x00one"
    assert not (workspace / ".xperfect-file-staging").exists()
    assert target.as_posix().endswith("/Folder/data.bin")
    assert files.storage(TENANT, OWNER)["reserved_bytes"] == 0
    target.write_bytes(b"changed by user")
    refresh_project_runtime_files_for_worker(home, workspace, saved)
    assert target.read_bytes() == b"changed by user"
    store.close()


def test_source_limit_unset_by_default_and_explicit_limit_is_enforced(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_MAX_BYTES", raising=False)
    source = tmp_path / "large.bin"
    with source.open("wb") as f:
        f.truncate(26 * 1024 * 1024)
    assert resolve_bootstrap_source_path(source) == source
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_MAX_BYTES", "10")
    with pytest.raises(PermissionError, match="size limit"):
        resolve_bootstrap_source_path(source)


def test_policy_lowering_preserves_files_and_does_not_authorize_increase(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "10")
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store, owner_roots=lambda *_: [])
    upload = files.create_upload(
        TENANT,
        OWNER,
        draft_id="d",
        name="file.txt",
        size_bytes=4,
        idempotency_key="one",
    )
    asyncio.run(files.receive(TENANT, OWNER, upload["upload_id"], chunks(b"data")))
    result = files.set_policy(TENANT, OWNER, {"storage_limit_bytes": 2})
    assert result["used_logical_bytes"] == 4 and result["available_bytes"] == 0
    assert files.manifest(TENANT, OWNER, [upload["upload_id"]])[0]["size_bytes"] == 4
    with pytest.raises(FileAdmissionError) as denied:
        files.set_policy(TENANT, OWNER, {"storage_limit_bytes": 11})
    assert denied.value.status_code == 403
    with pytest.raises(FileAdmissionError):
        files.create_upload(
            TENANT,
            OWNER,
            draft_id="d",
            name="next.txt",
            size_bytes=1,
            idempotency_key="two",
        )
    assert (
        files.set_policy(
            TENANT, OWNER, {"storage_limit_bytes": 11}, administrator=True
        )["limit_bytes"]
        == 11
    )
    assert (
        files.set_policy(TENANT, OWNER, {}, administrator=True, inherit=True)[
            "limit_bytes"
        ]
        == 10
    )
    store.close()


def test_download_reference_rejects_changed_revision(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker_id = worker(store, workspace)
    target = workspace / "report.txt"
    target.write_bytes(b"original")
    row = files.list_files(worker_id, TENANT, OWNER)["items"][0]
    descriptor, name, size = files.open_download(
        worker_id, TENANT, OWNER, row["file_id"], revision=row["revision"]
    )
    import os

    try:
        assert os.read(descriptor, size) == b"original"
        assert name == "report.txt"
    finally:
        os.close(descriptor)
    target.write_bytes(b"replaced")
    with pytest.raises(FileAdmissionError) as changed:
        files.open_download(
            worker_id, TENANT, OWNER, row["file_id"], revision=row["revision"]
        )
    assert changed.value.status_code == 409
    with pytest.raises(FileAdmissionError) as private:
        files.open_download(
            worker_id,
            TENANT,
            "different-owner",
            row["file_id"],
            revision=row["revision"],
        )
    assert private.value.status_code == 404
    assert files.list_files(worker_id, TENANT, OWNER)["drag_out_supported"] is False
    monkeypatch.setenv("GLASSHIVE_FILE_DRAG_OUT_TARGETS", "chromium_macos")
    assert files.list_files(worker_id, TENANT, OWNER)["drag_out_targets"] == [
        "chromium_macos"
    ]
    store.close()


def test_pending_failed_projection_reserves_its_name_and_keeps_both_contexts(
    tmp_path, monkeypatch
):
    import workers_projects_runtime.workspace_files as module

    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store)
    root = tmp_path / "workspace"
    root.mkdir()
    worker_id = worker(store, root)
    uploads = []
    for key, data in [("one", b"one"), ("two", b"two")]:
        upload = files.create_upload(
            TENANT,
            OWNER,
            draft_id="d",
            name="same.txt",
            size_bytes=3,
            idempotency_key=key,
        )
        asyncio.run(files.receive(TENANT, OWNER, upload["upload_id"], chunks(data)))
        uploads.append(upload["upload_id"])
    materialize = module.materialize_managed_file

    def interrupted(*args, **kwargs):
        raise FileAdmissionError("Synthetic publish interruption", 503)

    monkeypatch.setattr(module, "materialize_managed_file", interrupted)
    with pytest.raises(FileAdmissionError):
        files.bind(worker_id, TENANT, OWNER, [uploads[0]], "first", directory="")
    monkeypatch.setattr(module, "materialize_managed_file", materialize)
    second = files.bind(worker_id, TENANT, OWNER, [uploads[1]], "second", directory="")
    first = files.bind(worker_id, TENANT, OWNER, [uploads[0]], "first", directory="")
    assert first["items"][0]["path"] == "same.txt"
    assert second["items"][0]["path"] == "same-2.txt"
    assert (root / "same.txt").read_bytes() == b"one"
    assert (root / "same-2.txt").read_bytes() == b"two"
    saved = json.loads(store.get_worker(worker_id)["bootstrap_bundle_json"])
    assert {entry["managed_upload_id"] for entry in saved["files"]} == set(uploads)
    assert not (root / ".xperfect-files.lock").exists()
    assert (
        files.control_lock_root(store.get_worker(worker_id)).parent
        == tmp_path / "workspace-file-locks"
    )
    store.close()


@pytest.mark.parametrize(
    "name", ["x" * 251 + ".txt", "é" * 125 + "a.txt", "a." + "x" * 253]
)
def test_keep_both_supports_maximum_utf8_filename(tmp_path, monkeypatch, name):
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker_id = worker(store, workspace)
    paths = []
    for index, content in enumerate((b"first", b"second")):
        upload = files.create_upload(
            TENANT,
            OWNER,
            draft_id="d",
            name=name,
            size_bytes=len(content),
            idempotency_key=f"upload-{index}",
        )
        asyncio.run(files.receive(TENANT, OWNER, upload["upload_id"], chunks(content)))
        result = files.bind(
            worker_id,
            TENANT,
            OWNER,
            [upload["upload_id"]],
            f"bind-{index}",
            directory="",
        )
        path = workspace / result["items"][0]["path"]
        assert len(path.name.encode("utf-8")) <= 255
        assert path.read_bytes() == content
        paths.append(path)
    assert paths[0] != paths[1]
    assert paths[0].read_bytes() == b"first"
    store.close()
