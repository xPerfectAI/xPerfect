"""MCP controls and host byte transport share the real owner-scoped Files API."""
import asyncio
import json
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from workers_projects_runtime import mcp_server
from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.workspace_file_mcp import WorkspaceFilesMcpClient


def output(result):
    value = result.structured_content or {}
    return value.get("result", value)


@pytest.fixture
def live_files(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-link-secret")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "synthetic-mcp-token")
    monkeypatch.setattr(mcp_server, "multi_user_security_enabled", lambda: False)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-test")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", "synthetic-account-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_OWNER_STORAGE_BYTES", raising=False)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    http = TestClient(app)
    headers = {"x-viventium-tenant-id": "tenant-test", "x-viventium-user-id": "owner-test", "x-viventium-user-role": "member"}
    headers["x-wpr-token"] = "synthetic-mcp-token"
    host_headers = {**headers, "Authorization": "Bearer synthetic-service-token", "x-wpr-token": "synthetic-service-token"}
    calls = []
    class Bridge:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            parsed = urlsplit(url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            calls.append((method, path, kwargs))
            return http.request(method, path, **kwargs)
    monkeypatch.setattr(mcp_server.httpx, "Client", Bridge)
    monkeypatch.setattr(mcp_server, "_request_headers", lambda: dict(headers))
    monkeypatch.setattr(mcp_server, "_runtime_dependency_blocked_payload", lambda **kwargs: None)
    monkeypatch.setattr(mcp_server, "_trusted_operation_idempotency_key", lambda *args, **kwargs: "mcp-files-atomic-retry")
    api = mcp_server.WorkersProjectsApiClient(base_url="http://runtime.invalid", api_token="synthetic-service-token")
    store = app.state.store
    project = store.create_project("owner-test", "Files", "Check Files parity", "codex-cli", tenant_id="tenant-test")
    worker = store.create_worker(project["project_id"], "owner-test", "Files", "operator", "codex-cli", "stub", "stub", "stub", tenant_id="tenant-test")
    root = tmp_path / "workspace"
    root.mkdir()
    worker = store.update_worker(worker["worker_id"], workspace_dir=str(root))
    app.state.service.start_assigned_run = lambda *args, **kwargs: None
    yield api, http, host_headers, worker, root, app, headers, calls
    app.state.service.shutdown()
    store.close()


def test_host_stream_then_mcp_bind_mutate_undo_and_owner_boundary(live_files):
    api, http, auth, worker, root, app, headers, calls = live_files
    server = mcp_server.create_mcp_server(api_client=api)
    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["workspace_files_trash"].annotations.readOnlyHint is False
            assert "content" not in tools["file_upload_begin"].inputSchema["properties"]
            started = output(await client.call_tool("file_upload_begin", {"draft_id": "draft", "name": "input.txt", "size_bytes": 3, "idempotency_key": "upload-one"}))
            route = started["upload_request"]
            assert route["method"] == "PUT" and route["authentication_required"] is True
            assert route["path"].startswith("/v1/file-uploads/")
            assert http.put(route["path"], content=b"abc").status_code == 401
            assert http.put(route["path"], headers=auth, content=b"abc").status_code == 200
            ready = output(await client.call_tool("file_uploads_list", {"draft_id": "draft"}))
            assert ready["items"][0]["state"] == "ready"
            await client.call_tool("workspace_files_bind", {"worker_id": worker["worker_id"], "upload_ids": [started["upload_id"]], "idempotency_key": "bind-one", "directory": ""})
            await client.call_tool("workspace_files_mkdir", {"worker_id": worker["worker_id"], "path": "folder"})
            listed = output(await client.call_tool("workspace_files_list", {"worker_id": worker["worker_id"]}))
            item = next(item for item in listed["items"] if item["name"] == "input.txt")
            moved = output(await client.call_tool("workspace_files_move", {"worker_id": worker["worker_id"], "file_id": item["file_id"], "revision": item["revision"], "path": "folder/renamed.txt"}))
            assert (root / "folder/renamed.txt").read_bytes() == b"abc"
            with pytest.raises(Exception):
                await client.call_tool("workspace_files_remove", {"worker_id": worker["worker_id"], "file_id": item["file_id"], "revision": item["revision"]})
            current = output(await client.call_tool("workspace_files_list", {"worker_id": worker["worker_id"], "directory": "folder"}))["items"][0]
            removed = output(await client.call_tool("workspace_files_remove", {"worker_id": worker["worker_id"], "file_id": item["file_id"], "revision": current["revision"]}))
            trash = output(await client.call_tool("workspace_files_trash", {"worker_id": worker["worker_id"]}))
            assert removed["undo_id"] in json.dumps(trash)
            await client.call_tool("workspace_files_restore", {"worker_id": worker["worker_id"], "file_id": item["file_id"], "undo_id": removed["undo_id"]})
            assert (root / "folder/renamed.txt").read_bytes() == b"abc"
            headers["x-viventium-user-id"] = "another-owner"
            with pytest.raises(Exception):
                await client.call_tool("workspace_files_list", {"worker_id": worker["worker_id"]})
            headers["x-viventium-user-id"] = "owner-test"
            headers["x-viventium-user-role"] = "viewer"
            with pytest.raises(Exception):
                await client.call_tool("workspace_files_trash", {"worker_id": worker["worker_id"]})
            with pytest.raises(Exception):
                await client.call_tool("workspace_files_mkdir", {"worker_id": worker["worker_id"], "path": "denied"})
            assert not (root / "denied").exists()
            assert all(secret not in json.dumps(started) for secret in ("Bearer", "runtime.invalid", str(root)))
    asyncio.run(scenario())
    assert all(call[2]["headers"]["Authorization"] == "Bearer synthetic-service-token" for call in calls)


def uploaded(live_files, key="one"):
    api, http, auth, *_ = live_files
    receipt = api._request("POST", "/v1/file-uploads", json_body={"draft_id": "draft", "name": "same.txt", "size_bytes": 3, "idempotency_key": key})
    assert http.put(f"/v1/file-uploads/{receipt['upload_id']}/content", headers=auth, content=b"abc").status_code == 200
    return receipt["upload_id"]


def test_host_upload_then_mcp_atomic_launch_retry_pins_exact_ids(live_files):
    api, _, _, _, _, app, _, _ = live_files
    upload_id = uploaded(live_files)
    async def scenario():
        async with Client(mcp_server.create_mcp_server(api_client=api)) as client:
            args = {"description": "Read the attached input", "file_upload_ids": [upload_id], "execution_mode": "docker", "profile": "codex-cli"}
            first = output(await client.call_tool("workspace_launch", args))
            second = output(await client.call_tool("workspace_launch", args))
            assert first["status"] == "dispatched" and second["idempotent_replay"] is True
            with app.state.store._connect() as conn:
                row = conn.execute("SELECT initial_run_id,worker_id FROM delegations").fetchone()
                manifest = app.state.store.get_run_file_manifest(row["initial_run_id"], worker_id=row["worker_id"], tenant_id="tenant-test", owner_id="owner-test")
                assert [item["upload_id"] for item in manifest] == [upload_id]
                assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM workspace_file_pins WHERE reference_id=?", (row["initial_run_id"],)).fetchone()[0] == 1
            other = uploaded(live_files, "other")
            rejected = output(await client.call_tool("workspace_launch", {**args, "file_upload_ids": [other]}))
            assert rejected["status"] == "blocked"
            assert rejected["failure_class"] == "delegation_idempotency_conflict"
    asyncio.run(scenario())


def test_mcp_schedule_exact_upload_ids_survive_cancel_and_run_acceptance(live_files):
    api, _, _, worker, _, app, _, _ = live_files
    upload_id = uploaded(live_files)
    async def scenario():
        async with Client(mcp_server.create_mcp_server(api_client=api)) as client:
            scheduled = output(await client.call_tool("worker_schedule", {"worker_id": worker["worker_id"], "instruction": "Read stored input", "file_upload_ids": [upload_id], "delay_seconds": 600}))
            stored = app.state.store.get_schedule(scheduled["schedule_id"])
            assert json.loads(stored["file_manifest_json"])[0]["upload_id"] == upload_id
            assert app.state.store.claim_schedule(stored["schedule_id"])
            run, _ = app.state.store.create_or_get_run_for_schedule(stored["schedule_id"])
            app.state.store.finalize_schedule(stored["schedule_id"], state="cancelled")
            assert app.state.store.get_run_file_manifest(run["run_id"], worker_id=worker["worker_id"], tenant_id="tenant-test", owner_id="owner-test")[0]["upload_id"] == upload_id
    asyncio.run(scenario())


def test_failed_file_acceptance_never_dispatches_and_mixed_transport_rejects(live_files):
    api, _, _, worker, _, app, _, _ = live_files
    upload_id = uploaded(live_files)
    calls = []
    api.assign_run = lambda *args, **kwargs: (calls.append(args) or {"run_id": "run_synthetic", "state": "queued"})
    async def scenario():
        async with Client(mcp_server.create_mcp_server(api_client=api)) as client:
            with pytest.raises(Exception):
                await client.call_tool("worker_run", {"worker_id": worker["worker_id"], "instruction": "Read input", "file_upload_ids": ["fil_missing"], "file_binding_key": "binding-one"})
            assert calls == []
            with pytest.raises(Exception):
                await client.call_tool("workspace_launch", {"description": "Read input", "file_upload_ids": [upload_id], "uploaded_files": [{"filename": "other.txt", "text": "other"}], "execution_mode": "docker", "profile": "codex-cli"})
            with app.state.store._connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 0
            await client.call_tool("worker_run", {"worker_id": worker["worker_id"], "instruction": "Read input", "file_upload_ids": [upload_id], "file_binding_key": "binding-one"})
            assert len(calls) == 1
    asyncio.run(scenario())


def test_file_route_ids_cannot_redirect_client():
    client = WorkspaceFilesMcpClient(mcp_server.WorkersProjectsApiClient())
    for bad in ("../other", "x?token=x", "x/content", "https://example.invalid"):
        with pytest.raises(ValueError):
            client.file_path("wrk_safe", bad)


def test_mcp_retrieval_descriptors_revalidate_auth_and_stream_outside_mcp(live_files):
    import io
    import zipfile
    api, http, auth, worker, root, _, headers, _ = live_files
    upload_id = uploaded(live_files)
    async def scenario():
        async with Client(mcp_server.create_mcp_server(api_client=api)) as client:
            draft = output(await client.call_tool("file_upload_download", {"upload_id": upload_id}))
            assert draft["authentication_required"] is True
            moved = output(await client.call_tool("file_upload_move", {"upload_id": upload_id, "relative_path": "folder/renamed.txt", "revision": draft["revision"]}))
            assert moved["relative_path"] == "folder/renamed.txt"
            headers["x-viventium-user-role"] = "viewer"
            bundle = output(await client.call_tool("file_uploads_export", {"upload_ids": [upload_id], "revisions": [moved["revision"]]}))
            assert bundle["method"] == "POST" and bundle["authentication_required"] is True
            assert "?" not in bundle["path"]
            assert http.post(bundle["path"], json=bundle["body"]).status_code == 401
            archive = http.post(bundle["path"], json=bundle["body"], headers={**auth, "x-viventium-user-role": "viewer"})
            assert archive.status_code == 200
            with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
                assert zipped.read("folder/renamed.txt") == b"abc"
            headers["x-viventium-user-role"] = "member"
            await client.call_tool("workspace_files_bind", {"worker_id": worker["worker_id"], "upload_ids": [upload_id], "idempotency_key": "bind-one", "directory": ""})
            listed = output(await client.call_tool("workspace_files_list", {"worker_id": worker["worker_id"], "directory": "folder"}))
            item = listed["items"][0]
            selected = output(await client.call_tool("workspace_files_download", {"worker_id": worker["worker_id"], "file_id": item["file_id"], "revision": item["revision"]}))
            assert selected["authentication_required"] is True
            assert http.get(selected["download_url"], headers=auth).content == b"abc"
            headers["x-viventium-user-role"] = "viewer"
            bundle = output(await client.call_tool("workspace_files_export", {"worker_id": worker["worker_id"], "file_ids": [item["file_id"]]}))
            assert http.post(bundle["path"], json=bundle["body"], headers={**auth, "x-viventium-user-role": "viewer"}).status_code == 200
            serialized = json.dumps([draft, moved, selected, bundle])
            assert all(value not in serialized for value in ("Bearer", "runtime.invalid", str(root)))
            headers["x-viventium-user-id"] = "another-owner"
            with pytest.raises(Exception):
                await client.call_tool("file_upload_download", {"upload_id": upload_id})
    asyncio.run(scenario())


def test_atomic_scope_resolver_reads_prospective_worker_in_same_transaction(live_files):
    api, _, _, _, _, app, _, _ = live_files
    upload_id = uploaded(live_files)
    seen = []
    def resolver(worker_id, *, tenant_id, owner_id, connection=None):
        if connection is None:
            row = app.state.store.get_worker(worker_id, tenant_id, owner_id)
        else:
            row = connection.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
            seen.append(worker_id)
        assert row["tenant_id"] == tenant_id and row["owner_id"] == owner_id
        return {"scope_id": "member:" + worker_id, "kind": "member", "workspace_id": row["project_id"], "member_id": worker_id, "root": "/synthetic/admitted/member"}
    app.state.service.files.scope_resolver = resolver
    result = api.create_delegation(tenant_id="tenant-test", owner_id="owner-test", idempotency_key="prospective-inputs", payload={"title": "Files", "goal": "Read input", "instruction": "Read input", "profile": "codex-cli", "executionMode": "docker", "fileUploadIds": [upload_id]})
    assert result["acceptedFileUploadIds"] == [upload_id]
    assert len(seen) == 1


def test_atomic_missing_upload_rolls_back_project_worker_run_and_pins(live_files):
    api, _, _, _, _, app, _, _ = live_files
    with app.state.store._connect() as conn:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("projects", "workers", "runs", "delegations", "workspace_file_pins")}
    with pytest.raises(Exception):
        api.create_delegation(tenant_id="tenant-test", owner_id="owner-test", idempotency_key="missing-inputs", payload={"title": "Files", "goal": "Read input", "instruction": "Read input", "profile": "codex-cli", "executionMode": "docker", "fileUploadIds": ["fil_missing"]})
    with app.state.store._connect() as conn:
        assert counts == {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in counts}


