"""The owner catalog exposes workspace identity without private runtime state."""

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app


def test_catalog_keeps_actual_workspace_identity_name_mode_and_owner_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DEFAULT_OWNER_ID", "synthetic-owner")
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub", reconcile_on_startup=False)
    with TestClient(app) as client:
        store = app.state.store
        project = store.create_project(
            "synthetic-owner", "Shared research space", "Inspect supplied sources", "codex-cli",
        )
        shared = store.create_execution_workspace(
            project_id=project["project_id"], tenant_id="local", owner_id="synthetic-owner",
            execution_mode="docker",
        )
        worker = store.create_worker(
            project_id=project["project_id"], owner_id="synthetic-owner", name="Research specialist",
            role="member", profile="codex-cli", backend="codex-cli", runtime="synthetic", model="synthetic-model",
            workspace_id=shared["workspace_id"], workspace_kind="named",
            bootstrap_bundle={"provider_account": {"policy": "personal_required", "account_id": "acct_synthetic"},
                              "env": {"SYNTHETIC_SECRET": "never-return"}},
        )
        other = store.create_project("other-owner", "Private other workspace", "Other task", "codex-cli")
        store.create_worker(
            project_id=other["project_id"], owner_id="other-owner", name="Other worker", role="main",
            profile="codex-cli", backend="codex-cli", runtime="synthetic", model="synthetic-model", workspace_kind="named",
        )
        response = client.get("/v1/workspaces?kind=named")
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        item = response.json()["items"][0]
        assert item["worker_id"] == worker["worker_id"]
        assert item["workspace_id"] == shared["workspace_id"]
        assert item["workspace_name"] == "Shared research space"
        assert item["execution_workspace_mode"] == "shared"
        assert item["name"] == "Research specialist"
        assert item["provider_account"] == {"policy": "personal_required", "account_id": "acct_synthetic"}
        assert "bootstrap_bundle_json" not in item
        assert "workspace_dir" not in item
        assert "never-return" not in response.text
        assert "Other worker" not in response.text
