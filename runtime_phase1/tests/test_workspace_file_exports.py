import io
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from workers_projects_runtime.workspace_file_api import install_workspace_file_routes
from workers_projects_runtime.workspace_file_exports import (
    CHUNK_BYTES,
    WorkspaceFileExport,
)
from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles


class FileStore:
    def __init__(self, root):
        self.db_path = str(root / "state.db")
        self.worker = {
            "worker_id": "worker",
            "owner_id": "owner",
            "tenant_id": "tenant",
            "workspace_dir": str(root / "workspace"),
            "state": "ready",
        }
        Path(self.worker["workspace_dir"]).mkdir()
        with self._connect() as conn:
            from workers_projects_runtime.workspace_file_mutations import (
                ensure_file_mutation_schema,
            )

            ensure_file_mutation_schema(conn)
            conn.execute("""CREATE TABLE workspace_file_entries (
                file_id TEXT PRIMARY KEY, tenant_id TEXT, owner_id TEXT, workspace_key TEXT,
                path TEXT, deleted INTEGER DEFAULT 0, UNIQUE(tenant_id,owner_id,workspace_key,path))""")

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_worker(self, worker_id, tenant_id, owner_id):
        if (worker_id, tenant_id, owner_id) == ("worker", "tenant", "owner"):
            return self.worker


@pytest.fixture
def workspace(tmp_path):
    files = WorkspaceFiles(FileStore(tmp_path))
    return files, Path(files.store.worker["workspace_dir"])


def ids(files, directory=""):
    return {
        item["name"]: item["file_id"]
        for item in files.list_files("worker", "tenant", "owner", directory=directory)[
            "items"
        ]
    }


def prepare(files, selected):
    return WorkspaceFileExport(files, "worker", "tenant", "owner", selected)


def test_closed_workspace_keeps_retained_files_readable_but_rejects_changes(workspace):
    files, root = workspace
    (root / "result.txt").write_bytes(b"saved result")
    file_id = ids(files)["result.txt"]
    files.store.worker["state"] = "terminated"

    listed = files.list_files("worker", "tenant", "owner")
    assert listed["can_write"] is False
    assert listed["items"][0]["file_id"] == file_id
    descriptor, _, _ = files.open_download(
        "worker", "tenant", "owner", file_id,
        revision=listed["items"][0]["revision"],
    )
    try:
        assert os.read(descriptor, 20) == b"saved result"
    finally:
        os.close(descriptor)
    assert b"saved result" in b"".join(prepare(files, [file_id]).stream())
    with pytest.raises(FileAdmissionError, match="Workspace is closed"):
        files.mkdir("worker", "tenant", "owner", "new")
    for state in ("terminating", "termination_failed"):
        files.store.worker["state"] = state
        with pytest.raises(FileAdmissionError, match="Workspace is closed"):
            files.list_files("worker", "tenant", "owner")


def test_nested_multi_selection_exact_bytes_empty_folder_and_no_retained_copy(
    workspace, monkeypatch
):
    files, root = workspace
    (root / "notes" / "empty").mkdir(parents=True)
    (root / "notes" / "nested").mkdir()
    payload = bytes(range(256)) * 5000
    (root / "notes" / "nested" / "data.bin").write_bytes(payload)
    (root / "résumé.txt").write_bytes("Crème brûlée".encode())
    selected = ids(files)
    nested_id = ids(files, "notes/nested")["data.bin"]
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "0")
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    chunks = list(
        prepare(files, [selected["notes"], nested_id, selected["résumé.txt"]]).stream()
    )
    assert max(map(len, chunks)) <= CHUNK_BYTES
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as archive:
        assert set(archive.namelist()) == {
            "notes/",
            "notes/empty/",
            "notes/nested/",
            "notes/nested/data.bin",
            "résumé.txt",
        }
        assert archive.read("notes/nested/data.bin") == payload
        assert archive.read("résumé.txt") == "Crème brûlée".encode()
        assert archive.testzip() is None
    assert (
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before
    )
    assert not files.private_root.exists()


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "private"])
def test_folder_rejects_unsafe_descendant_instead_of_partial_archive(workspace, unsafe):
    files, root = workspace
    (root / "folder").mkdir()
    (root / "source.txt").write_bytes(b"private source")
    selected = ids(files)["folder"]
    target = root / "folder" / "unsafe"
    if unsafe == "symlink":
        target.symlink_to(root / "source.txt")
    elif unsafe == "hardlink":
        os.link(root / "source.txt", target)
    elif unsafe == "fifo":
        os.mkfifo(target)
    else:
        (root / "folder" / ".private").mkdir()
    with pytest.raises(FileAdmissionError) as error:
        prepare(files, [selected])
    assert error.value.status_code == 403


