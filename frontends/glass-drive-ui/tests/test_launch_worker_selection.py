"""Worker identity and workspace placement use the existing launch owners."""

import httpx
import pytest
from fastapi.testclient import TestClient

from glass_drive_ui.server import create_app
from test_server import FakeRuntimeClient, clear_glasshive_ui_env  # noqa: F401


class ExistingWorkspaceClient(FakeRuntimeClient):
    def __init__(self):
        super().__init__()
        self.workspace_reads = []
        self.existing_workspace = {
            **self.shared_workspace_response,
            "workspace_id": "wsp_existing",
            "project_id": "prj_existing",
            "file_placement": "member_private",
        }
        self.workspace_error = None
        self.member_error = None

    def execution_workspace_members(self, workspace_id):
        self.workspace_reads.append(workspace_id)
        if self.workspace_error:
            raise self.workspace_error
        return dict(self.existing_workspace)

    def add_execution_workspace_member(self, workspace_id, payload):
        if self.member_error:
            raise self.member_error
        super().add_execution_workspace_member(workspace_id, payload)
        return {
            "worker_id": "wrk_shared",
            "workspace_id": workspace_id,
            "project_id": "prj_existing",
        }


def runtime_rejection(status, detail):
    response = httpx.Response(
        status,
        json={"detail": detail},
        request=httpx.Request("POST", "https://runtime.invalid/workspaces"),
    )
    return httpx.HTTPStatusError("synthetic rejection", request=response.request, response=response)


@pytest.mark.parametrize("name,expected", [(None, "Inspect source files"), ("", "Inspect source files"), ("  Research specialist  ", "Research specialist")])
def test_new_worker_optional_name_does_not_change_project_goal(name, expected):
    runtime = FakeRuntimeClient()
    payload = {"description": "Inspect source files", "workspace_option": "new:codex-cli"}
    if name is not None:
        payload["worker_name"] = name
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json=payload)
    assert response.status_code == 200, response.text
    assert runtime.create_worker_requests[0]["name"] == expected
    assert runtime.create_worker_requests[0]["workspace_kind"] == ("named" if name and name.strip() else "ephemeral")
    assert runtime.create_project_requests[0]["goal"] == "Inspect source files"
    assert runtime.assign_requests[0]["worker_id"] == "wrk_new"


def test_copy_name_uses_existing_idempotent_copy_contract():
    runtime = FakeRuntimeClient()
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Review another scenario",
        "workspace_option": "duplicate:wrk_1",
        "worker_name": "  Independent reviewer  ",
        "idempotency_key": "copy-selection-key",
    })
    assert response.status_code == 200, response.text
    assert runtime.workspace_duplicate_requests == [{
        "worker_id": "wrk_1", "idempotency_key": "copy-selection-key", "name": "Independent reviewer",
    }]
    assert runtime.create_project_requests == runtime.create_worker_requests == []
    assert runtime.assign_requests[0]["worker_id"] == "wrk_dup"


@pytest.mark.parametrize("selection,extra,code", [
    ("open:wrk_1", {"worker_name": "Different name"}, "existing_worker_name_locked"),
    ("existing:wrk_1", {"worker_name": "Different name"}, "existing_worker_name_locked"),
    ("open:wrk_1", {"execution_workspace_id": "wsp_existing"}, "existing_worker_workspace_locked"),
    ("duplicate:wrk_1", {"execution_workspace_id": "wsp_existing"}, "existing_worker_workspace_locked"),
])
def test_reuse_or_copy_cannot_move_an_existing_identity(selection, extra, code):
    runtime = ExistingWorkspaceClient()
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Use the selected worker", "workspace_option": selection, **extra,
    })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert runtime.workspace_reads == runtime.get_worker_requests == []
    assert runtime.create_project_requests == runtime.create_worker_requests == []
    assert runtime.add_execution_workspace_member_requests == runtime.assign_requests == []


def test_new_worker_uses_exact_existing_workspace_project_account_and_files():
    runtime = ExistingWorkspaceClient()
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Inspect the supplied materials",
        "success_criteria": "Explain the result",
        "context": "Keep this exact background",
        "workspace_option": "new:codex-cli",
        "worker_name": "Materials reviewer",
        "execution_workspace_id": "wsp_existing",
        "workspace_type": "host",  # New-workspace placement cannot move the chosen workspace.
        "workspace_file_placement": "common",
        "provider_account_policy": "personal_required",
        "provider_account_id": "acct_1",
        "effort": "high",
        "file_upload_ids": ["upload-first", "upload-second"],
        "file_upload_revisions": ["rev-first", "rev-second"],
        "idempotency_key": "exact-source-files",
    })
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == "prj_existing"
    assert runtime.workspace_reads == ["wsp_existing"]
    assert runtime.create_project_requests == runtime.create_worker_requests == []
    assert runtime.create_execution_workspace_requests == []
    assert runtime.add_execution_workspace_member_requests == [{
        "workspace_id": "wsp_existing",
        "payload": {
            "name": "Materials reviewer", "role": "Explain the result", "profile": "codex-cli",
            "effort": "high", "provider_account_policy": "personal_required", "provider_account_id": "acct_1",
        },
    }]
    assert runtime.file_upload_requests == [("POST", "/v1/workers/wrk_shared/files", {
        "upload_ids": ["upload-first", "upload-second"],
        "revisions": ["rev-first", "rev-second"],
        "idempotency_key": "exact-source-files",
    })]
    assert runtime.assign_requests[0]["worker_id"] == "wrk_shared"
    assert "Keep this exact background" in runtime.assign_requests[0]["instruction"]
    assert runtime.existing_workspace["file_placement"] == "member_private"


