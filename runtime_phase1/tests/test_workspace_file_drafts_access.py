"""Stored draft access uses owner authority and immutable accepted versions."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid
import zipfile

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles
from workers_projects_runtime.workspace_file_api import install_workspace_file_routes
from workers_projects_runtime.workspace_file_drafts import DraftDownload, DraftExport
from workers_projects_runtime.workspace_file_exports import CHUNK_BYTES


@pytest.fixture
def drafts(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    monkeypatch.delenv("GLASSHIVE_OWNER_STORAGE_BYTES", raising=False)
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-draft-access-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    store = Store(str(tmp_path / "state.db"))
    files = WorkspaceFiles(store, owner_roots=lambda *_: [])
    app = FastAPI()
    auth = SimpleNamespace(auth_mode="session", tenant_id="tenant", owner_id="owner", role="owner", enterprise=True)
    install_workspace_file_routes(app, files, lambda _: auth, lambda *_: None)
    with TestClient(app) as client:
        yield files, client, auth, app
    store.close()


def upload(files, data=b"hello", path="brief.txt", draft="draft"):
    item = files.create_upload("tenant", "owner", draft_id=draft, name=Path(path).name,
        relative_path=path, size_bytes=len(data), idempotency_key=uuid.uuid4().hex)
    async def chunks():
        for offset in range(0, len(data), CHUNK_BYTES):
            yield data[offset:offset + CHUNK_BYTES]
    return asyncio.run(files.receive("tenant", "owner", item["upload_id"], chunks()))


def blob(files, item):
    return files._owner_root("tenant", "owner") / (item["upload_id"] + ".blob")


def test_binding_destination_column_migrates_existing_store(tmp_path):
    database = tmp_path / "historical.db"
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE workspace_file_bindings (
            binding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            worker_id TEXT NOT NULL, request_key TEXT NOT NULL, manifest_json TEXT NOT NULL,
            UNIQUE(tenant_id, owner_id, worker_id, request_key))""")
    store = Store(str(database))
    try:
        with store._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_file_bindings)")}
        assert "request_directory_json" in columns
    finally:
        store.close()


def test_metadata_and_binary_empty_range_download_remain_available_at_quota(drafts, monkeypatch):
    files, client, _, _ = drafts
    data = bytes(range(256)) * 3000
    for expected, path in ((data, "nested/résumé.bin"), (b"", "empty.txt")):
        item = upload(files, expected, path)
        monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "0")
        result = client.get(f"/v1/file-uploads/{item['upload_id']}")
        assert result.status_code == 200
        public = result.json()
        assert public["revision"] == item["revision"]
        assert "source_path" not in public and "owner_id" not in public
        response = client.get(public["download_url"])
        assert response.content == expected
        assert response.headers["cache-control"] == "private, no-store"
        response = client.get(public["download_url"], headers={"Range": "bytes=5-12"})
        assert response.status_code == (206 if expected else 416)
        if expected:
            assert response.content == expected[5:13]
        monkeypatch.delenv("GLASSHIVE_OWNER_STORAGE_BYTES")