def test_owner_scope_and_database_path_traversal(workspace):
    files, root = workspace
    (root / "input.txt").write_text("input")
    selected = ids(files)["input.txt"]
    with pytest.raises(FileAdmissionError) as error:
        WorkspaceFileExport(files, "worker", "tenant", "other-owner", [selected])
    assert error.value.status_code == 404
    with files.store._connect() as conn:
        conn.execute(
            "UPDATE workspace_file_entries SET path='../state.db' WHERE file_id=?",
            (selected,),
        )
    with pytest.raises(FileAdmissionError) as error:
        prepare(files, [selected])
    assert error.value.status_code == 422


def test_stale_selection_conflicts_before_first_byte(workspace):
    files, root = workspace
    target = root / "input.txt"
    target.write_text("input")
    exported = prepare(files, [ids(files)["input.txt"]])
    target.write_text("new input")
    with pytest.raises(FileAdmissionError, match="changed"):
        next(exported.stream())


def test_midstream_mutation_aborts_without_valid_archive(workspace):
    files, root = workspace
    target = root / "input.bin"
    target.write_bytes(b"a" * CHUNK_BYTES * 3)
    stream = prepare(files, [ids(files)["input.bin"]]).stream()
    chunks = [next(stream), next(stream)]
    with target.open("r+b") as output:
        output.seek(CHUNK_BYTES * 2)
        output.write(b"changed")
    with pytest.raises(FileAdmissionError, match="changed"):
        for chunk in stream:
            chunks.append(chunk)
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(b"".join(chunks)))


def test_export_routes_allow_viewer_reads_and_reject_foreign_identity(workspace):
    files, root = workspace
    (root / "input.txt").write_bytes(b"exact bytes")
    selected = ids(files)["input.txt"]
    app = FastAPI()

    def context(request):
        return SimpleNamespace(
            auth_mode="session",
            role="viewer",
            enterprise=True,
            owner_id=request.headers.get("x-test-owner", "owner"),
            tenant_id="tenant",
        )

    def require_worker(worker_id, request):
        worker = files.store.get_worker(worker_id, "tenant", context(request).owner_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="Worker not found")
        return worker

    install_workspace_file_routes(app, files, context, require_worker)
    client = TestClient(app)
    for method in ("GET", "POST"):
        response = (
            client.get(
                "/v1/workers/worker/files/export", params=[("file_ids", selected)]
            )
            if method == "GET"
            else client.post(
                "/v1/workers/worker/files/export", json={"file_ids": [selected]}
            )
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["cache-control"] == "private, no-store"
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.read("input.txt") == b"exact bytes"
    denied = client.get(
        "/v1/workers/worker/files/export",
        params=[("file_ids", selected)],
        headers={"x-test-owner": "other"},
    )
    assert denied.status_code == 404
    missing = client.post(
        "/v1/workers/worker/files/export", json={"file_ids": ["missing"]}
    )
    assert missing.status_code == 404

    files.store.worker["state"] = "terminated"
    listing = client.get("/v1/workers/worker/files")
    assert listing.status_code == 200
    assert listing.json()["can_write"] is False
    retained = client.get(
        "/v1/workers/worker/files/export", params=[("file_ids", selected)]
    )
    assert retained.status_code == 200
    with zipfile.ZipFile(io.BytesIO(retained.content)) as archive:
        assert archive.read("input.txt") == b"exact bytes"