@pytest.mark.parametrize("patch,code", [
    ({"mode": "isolated"}, "existing_workspace_isolated"),
    ({"execution_mode": "host"}, "shared_host_execution_unavailable"),
    ({"runtime_readiness": {"available": False, "code": "shared_capacity_busy"}}, "shared_capacity_busy"),
])
def test_existing_workspace_ineligibility_rejects_before_mutation(patch, code):
    runtime = ExistingWorkspaceClient()
    runtime.existing_workspace.update(patch)
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Add a worker", "workspace_option": "new:codex-cli",
        "execution_workspace_id": "wsp_existing",
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code
    assert runtime.create_project_requests == runtime.create_worker_requests == []
    assert runtime.add_execution_workspace_member_requests == runtime.assign_requests == []


@pytest.mark.parametrize("at_read,status", [(True, 404), (False, 409)])
def test_native_scope_or_changed_admission_cannot_create_a_replacement(at_read, status):
    runtime = ExistingWorkspaceClient()
    error = runtime_rejection(status, {"code": "workspace_unavailable", "message": "Workspace is unavailable."})
    if at_read:
        runtime.workspace_error = error
    else:
        runtime.member_error = error
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Add a worker", "workspace_option": "new:codex-cli",
        "execution_workspace_id": "wsp_existing",
    })
    assert response.status_code == status
    assert runtime.create_project_requests == runtime.create_worker_requests == []
    assert runtime.create_execution_workspace_requests == []
    assert runtime.add_execution_workspace_member_requests == runtime.assign_requests == []


def test_existing_workspace_member_failure_keeps_exact_created_identity():
    runtime = ExistingWorkspaceClient()
    runtime.fail_assign = True
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Add a worker", "workspace_option": "new:codex-cli",
        "execution_workspace_id": "wsp_existing",
    })
    assert response.status_code == 502
    assert len(runtime.add_execution_workspace_member_requests) == 1
    assert runtime.launch_failures[0]["worker_id"] == "wrk_shared"
    assert runtime.create_project_requests == runtime.create_worker_requests == []


def test_name_limit_is_validated_before_runtime_mutation():
    runtime = FakeRuntimeClient()
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Create a worker", "worker_name": "a" * 121,
    })
    assert response.status_code == 422
    assert runtime.create_project_requests == runtime.create_worker_requests == []


def test_shared_member_metadata_limit_keeps_full_required_outcome_in_instruction():
    runtime = ExistingWorkspaceClient()
    outcome = "Preserve the complete requested outcome. " * 7
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Review supplied materials", "success_criteria": outcome,
        "workspace_option": "new:codex-cli", "execution_workspace_id": "wsp_existing",
    })
    assert response.status_code == 200, response.text
    assert runtime.add_execution_workspace_member_requests[0]["payload"]["role"] == outcome[:80]
    assert outcome.strip() in runtime.assign_requests[0]["instruction"]


def test_explicit_user_reuse_retains_one_off_worker_before_admitting_new_work():
    runtime = FakeRuntimeClient()
    runtime.get_worker = lambda worker_id: {"worker_id": worker_id, "project_id": "prj_1", "profile": "codex-cli", "workspace_kind": "ephemeral"}
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Continue this worker", "workspace_option": "open:wrk_1",
    })
    assert response.status_code == 200, response.text
    assert runtime.metadata_requests == [{"worker_id": "wrk_1", "payload": {"workspace_kind": "named"}}]
    assert runtime.assign_requests[0]["worker_id"] == "wrk_1"
    assert runtime.create_worker_requests == runtime.create_project_requests == []


def test_exact_worker_detail_returns_safe_selection_metadata_without_credentials():
    runtime = ExistingWorkspaceClient()
    runtime.get_worker = lambda worker_id: {"worker_id": worker_id, "project_id": "prj_1", "name": "Research specialist",
        "profile": "codex-cli", "workspace_id": "wsp_existing", "workspace_kind": "named",
        "workspace_dir": "/private/runtime", "bootstrap_bundle_json": "never-return", "gateway_token": "never-return"}
    runtime.existing_workspace["members"] = [{"worker_id": "wrk_26", "provider_account": {
        "account_id": "acct_1", "policy": "personal_required", "secret_locator": "never-return"}}]
    response = TestClient(create_app(runtime_client=runtime)).get("/api/workspaces/wrk_26")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["worker_id"] == "wrk_26" and data["workspace_id"] == "wsp_existing"
    assert data["name"] == "Research specialist" and data["workspace_name"] == "Alpha"
    assert data["execution_workspace_mode"] == "shared"
    assert data["provider_account"] == {"account_id": "acct_1", "policy": "personal_required"}
    assert "never-return" not in response.text and "/private" not in response.text
