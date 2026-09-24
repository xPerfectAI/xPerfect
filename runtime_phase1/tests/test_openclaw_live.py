from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app


pytestmark = pytest.mark.skipif(
    os.environ.get("WPR_RUN_OPENCLAW_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="Set WPR_RUN_OPENCLAW_LIVE_TESTS=1 to run live OpenClaw integration tests",
)


def wait_for_run(client: TestClient, run_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(1.0)
    raise AssertionError(f"Run {run_id} did not settle within {timeout}s")


def test_live_openclaw_worker_can_boot_and_resume(tmp_path):
    db_path = tmp_path / "runtime-live.db"
    client = TestClient(create_app(str(db_path), runtime_backend="openclaw"))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Live OpenClaw",
            "goal": "Prove the standalone worker runtime can boot a real OpenClaw-backed worker.",
            "default_worker_profile": "openclaw-general",
        },
    ).json()

    worker_resp = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Live Worker",
            "role": "research",
            "profile": "openclaw-general",
            "backend": "openclaw",
        },
    )
    assert worker_resp.status_code == 201
    worker = worker_resp.json()
    assert worker["state"] == "ready"
    assert worker["session_key"].endswith(worker["worker_id"])
    live = client.get(f"/v1/workers/{worker['worker_id']}/live").json()
    assert live["runtime_details"]["mode"] == "workstation-desktop"
    assert live["runtime_details"]["view_url"].startswith("http://127.0.0.1:")

    run_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Reply with exactly WORKERS_PROJECTS_RUNTIME_OK and no other text."},
    )
    assert run_resp.status_code == 202
    run = wait_for_run(client, run_resp.json()["run_id"])
    assert run["state"] == "completed", run
    assert "WORKERS_PROJECTS_RUNTIME_OK" in run["output_text"], run["output_text"]

    pause_resp = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert pause_resp.status_code == 202
    assert pause_resp.json()["state"] == "paused"

    resume_resp = client.post(f"/v1/workers/{worker['worker_id']}/resume")
    assert resume_resp.status_code == 202
    assert resume_resp.json()["state"] == "ready"

    resume_run_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/message",
        json={"message": "Reply with exactly RESUME_OK and no other text."},
    )
    assert resume_run_resp.status_code == 202
    resume_run = wait_for_run(client, resume_run_resp.json()["run_id"])
    assert resume_run["state"] == "completed", resume_run
    assert "RESUME_OK" in resume_run["output_text"], resume_run["output_text"]
