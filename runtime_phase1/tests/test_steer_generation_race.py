"""Exact lifecycle claim races preserve both useful results and newer work."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import RuntimeGenerationChangedError, StubRuntime
from test_phase3_controls import _exact_terminal_generation, _worker_with_run


@pytest.mark.parametrize("winner", ["completion", "new_run", "control"])
def test_legacy_steer_reconciles_only_exact_finished_generation(tmp_path, monkeypatch, winner):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    runtime = StubRuntime()
    store, previous_service, worker, original = _worker_with_run(
        tmp_path, runtime, background=False,
    )
    previous_service.shutdown()
    app = create_app(str(tmp_path / "runtime.db"), runtime=runtime,
                     reconcile_on_startup=False)
    with TestClient(app) as client:
        service = app.state.service
        store = service.store
        # This deterministic admission case does not execute a new provider job.
        monkeypatch.setattr(service, "_ensure_worker_processor", lambda *_args: None)
        monkeypatch.setattr(runtime, "interrupt_worker", lambda *_args, **_kwargs: pytest.fail("the changed generation must not be interrupted"))
        original_claim = store.try_claim_worker_compute_release
        winner_record = {}

        def race_at_exact_claim(*args, **kwargs):
            if not winner_record:
                result = store.finalize_run_if_state(
                    original["run_id"], "running", "completed",
                    output_text="Original saved result",
                    **_exact_terminal_generation(store, original["run_id"]),
                )
                assert result is not None
                winner_record["completed"] = result
                if winner == "new_run":
                    winner_record["new_run"] = store.create_run(
                        worker["worker_id"], worker["project_id"],
                        "Newer accepted work", state="queued",
                    )
                elif winner == "control":
                    current = store.get_worker(worker["worker_id"])
                    winner_record["claim"] = original_claim(
                        worker["worker_id"], expected_updated_at=current["updated_at"],
                        expected_last_run_id=current["last_run_id"],
                        expected_state=current["state"], expected_container_id="",
                        owner="competing-control", ttl_s=300, kind="idle",
                    )
                    assert winner_record["claim"] is not None
            return original_claim(*args, **kwargs)

        monkeypatch.setattr(store, "try_claim_worker_compute_release", race_at_exact_claim)
        draft = "Keep this exact new instruction"
        response = client.post(f"/v1/workers/{worker['worker_id']}/steer", json={"message": draft})
        old = store.get_run(original["run_id"])
        assert old["state"] == "completed"
        assert old["output_text"] == "Original saved result"
        runs = store.list_runs_for_worker(worker["worker_id"])
        if winner == "completion":
            assert response.status_code == 202, response.text
            accepted = response.json()
            assert accepted["worker_id"] == worker["worker_id"]
            assert accepted["run_id"] != original["run_id"]
            assert draft in accepted["instruction"]
            assert len(runs) == 2
        else:
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == {
                "code": "active_work_generation_changed",
                "message": "Current work changed. Check its status and try again.",
                "retryable": True,
            }
            assert not any(draft in run["instruction"] for run in runs)
            if winner == "new_run":
                newer = store.get_run(winner_record["new_run"]["run_id"])
                assert newer["state"] == "queued"
                assert store.get_worker(worker["worker_id"])["last_run_id"] == newer["run_id"]
            else:
                assert store.get_worker(worker["worker_id"])["compute_release_token"] == winner_record["claim"]["token"]


@pytest.mark.parametrize("control", ["pause", "resume", "interrupt", "steer"])
def test_generation_conflict_uses_shared_typed_http_handler(tmp_path, monkeypatch, control):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub")
    with TestClient(app) as client:
        store = app.state.service.store
        project = store.create_project("demo-owner", "Synthetic conflict", "Preserve exact work", "openclaw-general")
        worker = store.create_worker(project_id=project["project_id"], owner_id="demo-owner", name="Synthetic worker", role="operator", profile="openclaw-general", backend="openclaw", runtime="openclaw-stub", model="stub-model")
        def conflict(*_args, **_kwargs):
            raise RuntimeGenerationChangedError()
        monkeypatch.setattr(app.state.service, f"{control}_worker", conflict)
        response = client.post(f"/v1/workers/{worker['worker_id']}/{control}", json={"message":"Preserved draft"} if control == "steer" else None)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "active_work_generation_changed"
        assert "Current work changed" in response.json()["detail"]["message"]
        assert store.list_runs_for_worker(worker["worker_id"]) == []