def test_zip_preserves_paths_bytes_and_emits_no_copy(drafts, monkeypatch):
    files, client, _, _ = drafts
    payloads = {"top.txt": b"", "Folder/data.bin": bytes(range(256)) * 5000, "Folder/子/notes.txt": b"notes"}
    items = [upload(files, data, path) for path, data in payloads.items()]
    before = sorted(files._owner_root("tenant", "owner").iterdir())
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "0")
    selection = {"upload_ids": [item["upload_id"] for item in items], "revisions": [item["revision"] for item in items]}
    descriptor = client.post("/v1/file-uploads/export/prepare", json=selection)
    assert descriptor.status_code == 200
    response = client.post(descriptor.json()["path"], json=descriptor.json()["body"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == set(payloads)
        assert {name: archive.read(name) for name in archive.namelist()} == payloads
    assert sorted(files._owner_root("tenant", "owner").iterdir()) == before
    chunks = DraftExport(files, "tenant", "owner", selection["upload_ids"], selection["revisions"]).stream()
    assert max(map(len, chunks)) <= CHUNK_BYTES


def test_rename_move_revision_conflicts_and_persistence(drafts):
    files, client, _, _ = drafts
    item = upload(files)
    identity = blob(files, item).stat()
    url = f"/v1/file-uploads/{item['upload_id']}"
    moved = client.patch(url, json={"relative_path": "folder/renamed.txt", "revision": item["revision"]})
    assert moved.status_code == 200
    new = moved.json()
    assert new["name"] == "renamed.txt" and new["relative_path"] == "folder/renamed.txt"
    assert new["revision"] != item["revision"] and new["sha256"] == item["sha256"]
    assert blob(files, item).stat() == identity
    assert client.get(url).json()["revision"] == new["revision"]
    assert WorkspaceFiles(files.store).get_upload("tenant", "owner", item["upload_id"])["relative_path"] == "folder/renamed.txt"
    assert client.patch(url, json={"relative_path": "stale.txt", "revision": item["revision"]}).status_code == 409
    assert client.get(url + "/content", params={"revision": item["revision"]}).status_code == 409
    assert client.post("/v1/file-uploads/export/prepare", json={"upload_ids": [item["upload_id"]], "revisions": [item["revision"]]}).status_code == 409
    # ABA must not restore an older metadata revision.
    back = client.patch(url, json={"relative_path": "brief.txt", "revision": new["revision"]}).json()
    assert back["revision"] not in (item["revision"], new["revision"])
    listed = client.get("/v1/file-uploads", params={"draft_id": "draft"}).json()["items"]
    assert listed[0]["revision"] == back["revision"]


@pytest.mark.parametrize("path", ["../bad", "/bad", "a/../bad", "a\\bad", "a//bad", ".ssh/key", ".env", "a\x00bad"])
def test_hostile_path_denied(drafts, path):
    files, client, _, _ = drafts
    item = upload(files)
    response = client.patch(f"/v1/file-uploads/{item['upload_id']}", json={"relative_path": path, "revision": item["revision"]})
    assert response.status_code in (403, 422)
    assert files.get_upload("tenant", "owner", item["upload_id"])["relative_path"] == "brief.txt"


def test_conflicting_paths_and_readiness_are_explicit(drafts):
    files, client, _, _ = drafts
    a, b = upload(files, path="a.txt"), upload(files, path="nested/b.txt")
    for target in ("a.txt", "a.txt/child", "nested"):
        result = client.patch(f"/v1/file-uploads/{b['upload_id']}", json={"relative_path": target, "revision": b["revision"]})
        # 'nested' is valid after b itself moves out; only another entry conflicts.
        if target != "nested":
            assert result.status_code == 409
    duplicate = upload(files, path="a.txt", draft="other")
    assert client.post("/v1/file-uploads/export/prepare", json={"upload_ids": [a["upload_id"], duplicate["upload_id"]]}).status_code == 409
    assert client.post("/v1/file-uploads/export/prepare", json={"upload_ids": [a["upload_id"]], "revisions": []}).status_code == 422
    assert client.post("/v1/file-uploads/export/prepare", json={"upload_ids": [a["upload_id"], a["upload_id"]]}).status_code == 422
    pending = files.create_upload("tenant", "owner", draft_id="draft", name="pending", size_bytes=2, idempotency_key="pending")
    assert client.get(f"/v1/file-uploads/{pending['upload_id']}").status_code == 409
    client.delete(f"/v1/file-uploads/{a['upload_id']}")
    assert client.get(f"/v1/file-uploads/{a['upload_id']}/content").status_code == 409


def test_cross_owner_signed_and_viewer_authority(drafts):
    files, client, auth, _ = drafts
    item = upload(files)
    path = f"/v1/file-uploads/{item['upload_id']}"
    for field, value in (("owner_id", "other"), ("tenant_id", "other"), ("auth_mode", "signed_link")):
        old = getattr(auth, field)
        setattr(auth, field, value)
        status = 403 if field == "auth_mode" else 404
        assert client.get(path).status_code == status
        assert client.get(path + "/content").status_code == status
        assert client.patch(path, json={"relative_path": "renamed", "revision": item["revision"]}).status_code == status
        assert client.post("/v1/file-uploads/export/prepare", json={"upload_ids": [item["upload_id"]]}).status_code == status
        setattr(auth, field, old)
    auth.role = "viewer"
    assert client.get(path + "/content").content == b"hello"
    assert client.patch(path, json={"relative_path": "renamed", "revision": item["revision"]}).status_code == 403


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "corrupt"])
def test_unsafe_or_corrupted_private_blob_denied_before_stream(drafts, kind):
    files, client, _, _ = drafts
    item = upload(files)
    target = blob(files, item)
    if kind == "symlink":
        target.unlink()
        target.symlink_to("outside")
    elif kind == "hardlink":
        os.link(target, target.with_suffix(".linked"))
    elif kind == "fifo":
        target.unlink()
        os.mkfifo(target)
    else:
        target.write_bytes(b"other")
    result = client.get(f"/v1/file-uploads/{item['upload_id']}/content")
    assert result.status_code == (409 if kind == "corrupt" else 403)


