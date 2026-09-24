import httpx
import pytest
from fastapi.testclient import TestClient
from glass_drive_ui.server import create_app
from test_server import FakeRuntimeClient, clear_glasshive_ui_env  # noqa: F401


@pytest.mark.parametrize(
    "destination", ["new:codex-cli", "open:wrk_1", "duplicate:wrk_1"]
)
def test_launch_binds_exact_uploads_before_admitting_run(destination):
    runtime = FakeRuntimeClient()
    operations = []
    original_assign = runtime.assign_run
    original_files = runtime.file_request

    def files(method, path, payload=None):
        operations.append(("bind", path, payload))
        return original_files(method, path, payload)

    def assign(worker_id, instruction):
        operations.append(("assign", worker_id))
        return original_assign(worker_id, instruction)

    runtime.file_request = files
    runtime.assign_run = assign
    response = TestClient(create_app(runtime_client=runtime)).post(
        "/api/launch",
        json={
            "description": "Use the files",
            "success_criteria": "Match both accepted file versions",
            "context": "Background supplied by the owner",
            "workspace_option": destination,
            "idempotency_key": "repeatable-launch",
            "file_upload_ids": ["fil_first", "fil_second"],
            "file_upload_revisions": ["rev_first", "rev_second"],
        },
    )
    assert response.status_code == 200, response.text
    assert operations[0][0] == "bind"
    assert operations[0][1] == f"/v1/workers/{response.json()['worker_id']}/files"
    assert operations[0][2]["upload_ids"] == ["fil_first", "fil_second"]
    assert operations[0][2]["revisions"] == ["rev_first", "rev_second"]
    if response.json()["status"] != "action_required":
        assert operations[1][0] == "assign"
        instruction = runtime.assign_requests[0]["instruction"]
        assert "Success Criteria:\nMatch both accepted file versions" in instruction
        assert "Context:\nBackground supplied by the owner" in instruction


def test_schedule_carries_upload_ids_without_mutating_current_workspace():
    runtime = FakeRuntimeClient()
    response = TestClient(create_app(runtime_client=runtime)).post(
        "/api/launch",
        json={
            "description": "Use these accepted versions",
            "success_criteria": "Check each accepted revision",
            "context": "Background for the scheduled work",
            "workspace_option": "open:wrk_1",
            "schedule_text": "tomorrow at noon",
            "file_upload_ids": ["fil_one", "fil_two"],
            "file_upload_revisions": ["rev_one", "rev_two"],
        },
    )
    assert response.status_code == 200, response.text
    assert runtime.schedule_requests[0]["file_upload_ids"] == ["fil_one", "fil_two"]
    assert runtime.schedule_requests[0]["file_upload_revisions"] == ["rev_one", "rev_two"]
    assert "Success Criteria:\nCheck each accepted revision" in runtime.schedule_requests[0]["instruction"]
    assert "Context:\nBackground for the scheduled work" in runtime.schedule_requests[0]["instruction"]
    assert runtime.file_upload_requests == []
    assert runtime.assign_requests == []


def test_launch_rejects_unpaired_file_revisions_before_creating_or_starting_workspace():
    runtime = FakeRuntimeClient()
    response = TestClient(create_app(runtime_client=runtime)).post(
        "/api/launch",
        json={
            "description": "Use the selected file",
            "workspace_option": "open:wrk_1",
            "file_upload_ids": ["fil_one", "fil_two"],
            "file_upload_revisions": ["rev_one"],
        },
    )
    assert response.status_code == 422
    assert runtime.file_upload_requests == []
    assert runtime.assign_requests == []
    assert runtime.schedule_requests == []


def test_failed_file_binding_does_not_dispatch():
    runtime = FakeRuntimeClient()

    def reject(*args, **kwargs):
        request = httpx.Request("POST", "http://runtime.invalid/files")
        response = httpx.Response(
            413, json={"detail": "Not enough storage"}, request=request
        )
        raise httpx.HTTPStatusError(
            "Not enough storage", request=request, response=response
        )

    runtime.file_request = reject
    response = TestClient(create_app(runtime_client=runtime)).post(
        "/api/launch",
        json={
            "description": "Use file",
            "workspace_option": "open:wrk_1",
            "file_upload_ids": ["fil_one"],
        },
    )
    assert response.status_code == 413
    assert runtime.assign_requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/storage",
        "/api/file-uploads?draft_id=owner-draft",
        "/api/workspace/wrk_other/files",
    ],
)
def test_shared_workspace_view_cannot_read_owner_drafts_or_another_workspace(
    path, monkeypatch
):
    from test_server import set_enterprise_ui_env, signed_worker_token

    set_enterprise_ui_env(monkeypatch, signed_secret="synthetic-file-view-secret")
    token = signed_worker_token("synthetic-file-view-secret")
    runtime = FakeRuntimeClient()
    response = TestClient(create_app(runtime_client=runtime)).get(
        path + ("&" if "?" in path else "?") + "gh_token=" + token
    )
    assert response.status_code == 401, response.text
    assert runtime.file_upload_requests == []


def test_goal_only_launch_uses_configured_route_without_invented_criteria(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    result = client.post("/api/launch", json={"description": "Summarize the supplied report"})
    assert result.status_code == 200, result.text
    instruction = runtime.assign_requests[-1]["instruction"]
    assert "Summarize the supplied report" in instruction
    assert "Success Criteria:" not in instruction
    assert "Context:" not in instruction
    assert runtime.create_worker_requests[-1]["profile"] == "claude-code"


def test_blank_goal_cannot_start_work():
    runtime = FakeRuntimeClient()
    result = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={"description": "  "})
    assert result.status_code == 422
    assert runtime.assign_requests == []
