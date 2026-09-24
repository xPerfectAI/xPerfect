import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glass_drive_ui.workspace_routes import install_workspace_ui_routes


def test_shared_workspace_facade_forwards_typed_create_request():
    calls = []
    app = FastAPI()

    class Client:
        def _request(self, method, path, json_body=None):
            calls.append((method, path, json_body))
            return {"workspace_id": "wsp_synthetic", "runtime_readiness": {"available": True}}

    install_workspace_ui_routes(app, lambda request, human_confirmation=False: Client())
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/project-synthetic/execution-workspaces",
            json={"execution_mode": "docker", "file_placement": "member_private"},
        )
    assert response.status_code == 201
    assert calls == [(
        "POST",
        "/v1/projects/project-synthetic/execution-workspaces",
        {"mode": "shared", "execution_mode": "docker", "file_placement": "member_private"},
    )]


def test_shared_workspace_facade_masks_runtime_failures_but_preserves_admission_reason():
    app = FastAPI()

    class Client:
        def _request(self, *args, **kwargs):
            response = httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "shared_runtime_unavailable",
                        "message": "Shared workspace is unavailable until the container runtime is ready.",
                    }
                },
                request=httpx.Request("POST", "https://runtime.invalid"),
            )
            raise httpx.HTTPStatusError(
                "private runtime detail", request=response.request, response=response
            )

    install_workspace_ui_routes(app, lambda *args, **kwargs: Client())
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/project-synthetic/execution-workspaces",
            json={},
        )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "shared_runtime_unavailable",
            "message": "Shared workspace is unavailable until the container runtime is ready.",
        }
    }
