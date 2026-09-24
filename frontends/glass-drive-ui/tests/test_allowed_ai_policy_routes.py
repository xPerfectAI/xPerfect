import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glass_drive_ui.allowed_ai_policy_routes import install_allowed_ai_policy_ui_routes


POLICY = {
    "version": 1,
    "mode": "selected",
    "harnesses": [
        {
            "profile": "codex-cli",
            "models": {"mode": "selected", "ids": ["codex-cli:model-a"]},
            "connections": {"mode": "selected", "ids": ["acct_example"]},
        }
    ],
}
PAYLOAD = {"expected_revision": 3, "policy": POLICY}


def harness():
    calls = []
    app = FastAPI()

    class Client:
        def __init__(self, human):
            self.human = human

        def _request(self, method, path, json_body=None):
            calls.append((self.human, method, path, json_body))
            return {"scope_id": "project/example", "revision": 4, "policy": POLICY}

    install_allowed_ai_policy_ui_routes(
        app, lambda request, human_confirmation=False: Client(human_confirmation)
    )
    return TestClient(app), calls


def test_policy_and_options_forward_owner_scope_and_exact_revision():
    client, calls = harness()
    with client:
        assert client.get("/api/projects/project-example/execution-policy").status_code == 200
        assert client.get("/api/projects/project-example/execution-options").status_code == 200
        assert (
            client.put("/api/projects/project-example/execution-policy", json=PAYLOAD).status_code
            == 200
        )
    assert calls == [
        (False, "GET", "/v1/projects/project-example/execution-policy", None),
        (False, "GET", "/v1/projects/project-example/execution-options", None),
        (True, "PUT", "/v1/projects/project-example/execution-policy", PAYLOAD),
    ]


def test_workspace_inherit_is_valid_but_project_inherit_and_all_with_ids_are_rejected():
    client, calls = harness()
    with client:
        workspace = {"expected_revision": 0, "policy": {"version": 1, "mode": "inherit", "harnesses": []}}
        assert client.put("/api/workspaces/workspace-example/execution-policy", json=workspace).status_code == 200
        project_inherit = workspace
        assert client.put("/api/projects/project-example/execution-policy", json=project_inherit).status_code == 422
        invalid_all = {
            "expected_revision": 3,
            "policy": {
                **POLICY,
                "harnesses": [{
                    "profile": "codex-cli",
                    "models": {"mode": "all", "ids": ["forbidden"]},
                    "connections": {"mode": "selected", "ids": []},
                }],
            },
        }
        assert client.put("/api/projects/project-example/execution-policy", json=invalid_all).status_code == 422
    assert calls == [
        (True, "PUT", "/v1/workspaces/workspace-example/execution-policy", workspace),
    ]


def test_runtime_errors_are_bounded_and_do_not_reveal_detail_text():
    app = FastAPI()

    class Client:
        def _request(self, *args, **kwargs):
            response = httpx.Response(
                409,
                json={"detail": {"code": "policy_changed", "debug": "private-value"}},
                request=httpx.Request("GET", "https://runtime.invalid"),
            )
            raise httpx.HTTPStatusError("private-value", request=response.request, response=response)

    install_allowed_ai_policy_ui_routes(app, lambda request, **kwargs: Client())
    with TestClient(app) as client:
        response = client.get("/api/projects/project-example/execution-policy")
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "policy_changed"}}
