"""Current-file navigation uses exact native placement and existing artifact authority."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.signed_links import sign_link_params
from test_internal_assertions import (
    assertion_keys, configure_signed_assertions, fresh_assertion_headers,
    ISSUER, AUDIENCE,
)


@pytest.mark.parametrize("native_root", [
    "/workspace/common", "/workspace/data/members/20002/worktree",
])
def test_live_artifact_native_aliases_open_exact_current_file(tmp_path, monkeypatch, native_root):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    runtime = StubRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"owner_id":"demo-owner", "title":"Synthetic file navigation", "goal":"Open the exact authorized current file."}).json()
        worker = client.post(f"/v1/projects/{project['project_id']}/workers", json={"owner_id":"demo-owner", "name":"Synthetic member", "role":"writer", "profile":"codex-cli", "execution_mode":"docker"}).json()
        root = Path(worker["workspace_dir"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "report one.txt").write_text("EXACT_CURRENT_FILE")
        (root / ".mcp.json").write_text("{}")
        runtime.native_workspace_placement = lambda current: {
            "mode":"shared", "workspace_id":current["workspace_id"],
            "member_id":current["worker_id"], "workspace_dir":current["workspace_dir"],
            "native_workspace":native_root,
        }
        live = client.get(f"/v1/workers/{worker['worker_id']}/live").json()
        items = live["artifacts"]["items"]
        assert len(items) == 1
        item = items[0]
        assert item["native_workspace_root"] == native_root
        assert item["native_paths"] == [native_root + "/report one.txt", "report one.txt", "./report one.txt"]
        assert client.get(item["open_url"]).status_code == 200
        assert client.get(item["download_url"]).content == b"EXACT_CURRENT_FILE"
        # An alias is current-file navigation, not a new claim of run authorship.
        assert "run_id" not in item and "result_revision" not in item
        runtime.native_workspace_placement = lambda current: {
            "mode":"shared", "workspace_id":current["workspace_id"],
            "member_id":"another-member", "workspace_dir":current["workspace_dir"],
            "native_workspace":"/workspace/data/members/20001/worktree",
        }
        foreign = client.get(f"/v1/workers/{worker['worker_id']}/live").json()["artifacts"]["items"][0]
        assert "native_paths" not in foreign
        # A mission's immutable completion envelope keeps its existing owner.
        runtime.native_workspace_placement = lambda current: {
            "workspace_id":current["workspace_id"], "member_id":current["worker_id"],
            "workspace_dir":current["workspace_dir"], "native_workspace":native_root,
        }
        monkeypatch.setattr(app.state.service.store, "get_delegation_for_worker", lambda *_args, **_kwargs: {"delegation_id":"synthetic-stale-mission"})
        mission = client.get(f"/v1/workers/{worker['worker_id']}/live").json()["artifacts"]["items"][0]
        assert "native_paths" not in mission


def test_ordinary_host_file_uses_owned_root_without_container_alias_guess(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"owner_id":"demo-owner", "title":"Synthetic host file", "goal":"Keep the existing host file route."}).json()
        worker = client.post(f"/v1/projects/{project['project_id']}/workers", json={"owner_id":"demo-owner", "name":"Synthetic host", "role":"writer", "profile":"codex-cli", "execution_mode":"host"}).json()
        root = Path(worker["workspace_dir"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.txt").write_text("HOST_CURRENT_FILE")
        item = client.get(f"/v1/workers/{worker['worker_id']}/live").json()["artifacts"]["items"][0]
        assert item["native_paths"][0] == str(root / "report.txt")
        assert not any(path.startswith("/workspace/") for path in item["native_paths"])
        assert client.get(item["download_url"]).content == b"HOST_CURRENT_FILE"


def _create_host_file(client, headers):
    project = client.post("/v1/projects", headers=headers(), json={"owner_id":"demo-owner", "title":"Synthetic private host", "goal":"Read an authorized file without disclosing host paths."}).json()
    worker = client.post(f"/v1/projects/{project['project_id']}/workers", headers=headers(), json={"owner_id":"demo-owner", "name":"Synthetic private host", "role":"writer", "profile":"codex-cli", "execution_mode":"host"}).json()
    root = Path(worker["workspace_dir"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.txt").write_text("AUTHORIZED_FILE_BYTES")
    return worker, root


def _assert_file_actions_keep_working(client, item, headers=lambda: {}):
    assert client.get(item["open_url"], headers=headers()).status_code == 200
    assert client.get(item["download_url"], headers=headers()).content == b"AUTHORIZED_FILE_BYTES"


def test_local_signed_viewer_cannot_receive_owner_host_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-runtime-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-workspace-link-secret")
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        headers = lambda: {"X-WPR-Token":"synthetic-runtime-service-token"}
        worker, root = _create_host_file(client, headers)
        path = f"/v1/workers/{worker['worker_id']}/live"
        owner = client.get(path, headers=headers()).json()
        assert owner["artifacts"]["items"][0]["native_paths"][0] == str(root / "report.txt")
        signed = sign_link_params(kind="worker_view", worker_id=worker["worker_id"], tenant_id=worker["tenant_id"], owner_id=worker["owner_id"])
        response = client.get(path, params=signed)
        assert response.status_code == 200
        viewer = response.json()
        item = viewer["artifacts"]["items"][0]
        assert "native_paths" not in item and "native_workspace_root" not in item
        assert "workspace_dir" not in viewer["worker"]
        assert str(root) not in response.text
        _assert_file_actions_keep_working(client, item)
        html = client.get(f"/ui/workers/{worker['worker_id']}", params=signed)
        assert html.status_code == 200 and str(root) not in html.text


@pytest.mark.parametrize("enterprise", [False, True])
def test_verified_service_viewer_keeps_files_but_not_owner_details(tmp_path, monkeypatch, assertion_keys, enterprise):
    """Use the existing real signed service-hop fixture, never trust a forged role header."""
    private_key, jwks = assertion_keys
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-workspace-link-secret")
    if enterprise:
        configure_signed_assertions(monkeypatch, jwks)
        tenant = "tenant-public-safe"
    else:
        tenant = "local"
        monkeypatch.setenv("WPR_API_TOKEN", "runtime-service-token")
        monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
        monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "local")
        monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "local_password")
        monkeypatch.setenv("GLASSHIVE_LOCAL_HUMAN_ASSERTION", "1")
        monkeypatch.setenv("GLASSHIVE_DEFAULT_OWNER_ID", "user-public-safe")
        monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_ISSUER", ISSUER)
        monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", AUDIENCE)
        import json
        monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON", json.dumps(jwks))
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        scope = "runtime:access workspaces:read workspaces:write runtime:internal_details"
        headers = lambda: fresh_assertion_headers(private_key, tenant=tenant, role="tenant_admin", scope=scope)
        worker, root = _create_host_file(client, headers)
        path = f"/v1/workers/{worker['worker_id']}/live"
        owner = client.get(path, headers=headers())
        assert owner.status_code == 200, owner.text
        assert owner.json()["artifacts"]["items"][0]["native_paths"][0] == str(root / "report.txt")
        viewer_headers = lambda: fresh_assertion_headers(private_key, tenant=tenant, role="viewer", scope=scope)
        response = client.get(path, headers=viewer_headers())
        assert response.status_code == 200, response.text
        item = response.json()["artifacts"]["items"][0]
        assert "native_paths" not in item and "native_workspace_root" not in item
        assert str(root) not in response.text
        if enterprise:
            assert client.get(item["open_url"]).status_code == 401
        _assert_file_actions_keep_working(client, item, viewer_headers if enterprise else lambda: {})
        # Correct visibility does not turn read access into control access.
        assert client.post(f"/v1/workers/{worker['worker_id']}/pause", headers=viewer_headers()).status_code == 403
