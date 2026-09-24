import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glass_drive_ui.worker_configuration_routes import (
    install_worker_configuration_ui_routes,
)

PAYLOAD = {
    "expected_revision": 4,
    "context": {
        "mode": "selected",
        "source_ids": ["bootstrap:project_definition"],
        "inline_chars": 0,
    },
    "tools": {"mode": "selected", "mcp_server_ids": []},
    "background": {"enabled": False, "max_parallel_runs": 1},
}


def harness():
    calls = []
    app = FastAPI()

    class Client:
        def __init__(self, human):
            self.human = human

        def _request(self, method, path, json_body=None):
            calls.append((self.human, method, path, json_body))
            return {"revision": 5}

    install_worker_configuration_ui_routes(
        app, lambda request, human_confirmation=False: Client(human_confirmation)
    )
    return TestClient(app), calls


def test_configuration_projects_exact_revision_and_owner_confirmation():
    client, calls = harness()
    with client:
        assert client.get("/api/workers/synthetic/configuration").status_code == 200
        assert (
            client.put("/api/workers/synthetic/configuration", json=PAYLOAD).status_code
            == 200
        )
    assert calls == [
        (False, "GET", "/v1/workers/synthetic/configuration", None),
        (True, "PUT", "/v1/workers/synthetic/configuration", PAYLOAD),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"owner_id": "other"},
        {"expected_revision": 0},
        {"context": {"mode": "guess", "source_ids": [], "inline_chars": 4}},
        {"context": {"mode": "inherit", "source_ids": [], "inline_chars": 262145}},
        {"background": {"enabled": True, "max_parallel_runs": 33}},
        {
            "tools": {
                "mode": "inherit",
                "mcp_server_ids": [],
                "grant_all_permissions": True,
            }
        },
    ],
)
def test_configuration_rejects_authority_expansion_and_unsupported_values(change):
    client, calls = harness()
    with client:
        assert (
            client.put(
                "/api/workers/synthetic/configuration", json=PAYLOAD | change
            ).status_code
            == 422
        )
    assert calls == []


def test_context_read_encodes_source_and_preserves_page_boundaries():
    client, calls = harness()
    with client:
        assert (
            client.get(
                "/api/workers/synthetic/configuration/context/bootstrap%3Aproject_definition?offset=12000&max_chars=500"
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/workers/synthetic/configuration/context/source?max_chars=65537"
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/workers/synthetic/configuration/context/source?offset=-1"
            ).status_code
            == 422
        )
    assert calls == [
        (
            False,
            "GET",
            "/v1/workers/synthetic/configuration/context/bootstrap%3Aproject_definition?offset=12000&max_chars=500",
            None,
        )
    ]


@pytest.mark.parametrize("code", [403, 409, 503])
def test_configuration_preserves_authority_conflict_and_availability_status_without_secret_text(
    code,
):
    app = FastAPI()

    class Client:
        def _request(self, *args, **kwargs):
            response = httpx.Response(
                code,
                json={"detail": "private-provider-value"},
                request=httpx.Request("GET", "https://runtime.invalid"),
            )
            raise httpx.HTTPStatusError(
                "private-provider-value", request=response.request, response=response
            )

    install_worker_configuration_ui_routes(app, lambda request, **kwargs: Client())
    with TestClient(app) as client:
        response = client.put("/api/workers/synthetic/configuration", json=PAYLOAD)
    assert response.status_code == code
    assert response.json() == {"detail": {"code": "configuration_unavailable"}}


def test_configuration_distinguishes_unavailable_selection_from_stale_revision():
    app = FastAPI()

    class Client:
        def _request(self, *args, **kwargs):
            response = httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "selection_unavailable",
                        "debug": "private-value",
                    }
                },
                request=httpx.Request("PUT", "https://runtime.invalid"),
            )
            raise httpx.HTTPStatusError(
                "private-value", request=response.request, response=response
            )

    install_worker_configuration_ui_routes(app, lambda request, **kwargs: Client())
    with TestClient(app) as client:
        response = client.put("/api/workers/synthetic/configuration", json=PAYLOAD)
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "selection_unavailable"}}