def test_atomic_copy_reservation_failure_rolls_back_before_dispatch(live_files):
    api, _, _, _, _, app, _, _ = live_files
    upload_id = uploaded(live_files)
    app.state.service.files.set_policy("tenant-test", "owner-test", {"storage_limit_bytes": 3})
    with app.state.store._connect() as conn:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("projects", "workers", "runs", "delegations", "workspace_file_pins")}
    with pytest.raises(mcp_server.GlassHiveBlockedError, match="Not enough storage"):
        api.create_delegation(tenant_id="tenant-test", owner_id="owner-test", idempotency_key="quota-inputs", payload={"title": "Files", "goal": "Read input", "instruction": "Read input", "profile": "codex-cli", "executionMode": "docker", "fileUploadIds": [upload_id]})
    with app.state.store._connect() as conn:
        assert counts == {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in counts}


def test_trash_requires_write_hop_while_prepared_export_uses_read_hop(monkeypatch):
    import httpx
    writes = []
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setattr(mcp_server, "get_access_token", lambda: object())
    monkeypatch.setattr(mcp_server, "_request_headers", lambda: {"x-viventium-user-id": "owner", "x-viventium-tenant-id": "tenant", "x-viventium-user-role": "member"})
    monkeypatch.setattr(mcp_server, "signed_runtime_assertion", lambda **kwargs: (writes.append(kwargs["write"]) or "synthetic-signed-assertion"))
    class Transport:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, method, url, **kwargs):
            assert kwargs["headers"]["X-GlassHive-User-Assertion"] == "synthetic-signed-assertion"
            return httpx.Response(200, json={"items": []}, request=httpx.Request(method, url))
    monkeypatch.setattr(mcp_server.httpx, "Client", Transport)
    api = mcp_server.WorkersProjectsApiClient(base_url="http://runtime.invalid")
    api._request("GET", "/v1/workers/wrk_safe/files/trash", require_write_scope=True)
    api._request("POST", "/v1/workers/wrk_safe/files/export/prepare", json_body={"file_ids": ["wf_safe"]}, file_export_prepare=True)
    api._request("POST", "/v1/file-uploads/export/prepare", json_body={"upload_ids": ["fil_safe"]}, file_export_prepare=True)
    assert writes == [True, False, False]
    for path in ("/v1/storage/policy", "/v1/workers/wrk_safe/files", "/v1/workers/wrk_safe/files/export"):
        with pytest.raises(ValueError, match="restricted"):
            api._request("POST", path, file_export_prepare=True)