@pytest.mark.parametrize("mutation", ["append", "truncate", "rewrite", "rename", "cancel"])
def test_actual_stream_mutation_closes_and_retry_recovers(drafts, mutation):
    files, _, _, app = drafts
    data = b"a" * (2 * CHUNK_BYTES + 3)
    item = upload(files, data)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "name", "") == "draft_download")
    request = Request({"type": "http", "method": "GET", "path_params": {}, "headers": []})
    async def exercise():
        response = endpoint(item["upload_id"], request, item["revision"])
        iterator = response.body_iterator
        assert await anext(iterator) == data[:CHUNK_BYTES]
        target = blob(files, item)
        if mutation == "append":
            with target.open("ab") as stream:
                stream.write(b"extra")
        elif mutation == "truncate":
            target.write_bytes(b"short")
        elif mutation == "rewrite":
            with target.open("r+b") as stream:
                stream.write(b"b")
        elif mutation == "rename":
            files.move_upload("tenant", "owner", item["upload_id"], relative_path="renamed", revision=item["revision"])
        else:
            files.cancel("tenant", "owner", item["upload_id"])
        with pytest.raises(FileAdmissionError):
            async for _ in iterator:
                pass
        await response.background()
    asyncio.run(exercise())
    if mutation in {"append", "truncate", "rewrite"}:
        blob(files, item).write_bytes(data)
    if mutation != "cancel":
        recovered = DraftDownload(files, "tenant", "owner", item["upload_id"])
        assert b"".join(recovered.stream()) == data
        assert recovered.descriptor is None


def test_zip_midstream_mutation_aborts_and_fresh_export_recovers(drafts):
    files, _, _, _ = drafts
    data = b"z" * (CHUNK_BYTES * 2)
    item = upload(files, data)
    prepared = DraftExport(files, "tenant", "owner", [item["upload_id"]])
    stream = prepared.stream()
    assert next(stream).startswith(b"PK")
    blob(files, item).write_bytes(b"broken")
    with pytest.raises(FileAdmissionError):
        b"".join(stream)
    blob(files, item).write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(b"".join(DraftExport(files, "tenant", "owner", [item["upload_id"]]).stream()))) as archive:
        assert archive.read("brief.txt") == data


def test_rename_keeps_accepted_schedule_and_binding_snapshots_immutable(drafts, tmp_path):
    files, _, _, _ = drafts
    project = files.store.create_project("owner", "Drafts", "Keep accepted bytes", "codex-cli", tenant_id="tenant")
    worker = files.store.create_worker(project["project_id"], "owner", "Worker", "main", "codex-cli", "stub", "stub", "stub", tenant_id="tenant")
    root = tmp_path / "workspace"
    root.mkdir()
    files.store.update_worker(worker["worker_id"], workspace_dir=str(root))
    item = upload(files, b"accepted", "original/file.bin")
    bound = files.bind(worker["worker_id"], "tenant", "owner", [item["upload_id"]], "binding", directory="")
    manifest = files.manifest("tenant", "owner", [item["upload_id"]])
    schedule = files.store.create_scheduled_run(worker_id=worker["worker_id"], project_id=project["project_id"], tenant_id="tenant", owner_id="owner", run_at="2027-01-02T03:04:05+00:00", instruction="Use accepted input", file_manifest=manifest)
    with files.store._connect() as conn:
        before = conn.execute("SELECT file_manifest_json FROM scheduled_runs WHERE schedule_id=?", (schedule["schedule_id"],)).fetchone()[0]
    old_blob = blob(files, item).stat()
    files.move_upload("tenant", "owner", item["upload_id"], relative_path="new/name.bin", revision=item["revision"])
    assert files.bind(worker["worker_id"], "tenant", "owner", [item["upload_id"]], "binding", directory="") == bound
    with files.store._connect() as conn:
        after = conn.execute("SELECT file_manifest_json FROM scheduled_runs WHERE schedule_id=?", (schedule["schedule_id"],)).fetchone()[0]
    assert before == after and json.loads(after)[0]["relative_path"] == "original/file.bin"
    assert (root / "original/file.bin").read_bytes() == b"accepted"
    assert blob(files, item).stat() == old_blob
    with pytest.raises(FileAdmissionError, match="attached"):
        files.cancel("tenant", "owner", item["upload_id"])


