import asyncio
import hashlib
import os
from pathlib import Path
import tracemalloc
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.workspace_file_exports import CHUNK_BYTES


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_ARTIFACT_DOWNLOAD_MAX_BYTES", raising=False)

    class LocalStub(StubRuntime):
        def _runtime_info(self, worker, *, pid):
            info = super()._runtime_info(worker, pid=pid)
            info.workspace_dir = str(tmp_path / worker["worker_id"] / "workspace")
            info.state_dir = str(tmp_path / worker["worker_id"] / "state")
            return info

    app = create_app(
        str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=LocalStub()
    )
    client = TestClient(app)
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Files", "goal": "Stream downloads"},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Files",
            "role": "operator",
            "profile": "codex-cli",
        },
    ).json()
    root = Path(worker["workspace_dir"])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield app, client, root, f"/v1/workers/{worker['worker_id']}/artifacts"
    finally:
        app.state.service.shutdown()


async def consume_asgi(app, path, query, *, on_chunk=None):
    observed = {"size": 0, "max_chunk": 0, "digest": hashlib.sha256()}

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.start":
            observed["status"] = message["status"]
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            observed["size"] += len(body)
            observed["max_chunk"] = max(observed["max_chunk"], len(body))
            observed["digest"].update(body)
            if on_chunk and body:
                on_chunk(body)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query).encode(),
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
    }
    await app(scope, receive, send)
    return observed


def test_large_sparse_artifact_streams_above_old_limit_with_bounded_memory(artifacts):
    app, client, root, base = artifacts
    target = root / "large.bin"
    size = 104 * 1024 * 1024
    with target.open("wb") as output:
        output.seek(size)
        output.write(b"end")
    before = set(root.iterdir())
    tracemalloc.start()
    try:
        observed = asyncio.run(
            consume_asgi(app, base + "/download", {"path": target.name})
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert observed["status"] == 200
    assert observed["size"] == size + 3
    assert observed["max_chunk"] <= CHUNK_BYTES
    assert peak < 8 * 1024 * 1024
    expected = hashlib.sha256()
    for _ in range(size // CHUNK_BYTES):
        expected.update(bytes(CHUNK_BYTES))
    expected.update(b"end")
    assert observed["digest"].digest() == expected.digest()
    assert set(root.iterdir()) == before
    preview = client.get(base + "/open", params={"path": target.name})
    assert preview.status_code == 413
    assert "Download" in preview.json()["detail"]


def test_download_ranges_and_explicit_optional_limit(artifacts, monkeypatch):
    _, client, root, base = artifacts
    (root / "data.txt").write_bytes(b"0123456789")
    response = client.get(
        base + "/download", params={"path": "data.txt"}, headers={"Range": "bytes=3-5"}
    )
    assert response.status_code == 206
    assert response.content == b"345"
    assert response.headers["content-range"] == "bytes 3-5/10"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        client.get(
            base + "/download",
            params={"path": "data.txt"},
            headers={"Range": "bytes=-2"},
        ).content
        == b"89"
    )
    assert (
        client.get(
            base + "/download",
            params={"path": "data.txt"},
            headers={"Range": "bytes=99-"},
        ).status_code
        == 416
    )
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_DOWNLOAD_MAX_BYTES", "5")
    assert (
        client.get(base + "/download", params={"path": "data.txt"}).status_code == 413
    )
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_DOWNLOAD_MAX_BYTES", "-1")
    assert (
        client.get(base + "/download", params={"path": "data.txt"}).content
        == b"0123456789"
    )


@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
def test_download_rejects_nonordinary_files_without_blocking(artifacts, kind):
    _, client, root, base = artifacts
    if kind == "hardlink":
        (root / "source.txt").write_text("source")
        os.link(root / "source.txt", root / "unsafe.txt")
    else:
        os.mkfifo(root / "unsafe.txt")
    assert (
        client.get(base + "/download", params={"path": "unsafe.txt"}).status_code == 400
    )


def test_concurrent_edit_aborts_download(artifacts):
    app, _, root, base = artifacts
    target = root / "changing.bin"
    target.write_bytes(b"a" * CHUNK_BYTES * 3)
    changed = False

    def mutate(_):
        nonlocal changed
        if not changed:
            changed = True
            with target.open("r+b") as output:
                output.seek(CHUNK_BYTES * 2)
                output.write(b"new content")

    with pytest.raises(Exception, match="handled exception|changed"):
        asyncio.run(
            consume_asgi(
                app, base + "/download", {"path": target.name}, on_chunk=mutate
            )
        )
    assert changed
