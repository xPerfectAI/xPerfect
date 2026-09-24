"""Acceptance checks for managed file drafts and retained workspace copies."""

import asyncio
import hashlib
import os
import time
from pathlib import Path

import pytest

from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles


TENANT = "tenant-test"
OWNER = "owner-test"


async def chunks(*parts):
    for part in parts:
        yield part


def draft(files, *, name="brief.txt", size=3, request_key="upload-one", owner=OWNER):
    return files.create_upload(
        TENANT,
        owner,
        draft_id="draft-one",
        name=name,
        size_bytes=size,
        idempotency_key=request_key,
    )


def worker(store, workspace: Path, *, owner=OWNER):
    project = store.create_project(
        owner, "Files", "Keep exact files", "codex-cli", tenant_id=TENANT
    )
    created = store.create_worker(
        project["project_id"],
        owner,
        "Worker",
        "main",
        "codex-cli",
        "stub",
        "stub",
        "stub",
        tenant_id=TENANT,
    )
    store.update_worker(created["worker_id"], workspace_dir=str(workspace))
    return created["worker_id"]


def test_local_unlimited_and_hosted_five_gb_defaults(tmp_path, monkeypatch):
    for name in (
        "GLASSHIVE_OWNER_STORAGE_BYTES",
        "GLASSHIVE_FILE_MAX_BYTES",
        "GLASSHIVE_UI_UPLOAD_MAX_FILES",
        "GLASSHIVE_UI_UPLOAD_MAX_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store, owner_roots=lambda *_: [])
    try:
        monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
        local = files.storage(TENANT, OWNER)
        assert local["limit_bytes"] is None
        assert local["available_bytes"] is None
        assert local["native_hard_enforcement"] is False
        assert all(
            local[key] is None
            for key in ("max_file_bytes", "max_batch_files", "max_batch_bytes")
        )

        monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
        hosted = files.storage(TENANT, "other-owner")
        assert hosted["limit_bytes"] == 5_000_000_000
        assert hosted["available_bytes"] == 5_000_000_000
        assert hosted["native_hard_enforcement"] is False
    finally:
        store.close()


def test_owner_isolation_and_pending_reservation_survive_second_store(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "5")
    path = str(tmp_path / "state.db")
    first_store = Store(path)
    second_store = Store(path)
    first = WorkspaceFiles(first_store, owner_roots=lambda *_: [])
    second = WorkspaceFiles(second_store, owner_roots=lambda *_: [])
    try:
        accepted = draft(first, size=4)
        assert second.storage(TENANT, OWNER)["reserved_bytes"] == 4
        assert second.storage(TENANT, OWNER)["available_bytes"] == 1
        with pytest.raises(FileAdmissionError) as insufficient:
            second.create_upload(
                TENANT,
                OWNER,
                draft_id="draft-two",
                name="more.txt",
                size_bytes=2,
                idempotency_key="upload-two",
            )
        assert insufficient.value.status_code == 413
        assert second.list_uploads(TENANT, "other-owner", "draft-one")["items"] == []
        with pytest.raises(FileAdmissionError) as private:
            second.manifest(TENANT, "other-owner", [accepted["upload_id"]])
        assert private.value.status_code == 404
        assert (
            draft(second, owner="other-owner", size=4)["upload_id"]
            != accepted["upload_id"]
        )
    finally:
        second_store.close()
        first_store.close()


def test_failed_stream_retries_same_upload_id_with_exact_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store, owner_roots=lambda *_: [])
    try:
        receipt = draft(files, size=4)
        upload_id = receipt["upload_id"]

        async def interrupted():
            yield b"ab"
            raise RuntimeError("connection lost")

        with pytest.raises(RuntimeError, match="connection lost"):
            asyncio.run(files.receive(TENANT, OWNER, upload_id, interrupted()))
        assert (
            files.list_uploads(TENANT, OWNER, "draft-one")["items"][0]["state"]
            == "failed"
        )
        assert (
            files.create_upload(
                TENANT,
                OWNER,
                draft_id="draft-one",
                name="brief.txt",
                size_bytes=4,
                idempotency_key="upload-one",
            )["upload_id"]
            == upload_id
        )

        ready = asyncio.run(
            files.receive(TENANT, OWNER, upload_id, chunks(b"ab", b"cd"))
        )
        assert ready["state"] == "ready"
        assert ready["received_bytes"] == 4
        assert ready["sha256"] == hashlib.sha256(b"abcd").hexdigest()
        assert (
            files._owner_root(TENANT, OWNER) / f"{upload_id}.blob"
        ).read_bytes() == b"abcd"
        assert (
            files.manifest(TENANT, OWNER, [upload_id])[0]["sha256"] == ready["sha256"]
        )
    finally:
        store.close()