@pytest.mark.parametrize("directory,changed", [(None, "beta"), ("", None), ("alpha", "beta")])
@pytest.mark.parametrize("historical", [False, True])
def test_binding_retry_keeps_the_accepted_destination(drafts, tmp_path, directory, changed, historical):
    files, client, _, _ = drafts
    project = files.store.create_project("owner", "Drafts", "Exact destination", "codex-cli", tenant_id="tenant")
    worker = files.store.create_worker(project["project_id"], "owner", "Worker", "main", "codex-cli", "stub", "stub", "stub", tenant_id="tenant")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "alpha").mkdir()
    (root / "beta").mkdir()
    files.store.update_worker(worker["worker_id"], workspace_dir=str(root))
    item = upload(files, b"\x00accepted\xff", "nested/input.bin")
    path = f"/v1/workers/{worker['worker_id']}/files"
    payload = {"upload_ids": [item["upload_id"]], "revisions": [item["revision"]],
               "idempotency_key": "same-key", "directory": directory}
    accepted = client.post(path, json=payload)
    assert accepted.status_code == 201, accepted.text
    if historical:
        with files.store._connect() as conn:
            conn.execute("UPDATE workspace_file_bindings SET request_directory_json=NULL WHERE request_key='same-key'")
    files.move_upload("tenant", "owner", item["upload_id"],
                      relative_path="renamed/input.bin", revision=item["revision"])
    replay = client.post(path, json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json() == accepted.json()
    accepted_path = accepted.json()["items"][0]["path"]
    assert (root / accepted_path).read_bytes() == b"\x00accepted\xff"
    changed_retry = client.post(path, json={**payload, "directory": changed})
    assert changed_retry.status_code == 409, changed_retry.text
    assert (root / accepted_path).read_bytes() == b"\x00accepted\xff"
    assert not any((root / "beta").iterdir())


def test_file_binding_rejects_stale_revision_but_keeps_id_only_compatibility(drafts, tmp_path):
    files, client, _, _ = drafts
    project = files.store.create_project("owner", "Drafts", "Check exact inputs", "codex-cli", tenant_id="tenant")
    worker = files.store.create_worker(project["project_id"], "owner", "Worker", "main", "codex-cli", "stub", "stub", "stub", tenant_id="tenant")
    root = tmp_path / "binding-revisions"
    root.mkdir()
    files.store.update_worker(worker["worker_id"], workspace_dir=str(root))
    item = upload(files, b"accepted", "original.txt")
    prefix = f"/v1/workers/{worker['worker_id']}/files"
    accepted = client.post(
        prefix,
        json={
            "upload_ids": [item["upload_id"]],
            "revisions": [item["revision"]],
            "idempotency_key": "binding-exact",
        },
    )
    assert accepted.status_code == 201, accepted.text
    moved = client.patch(
        f"/v1/file-uploads/{item['upload_id']}",
        json={"relative_path": "renamed.txt", "revision": item["revision"]},
    )
    assert moved.status_code == 200
    replay = client.post(
        prefix,
        json={
            "upload_ids": [item["upload_id"]],
            "revisions": [item["revision"]],
            "idempotency_key": "binding-exact",
        },
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == accepted.json()
    changed_retry = client.post(
        prefix,
        json={
            "upload_ids": [item["upload_id"]],
            "revisions": [moved.json()["revision"]],
            "idempotency_key": "binding-exact",
        },
    )
    assert changed_retry.status_code == 409
    assert client.post(
        prefix,
        json={
            "upload_ids": [item["upload_id"]],
            "revisions": [item["revision"], moved.json()["revision"]],
            "idempotency_key": "binding-exact",
        },
    ).status_code == 422
    stale = client.post(
        prefix,
        json={
            "upload_ids": [item["upload_id"]],
            "revisions": [item["revision"]],
            "idempotency_key": "binding-stale",
        },
    )
    assert stale.status_code == 409, stale.text
    legacy = client.post(
        prefix,
        json={
            "upload_ids": [item["upload_id"]],
            "idempotency_key": "binding-legacy",
        },
    )
    assert legacy.status_code == 201, legacy.text


def test_binding_denies_unavailable_owner_storage_before_acceptance(drafts, monkeypatch):
    files, client, _, _ = drafts
    project = files.store.create_project("owner", "Drafts", "Storage failure", "codex-cli", tenant_id="tenant")
    worker = files.store.create_worker(
        project["project_id"], "owner", "Worker", "main", "codex-cli",
        "stub", "stub", "stub", tenant_id="tenant",
    )
    item = upload(files, b"accepted", "input.txt")

    class UnavailableQuota:
        def snapshot(self, *_args):
            raise RuntimeError("synthetic owner storage outage")

    monkeypatch.setattr(files.storage_adapter, "backend", UnavailableQuota())
    result = client.post(
        f"/v1/workers/{worker['worker_id']}/files",
        json={
            "upload_ids": [item["upload_id"]],
            "revisions": [item["revision"]],
            "idempotency_key": "storage-unavailable",
        },
    )
    assert result.status_code == 503, result.text
    with files.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_bindings").fetchone()[0] == 0
    assert files.store.get_worker(worker["worker_id"])["bootstrap_bundle_json"] is None


def test_binding_reports_missing_source_authority_without_accepting_files(drafts, tmp_path, monkeypatch):
    files, client, _, _ = drafts
    project = files.store.create_project("owner", "Drafts", "Source authority", "codex-cli", tenant_id="tenant")
    worker = files.store.create_worker(
        project["project_id"], "owner", "Worker", "main", "codex-cli",
        "stub", "stub", "stub", tenant_id="tenant",
    )
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "pending").mkdir()
    files.store.update_worker(worker["worker_id"], workspace_dir=str(root))
    item = upload(files, b"exact input", "nested/input.txt")
    payload = {
        "upload_ids": [item["upload_id"]],
        "revisions": [item["revision"]],
        "idempotency_key": "source-authority-retry",
        "directory": "pending",
    }
    path = f"/v1/workers/{worker['worker_id']}/files"
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.delenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET")
    refused = client.post(path, json=payload)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["code"] == "stored_file_source_authority_unavailable"
    with files.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_bindings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_projection_targets").fetchone()[0] == 0
    assert files.store.get_worker(worker["worker_id"])["bootstrap_bundle_json"] is None
    assert list((root / "pending").iterdir()) == []

    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-draft-access-secret")
    accepted = client.post(path, json=payload)
    assert accepted.status_code == 201, accepted.text
    assert (root / accepted.json()["items"][0]["path"]).read_bytes() == b"exact input"


def test_raw_api_upload_move_retry_download_and_form_zip(drafts):
    files, client, _, _ = drafts
    data = b"\x00\xff\x80\x10" * (CHUNK_BYTES // 2)
    admission = {"draft_id": "api-draft", "name": "raw.bin", "relative_path": "original/raw.bin", "size_bytes": len(data), "idempotency_key": "api-raw"}
    created = client.post("/v1/file-uploads", json=admission)
    assert created.status_code == 201 and created.json()["revision"]
    upload_id = created.json()["upload_id"]
    ready = client.put(f"/v1/file-uploads/{upload_id}/content", content=data, headers={"Content-Type": "application/octet-stream"})
    assert ready.status_code == 200 and ready.json()["sha256"] == hashlib.sha256(data).hexdigest()
    moved = client.patch(f"/v1/file-uploads/{upload_id}", json={"relative_path": "Folder/renamed.bin", "revision": ready.json()["revision"]}).json()
    retried = client.post("/v1/file-uploads", json=admission)
    assert retried.status_code == 201 and retried.json() == moved
    assert client.get(f"/v1/file-uploads/{upload_id}/content", params={"revision": moved["revision"]}).content == data
    selection = {"upload_ids": [upload_id], "revisions": [moved["revision"]]}
    descriptor = client.get("/v1/file-uploads/export/prepare", params=selection).json()
    assert descriptor["method"] == "POST" and "source_path" not in descriptor
    assert descriptor["body"] == selection and "?" not in descriptor["path"]
    from urllib.parse import urlencode
    encoded = urlencode(selection, doseq=True)
    response = client.post("/v1/file-uploads/export", content=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("Folder/renamed.bin") == data
    assert client.post("/v1/file-uploads/export", json=selection).status_code == 200
    assert client.post("/v1/file-uploads/export", content="bad", headers={"Content-Type": "application/json"}).status_code == 422


def test_interrupted_download_and_export_close_descriptors(drafts, monkeypatch):
    files, _, _, _ = drafts
    item = upload(files, b"a" * (CHUNK_BYTES * 2))
    prepared = DraftDownload(files, "tenant", "owner", item["upload_id"])
    fd = prepared.descriptor
    stream = prepared.stream()
    assert next(stream)
    stream.close()
    with pytest.raises(OSError):
        os.fstat(fd)
    exported = DraftExport(files, "tenant", "owner", [item["upload_id"]])
    tracked = []
    original = DraftDownload.__init__
    def track(self, *args, **kwargs):
        original(self, *args, **kwargs)
        tracked.append((self, self.descriptor))
    monkeypatch.setattr(DraftDownload, "__init__", track)
    stream = exported.stream()
    assert next(stream).startswith(b"PK")
    stream.close()
    assert tracked
    for prepared, fd in tracked:
        assert prepared.descriptor is None
        with pytest.raises(OSError):
            os.fstat(fd)


def test_draft_stream_reads_only_bounded_chunks_including_preflight(drafts, monkeypatch):
    files, _, _, _ = drafts
    data = bytes(range(256)) * (CHUNK_BYTES // 32)
    item = upload(files, data)
    read_sizes = []
    original = os.read
    def bounded_read(fd, size):
        read_sizes.append(size)
        assert size <= CHUNK_BYTES
        return original(fd, size)
    monkeypatch.setattr(os, "read", bounded_read)
    prepared = DraftDownload(files, "tenant", "owner", item["upload_id"])
    digest = hashlib.sha256()
    for chunk in prepared.stream():
        digest.update(chunk)
    assert digest.hexdigest() == item["sha256"]
    assert len(read_sizes) >= 2 * len(data) // CHUNK_BYTES


def test_workspace_metadata_and_prepare_descriptors_are_owner_checked(drafts, tmp_path):
    files, client, auth, _ = drafts
    project = files.store.create_project("owner", "Files", "Read existing files", "codex-cli", tenant_id="tenant")
    worker = files.store.create_worker(project["project_id"], "owner", "Worker", "main", "codex-cli", "stub", "stub", "stub", tenant_id="tenant")
    root = tmp_path / "metadata"
    root.mkdir()
    (root / "folder").mkdir()
    (root / "folder/empty.txt").write_bytes(b"")
    files.store.update_worker(worker["worker_id"], workspace_dir=str(root))
    prefix = f"/v1/workers/{worker['worker_id']}/files"
    folder = client.get(prefix).json()["items"][0]
    metadata = client.get(prefix + "/" + folder["file_id"], params={"revision": folder["revision"]})
    assert metadata.status_code == 200 and metadata.json()["is_dir"] is True
    entry = client.get(prefix, params={"directory": "folder"}).json()["items"][0]
    result = client.get(prefix + "/" + entry["file_id"])
    assert client.get(result.json()["download_url"]).content == b""
    assert client.get(prefix + "/" + entry["file_id"], params={"revision": "stale"}).status_code == 409
    descriptor = client.get(prefix + "/export/prepare", params={"file_ids": folder["file_id"]})
    assert descriptor.status_code == 200
    assert descriptor.json()["method"] == "POST"
    assert descriptor.json()["body"] == {"file_ids": [folder["file_id"]]}
    assert "?" not in descriptor.json()["path"]
    assert client.post(descriptor.json()["path"], json=descriptor.json()["body"]).content.startswith(b"PK")
    auth.owner_id = "other"
    assert client.post(descriptor.json()["path"], json=descriptor.json()["body"]).status_code == 404
    assert client.get(prefix + "/" + entry["file_id"]).status_code == 404
    assert client.get(prefix + "/export/prepare", params={"file_ids": folder["file_id"]}).status_code == 404
