"""Assignment retries read accepted work before changing worker configuration or lifecycle."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.store import AssignmentIdempotencyConflictError


@pytest.fixture
def subject(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub")
    service = app.state.service
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={
            "owner_id": "demo-owner", "title": "Synthetic assignment retry",
            "goal": "Retain the accepted instruction", "default_worker_profile": "codex-cli",
        }).json()
        response = client.post(f"/v1/projects/{project['project_id']}/workers", json={
            "owner_id": "demo-owner", "name": "Synthetic worker", "role": "operator",
            "profile": "codex-cli", "start_synchronously": True,
            "bootstrap_bundle": {"env": {"SYNTHETIC_REFERENCE": "original"}},
        })
        assert response.status_code == 201, response.text
        worker_id = response.json()["worker_id"]
        path = f"/v1/workers/{worker_id}/assign"
        headers = {"Idempotency-Key": "synthetic-assignment-key"}
        accepted = client.post(path, headers=headers, json={"instruction": "Original accepted job"})
        assert accepted.status_code == 202, accepted.text
        yield client, service, worker_id, accepted.json()["run_id"], path, headers


def snapshot(service, worker_id):
    worker = service.store.get_worker(worker_id)
    return {key: worker.get(key) for key in (
        "state", "bootstrap_bundle_json", "model", "last_run_id", "updated_at",
    )}


def test_conflicting_retry_returns_409_without_changing_worker(subject):
    client, service, worker_id, run_id, path, headers = subject
    before = snapshot(service, worker_id)
    response = client.post(path, headers=headers, json={
        "instruction": "Different rejected job", "effort": "low",
        "bootstrap_bundle": {"env": {"SYNTHETIC_REFERENCE": "must-not-persist"}},
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "assignment_idempotency_conflict"
    assert snapshot(service, worker_id) == before
    assert [row["run_id"] for row in service.store.list_runs_for_worker(worker_id)] == [run_id]


def test_same_instruction_replays_without_replacing_configuration(subject, monkeypatch):
    client, service, worker_id, run_id, path, headers = subject
    service.store.update_run(run_id, state="completed", output_text="Saved result")
    before = snapshot(service, worker_id)
    def unexpected(*_args, **_kwargs):
        raise AssertionError("An accepted retry must not preflight or resume native execution")
    monkeypatch.setattr(service, "_queued_runtime_preflight", unexpected)
    monkeypatch.setattr(service, "resume_worker", unexpected)
    response = client.post(path, headers=headers, json={
        "instruction": "Original accepted job",
    })
    assert response.status_code == 202, response.text
    assert response.json()["run_id"] == run_id
    assert response.json()["output_text"] == "Saved result"
    assert snapshot(service, worker_id) == before


def test_paused_assignment_retry_does_not_resume_current_work(subject, monkeypatch):
    client, service, worker_id, run_id, path, headers = subject
    service.store.update_run(run_id, state="paused")
    service.store.update_worker(worker_id, state="paused")
    before = snapshot(service, worker_id)
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Reading a paused reservation must not resume or start work")
    monkeypatch.setattr(service, "resume_worker", unexpected)
    monkeypatch.setattr(service, "_ensure_worker_processor", unexpected)
    response = client.post(path, headers=headers, json={"instruction": "Original accepted job"})
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "paused"
    assert snapshot(service, worker_id) == before


def test_simultaneous_same_key_replays_keep_one_run_and_original_setup(subject):
    client, service, worker_id, run_id, path, headers = subject
    service.store.update_run(run_id, state="completed", output_text="Saved result")
    before = snapshot(service, worker_id)
    def replay(index):
        return client.post(path, headers=headers, json={
            "instruction": "Original accepted job",
        })
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(replay, range(8)))
    assert all(response.status_code == 202 for response in results)
    assert {response.json()["run_id"] for response in results} == {run_id}
    assert snapshot(service, worker_id) == before


def test_store_conflict_keeps_legacy_value_error_contract(subject):
    _client, service, worker_id, run_id, _path, _headers = subject
    worker = service.store.get_worker(worker_id)
    with pytest.raises(AssignmentIdempotencyConflictError, match="different instruction") as failure:
        service.store.create_idempotent_run(
            run_id=run_id, worker_id=worker_id, project_id=worker["project_id"],
            instruction="Different rejected job",
        )
    assert isinstance(failure.value, ValueError)
    assert json.loads(worker["bootstrap_bundle_json"])["env"]["SYNTHETIC_REFERENCE"] == "original"


@pytest.mark.parametrize("changed", [
    {"bootstrap_bundle": {"env": {"SYNTHETIC_REFERENCE": "changed"}}},
    {"effort": "low"},
    {"continuationContext": {"version": 1, "baseInstruction": "Changed continuation", "guidance": []}},
])
def test_changed_execution_input_conflicts_before_effects(subject, changed):
    client, service, worker_id, run_id, path, headers = subject
    before = snapshot(service, worker_id)
    response = client.post(path, headers=headers, json={"instruction": "Original accepted job", **changed})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "assignment_idempotency_conflict"
    assert snapshot(service, worker_id) == before
    assert len(service.store.list_runs_for_worker(worker_id)) == 1


def test_first_admission_concurrency_keeps_one_complete_request(subject):
    client, service, worker_id, _run_id, path, _headers = subject
    headers = {"Idempotency-Key": "synthetic-concurrent-first"}
    body = {"instruction": "One concurrent new job", "effort": "high",
            "bootstrap_bundle": {"env": {"SYNTHETIC_REFERENCE": "accepted"}}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _index: client.post(path, headers=headers, json=body), range(8)))
    assert all(response.status_code == 202 for response in responses)
    ids = {response.json()["run_id"] for response in responses}
    assert len(ids) == 1 and len(service.store.list_runs_for_worker(worker_id)) == 2
    assert {response.json()["effort"] for response in responses} == {"high"}
    record = service.store.get_run(ids.pop())
    identity = json.loads(record["assignment_request_json"])
    assert identity["version"] == 1 and len(identity["sha256"]) == 64
    assert identity["effort"] == "high"
    assert "accepted" not in record["assignment_request_json"]


def test_completed_replay_returns_accepted_effort(subject):
    client, service, worker_id, _run_id, path, _headers = subject
    headers = {"Idempotency-Key": "synthetic-accepted-effort"}
    body = {"instruction": "One high effort job", "effort": "high"}
    accepted = client.post(path, headers=headers, json=body)
    run_id = accepted.json()["run_id"]
    service.store.update_run(run_id, state="completed", output_text="Saved high effort result")
    replay = client.post(path, headers=headers, json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["effort"] == "high" and replay.json()["run_id"] == run_id
    changed = client.post(path, headers=headers, json={**body, "effort": "low"})
    assert changed.status_code == 409


def test_legacy_rich_retry_uses_read_only_assignment_recovery(subject):
    client, service, worker_id, run_id, path, headers = subject
    with service.store._connect() as connection:
        connection.execute("UPDATE runs SET assignment_request_json='' WHERE run_id=?", (run_id,))
    before = snapshot(service, worker_id)
    simple = client.post(path, headers=headers, json={"instruction": "Original accepted job"})
    assert simple.status_code == 202 and simple.json()["effort"] == ""
    richer = client.post(path, headers=headers, json={"instruction": "Original accepted job", "effort": "high"})
    assert richer.status_code == 409
    saved = client.get(f"/v1/workers/{worker_id}/assignments/by-idempotency/synthetic-assignment-key")
    assert saved.status_code == 200 and saved.json()["run_id"] == run_id
    assert snapshot(service, worker_id) == before
