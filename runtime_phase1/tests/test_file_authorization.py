from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.auth import AuthContext
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.signed_links import sign_link_token
from workers_projects_runtime.workspace_file_api import install_workspace_file_routes


def test_real_auth_boundary_scopes_drafts_policy_and_signed_file_views(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-test")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-key")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-link-key")
    app = create_app(
        str(tmp_path / "state.db"), runtime_backend="stub", runtime=StubRuntime()
    )
    client = TestClient(app)
    owner = {
        "X-WPR-Token": "synthetic-service-key",
        "X-Viventium-Tenant-Id": "tenant-test",
        "X-Viventium-User-Id": "owner-one",
        "X-Viventium-User-Role": "member",
    }
    other = {**owner, "X-Viventium-User-Id": "owner-two"}
    viewer = {**owner, "X-Viventium-User-Role": "viewer"}
    admin = {**owner, "X-Viventium-User-Role": "tenant_admin"}
    metadata = {
        "draft_id": "draft",
        "name": "input.txt",
        "size_bytes": 3,
        "idempotency_key": "file-one",
    }
    assert client.post("/v1/file-uploads", json=metadata).status_code == 401
    assert (
        client.post("/v1/file-uploads", json=metadata, headers=viewer).status_code
        == 403
    )
    created = client.post("/v1/file-uploads", json=metadata, headers=owner)
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    assert (
        client.put(
            f"/v1/file-uploads/{upload_id}/content", content=b"abc", headers=other
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/v1/file-uploads/{upload_id}", headers=other).status_code == 404
    )
    assert (
        client.get("/v1/file-uploads?draft_id=draft", headers=other).json()["items"]
        == []
    )
    assert (
        client.put(
            f"/v1/file-uploads/{upload_id}/content", content=b"abc", headers=owner
        ).status_code
        == 200
    )
    assert (
        client.patch(
            "/v1/storage/policy", json={"storage_limit_bytes": 10}, headers=owner
        ).status_code
        == 200
    )
    assert (
        client.patch(
            "/v1/storage/policy", json={"storage_limit_bytes": 11}, headers=owner
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/v1/storage/owners/owner-two/policy",
            json={"storage_limit_bytes": 5},
            headers=owner,
        ).status_code
        == 403
    )
    updated = client.patch(
        "/v1/storage/owners/owner-two/policy",
        json={"storage_limit_bytes": 5},
        headers=admin,
    )
    assert updated.status_code == 200 and updated.json()["limit_bytes"] == 5

    store = app.state.store
    project = store.create_project(
        "owner-one", "Files", "Check ownership", "codex-cli", tenant_id="tenant-test"
    )
    workers = []
    for name in ("first", "second"):
        worker = store.create_worker(
            project["project_id"],
            "owner-one",
            name,
            "main",
            "codex-cli",
            "stub",
            "stub",
            "stub",
            tenant_id="tenant-test",
        )
        root = tmp_path / name
        root.mkdir()
        (root / "report.txt").write_bytes(b"owned content")
        store.update_worker(worker["worker_id"], workspace_dir=str(root))
        workers.append(worker["worker_id"])
    token = sign_link_token(
        kind="worker_view",
        worker_id=workers[0],
        tenant_id="tenant-test",
        owner_id="owner-one",
    )
    base = f"/v1/workers/{workers[0]}/files"
    listing = client.get(base, headers=viewer)
    assert listing.status_code == 200, listing.text
    assert listing.json()["can_write"] is False
    assert client.get(f"{base}/trash", headers=viewer).status_code == 403
    assert client.get(f"{base}/trash", headers=owner).status_code == 200
    item = listing.json()["items"][0]
    result = client.get(item["download_url"], headers=other)
    assert result.status_code == 404
    result = client.get(item["download_url"], headers=viewer)
    assert result.status_code == 200 and result.content == b"owned content"
    assert (
        client.post(
            base,
            headers=viewer,
            json={"upload_ids": [upload_id], "idempotency_key": "attach"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/v1/workers/{workers[1]}/files", params={"gh_token": token}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/file-uploads", params={"draft_id": "draft", "gh_token": token}
        ).status_code
        == 401
    )


def test_trash_requires_write_scope_even_for_a_member_read_assertion():
    class Files:
        def trash(self, *_args):
            return {"items": []}

    context = AuthContext(
        tenant_id="tenant-test",
        user_id="owner-one",
        role="member",
        scopes=("runtime:access", "workspaces:read"),
        auth_mode="signed_internal_assertion",
        enterprise=True,
    )
    app = FastAPI()
    install_workspace_file_routes(app, Files(), lambda _request: context, lambda _worker, _request: {})
    client = TestClient(app)
    assert client.get("/v1/workers/wrk_1/files/trash").status_code == 403
