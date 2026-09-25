from pathlib import Path
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime


def test_workspace_upload_api_streams_and_enforces_owner_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "5")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli")
    # Every supported launcher trusts its own managed-files store as a source root.
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()
        )
    )
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Files",
            "goal": "Keep exact inputs",
            "default_worker_profile": "codex-cli",
        },
    )
    assert project.status_code == 201
    worker = client.post(
        f"/v1/projects/{project.json()['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "File worker",
            "role": "main",
            "profile": "codex-cli",
            "bootstrap_profile": "none",
        },
    )
    assert worker.status_code == 201
    worker_id = worker.json()["worker_id"]
    Path(worker.json()["workspace_dir"]).mkdir(parents=True, exist_ok=True)
    created = client.post(
        "/v1/file-uploads",
        json={
            "draft_id": "draft",
            "name": "brief.txt",
            "size_bytes": 3,
            "idempotency_key": "first",
        },
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    uploaded = client.put(f"/v1/file-uploads/{upload_id}/content", content=b"abc")
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["received_bytes"] == 3
    denied = client.post(
        "/v1/file-uploads",
        json={
            "draft_id": "draft",
            "name": "second.txt",
            "size_bytes": 3,
            "idempotency_key": "second",
        },
    )
    assert denied.status_code == 413
    # Retaining both the immutable accepted version and its workspace copy costs six bytes.
    denied_copy = client.post(
        f"/v1/workers/{worker_id}/files",
        json={"upload_ids": [upload_id], "idempotency_key": "binding"},
    )
    assert denied_copy.status_code == 413