def test_cancel_active_stream_then_reload_and_expire_unpinned_upload(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    path = str(tmp_path / "state.db")
    store = Store(path)
    files = WorkspaceFiles(store, owner_roots=lambda *_: [])
    try:
        upload_id = draft(files, size=4)["upload_id"]

        async def scenario():
            first_chunk = asyncio.Event()
            continue_stream = asyncio.Event()

            async def active():
                first_chunk.set()
                yield b"ab"
                await continue_stream.wait()
                yield b"cd"

            task = asyncio.create_task(
                files.receive(TENANT, OWNER, upload_id, active())
            )
            await first_chunk.wait()
            assert files.cancel(TENANT, OWNER, upload_id)["state"] == "cancelling"
            continue_stream.set()
            with pytest.raises(FileAdmissionError):
                await task

        asyncio.run(scenario())
        assert (
            files.list_uploads(TENANT, OWNER, "draft-one")["items"][0]["state"]
            == "cancelled"
        )
        assert not (files._owner_root(TENANT, OWNER) / f"{upload_id}.part").exists()

        expiring_id = files.create_upload(
            TENANT,
            OWNER,
            draft_id="draft-two",
            name="old.txt",
            size_bytes=3,
            idempotency_key="old",
        )["upload_id"]
        asyncio.run(files.receive(TENANT, OWNER, expiring_id, chunks(b"old")))
        with store._connect() as conn:
            conn.execute(
                "UPDATE workspace_file_uploads SET expires_at=?, lease_until=0 WHERE upload_id=?",
                (time.time() - 1, expiring_id),
            )
        reloaded_store = Store(path)
        try:
            reloaded = WorkspaceFiles(reloaded_store, owner_roots=lambda *_: [])
            reloaded.expire(TENANT, OWNER)
            old = reloaded.list_uploads(TENANT, OWNER, "draft-two")["items"][0]
            assert old["state"] == "expired"
            assert not (
                reloaded._owner_root(TENANT, OWNER) / f"{expiring_id}.blob"
            ).exists()
        finally:
            reloaded_store.close()
    finally:
        store.close()


def test_zero_byte_file_has_real_ready_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store, owner_roots=lambda *_: [])
    try:
        upload_id = draft(files, name="empty.txt", size=0)["upload_id"]
        ready = asyncio.run(files.receive(TENANT, OWNER, upload_id, chunks()))
        assert ready["state"] == "ready"
        assert ready["received_bytes"] == 0
        assert ready["sha256"] == hashlib.sha256(b"").hexdigest()
        assert (
            files._owner_root(TENANT, OWNER) / f"{upload_id}.blob"
        ).read_bytes() == b""
    finally:
        store.close()


def test_binding_counts_copy_once_and_receipt_prevents_resurrection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "6")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store)
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", os.fspath(files.private_root))
    try:
        worker_id = worker(store, workspace)
        upload_id = draft(files)["upload_id"]
        asyncio.run(files.receive(TENANT, OWNER, upload_id, chunks(b"abc")))
        assert files.storage(TENANT, OWNER)["used_logical_bytes"] == 3
        bound = files.bind(
            worker_id, TENANT, OWNER, [upload_id], "bind-one", directory=""
        )
        path = workspace / bound["items"][0]["path"]
        assert path.read_bytes() == b"abc"
        assert files.storage(TENANT, OWNER)["used_logical_bytes"] == 6
        assert files.storage(TENANT, OWNER)["reserved_bytes"] == 0
        with store._connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM workspace_file_bindings").fetchone()[
                    0
                ]
                == 1
            )
        files.bind(worker_id, TENANT, OWNER, [upload_id], "bind-one", directory="")
        assert files.storage(TENANT, OWNER)["used_logical_bytes"] == 6
        with store._connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM workspace_file_bindings").fetchone()[
                    0
                ]
                == 1
            )

        listed = files.list_files(worker_id, TENANT, OWNER)["items"]
        assert len(listed) == 1
        files.mutate(
            worker_id,
            TENANT,
            OWNER,
            listed[0]["file_id"],
            revision=listed[0]["revision"],
            delete=True,
        )
        assert not path.exists()
        files.bind(worker_id, TENANT, OWNER, [upload_id], "bind-one", directory="")
        assert not path.exists()
        # Removal retains the workspace copy in private Trash for Undo.
        assert files.storage(TENANT, OWNER)["used_logical_bytes"] == 6
    finally:
        store.close()


def test_symlink_workspace_and_child_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(outside, target_is_directory=True)
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store)
    try:
        worker_id = worker(store, workspace_link)
        with pytest.raises(FileAdmissionError):
            files.list_files(worker_id, TENANT, OWNER)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store.update_worker(worker_id, workspace_dir=str(workspace))
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
        with pytest.raises(FileAdmissionError):
            files.mkdir(worker_id, TENANT, OWNER, "linked/new")
        assert not (outside / "new").exists()
    finally:
        store.close()


@pytest.mark.parametrize("error_name", ["ENOSPC", "EDQUOT"])
def test_upload_disk_exhaustion_releases_reservation_and_can_retry(
    tmp_path, monkeypatch, error_name
):
    import errno
    import os

    # Native XFS commonly returns ENOSPC; other quota paths can return EDQUOT.
    error_number = getattr(errno, error_name)
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store)
    upload = files.create_upload(
        TENANT,
        OWNER,
        draft_id="d",
        name="input.txt",
        size_bytes=4,
        idempotency_key="input",
    )
    sync = os.fsync

    def full(_fd):
        raise OSError(error_number, "synthetic quota boundary")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(FileAdmissionError, match="Storage allocation failed") as caught:
        asyncio.run(files.receive(TENANT, OWNER, upload["upload_id"], chunks(b"data")))
    assert caught.value.status_code == 507
    stored = files.list_uploads(TENANT, OWNER, "d")["items"][0]
    assert stored["state"] == "failed"
    assert "Storage allocation failed" in stored["error"]
    assert files.storage(TENANT, OWNER)["reserved_bytes"] == 0
    assert not list(files._owner_root(TENANT, OWNER).glob("*.part"))
    monkeypatch.setattr(os, "fsync", sync)
    ready = asyncio.run(
        files.receive(TENANT, OWNER, upload["upload_id"], chunks(b"data"))
    )
    assert ready["state"] == "ready" and ready["upload_id"] == upload["upload_id"]
    store.close()
