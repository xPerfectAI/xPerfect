from __future__ import annotations

from workers_projects_runtime.models import WorkerResponse


def test_worker_response_exposes_current_run_id_for_active_worker():
    worker = WorkerResponse(
        worker_id="wrk_active",
        project_id="prj_active",
        owner_id="owner-1",
        name="Active Worker",
        role="research",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        execution_mode="host",
        state="running",
        last_run_id="run_active",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    payload = worker.model_dump()

    assert payload["backend"] == "codex-cli"
    assert payload["last_run_id"] == "run_active"
    assert payload["current_run_id"] == "run_active"


def test_worker_response_leaves_current_run_id_empty_for_ready_worker():
    worker = WorkerResponse(
        worker_id="wrk_ready",
        project_id="prj_ready",
        owner_id="owner-1",
        name="Ready Worker",
        role="research",
        profile="claude-code",
        backend="openclaw",
        runtime="claude-code",
        execution_mode="host",
        state="ready",
        last_run_id="run_previous",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    payload = worker.model_dump()

    assert payload["backend"] == "claude-code"
    assert payload["last_run_id"] == "run_previous"
    assert payload["current_run_id"] is None
