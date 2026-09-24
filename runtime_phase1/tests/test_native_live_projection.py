from __future__ import annotations

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.signed_links import sign_link_params


def test_worker_live_projects_exact_grok_pending_input_and_redacts_read_only_view(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-secret")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-runtime-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    app = create_app(
        str(tmp_path / "runtime.db"),
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    store = app.state.store
    project = store.create_project(
        "owner-a",
        "Native input",
        "Show a pending response",
        "grok-build",
        tenant_id="tenant-alpha",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Grok worker",
        role="research",
        profile="grok-build",
        backend="profiled",
        runtime="grok-build",
        model="grok-exact",
        tenant_id="tenant-alpha",
    )
    active = {
        "run_id": "run-native",
        "worker_id": worker["worker_id"],
        "project_id": project["project_id"],
        "state": "running",
        "active_attempt_id": "attempt-native",
    }
    monkeypatch.setattr(store, "get_active_run", lambda worker_id: dict(active))
    monkeypatch.setattr(
        app.state.service,
        "native_worker_control",
        lambda worker_id, **kwargs: {
            "run_id": kwargs["run_id"],
            "attempt_id": kwargs["attempt_id"],
            "pending_requests": [
                {
                    "request_id": "request-native",
                    "method": "x.ai/ask_user_question",
                    "request": {"questions": [{"question": "private question"}]},
                }
            ],
            "receipts": [{"message_id": "private-receipt", "secret": "must-not-leak"}],
        },
    )

    with TestClient(app) as client:
        owner_headers = {
            "X-WPR-Token": "synthetic-runtime-token",
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "owner-a",
            "X-Viventium-User-Role": "operator",
        }
        owner_live = client.get(
            f"/v1/workers/{worker['worker_id']}/live",
            headers=owner_headers,
        )
        assert owner_live.status_code == 200
        owner_pending = owner_live.json()["native_control"]
        assert owner_pending["run_id"] == "run-native"
        assert owner_pending["attempt_id"] == "attempt-native"
        assert owner_pending["pending_requests"][0]["request"]["questions"]
        assert "receipts" not in owner_pending

        signed_query = sign_link_params(
            kind="worker_view",
            worker_id=worker["worker_id"],
            tenant_id="tenant-alpha",
            owner_id="owner-a",
        )
        signed = client.get(
            f"/v1/workers/{worker['worker_id']}/live",
            params=signed_query,
        )
        assert signed.status_code == 200
        signed_pending = signed.json()["native_control"]
        assert signed_pending["read_only"] is True
        assert signed_pending["pending_requests"] == [
            {"request_id": "request-native", "method": "x.ai/ask_user_question"}
        ]
        assert "private question" not in signed.text
        assert "private-receipt" not in signed.text

        # The direct runtime read uses the same safe contract as the authorized live view.
        viewer_headers = {**owner_headers, "X-Viventium-User-Role": "viewer"}
        signed_direct = client.get(
            f"/v1/workers/{worker['worker_id']}/native-control",
            headers=viewer_headers,
        )
        assert signed_direct.status_code == 200, signed_direct.text
        assert signed_direct.json()["pending_requests"] == [
            {"request_id": "request-native", "method": "x.ai/ask_user_question"}
        ]
        assert "private question" not in signed_direct.text
        assert "private-receipt" not in signed_direct.text

        # A read that crosses an attempt transition is unavailable, never relabeled.
        monkeypatch.setattr(
            app.state.service,
            "native_worker_control",
            lambda worker_id, **kwargs: {
                "run_id": "run-old",
                "attempt_id": "attempt-old",
                "pending_requests": [{"request_id": "old", "method": "session/request_permission"}],
            },
        )
        stale = client.get(
            f"/v1/workers/{worker['worker_id']}/live",
            headers=owner_headers,
        )
        assert stale.status_code == 200
        assert stale.json()["native_control"]["available"] is False