def test_workspace_schedule_threads_exact_ids_to_durable_manifest(live_files):
    api, _, _, _, _, app, _, _ = live_files
    upload_id = uploaded(live_files)
    async def scenario():
        async with Client(mcp_server.create_mcp_server(api_client=api)) as client:
            result = output(await client.call_tool("workspace_schedule", {"description": "Read the input later", "file_upload_ids": [upload_id], "delay_seconds": 600, "execution_mode": "docker", "profile": "codex-cli"}))
            assert result["status"] == "scheduled"
    asyncio.run(scenario())
    with app.state.store._connect() as conn:
        row = conn.execute("SELECT file_manifest_json FROM scheduled_runs").fetchone()
        assert json.loads(row[0])[0]["upload_id"] == upload_id


def test_duplicate_binds_exact_uploads_to_returned_destination_and_retry(live_files):
    api, _, _, worker, _, _, _, _ = live_files
    upload_id = uploaded(live_files)
    # Duplication itself has existing control-plane coverage. This checks the
    # added MCP orchestration against the real destination Files authority.
    api.duplicate_workspace = lambda *args, **kwargs: {"workspace": worker, "idempotent_replay": True}
    async def scenario():
        async with Client(mcp_server.create_mcp_server(api_client=api)) as client:
            args = {"worker_id": "wrk_source", "idempotency_key": "duplicate-files-key", "file_upload_ids": [upload_id]}
            for _ in range(2):
                result = output(await client.call_tool("workspace_duplicate", args))
                assert result["workspace"]["worker_id"] == worker["worker_id"]
    asyncio.run(scenario())


def test_bind_missing_runtime_ack_is_not_reported_as_accepted():
    class OldRuntime:
        _path_id = mcp_server.WorkersProjectsApiClient._path_id
        def _request(self, *args, **kwargs):
            return {"state": "available", "items": []}
    with pytest.raises(ValueError, match="exact stored file binding"):
        WorkspaceFilesMcpClient(OldRuntime()).bind("wrk_safe", ["fil_safe"], "binding-key")
