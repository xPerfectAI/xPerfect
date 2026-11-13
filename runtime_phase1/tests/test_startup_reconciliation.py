import pytest

from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


def test_stale_queued_recovery_cannot_overwrite_completed_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Recovery", "Synthetic restart", "codex-cli")
    worker = store.create_worker(project_id=project["project_id"], owner_id="owner",
                                 name="Worker", role="general", profile="codex-cli",
                                 backend="openclaw", runtime="codex-cli", model="example-model")
    store.update_worker_state(worker["worker_id"], "starting")
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic", state="queued")
    observed = store.get_worker(worker["worker_id"])
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    real_has_queued = store.has_queued_runs
    dispatches = []

    def completed_after_observation(worker_id):
        queued = real_has_queued(worker_id)
        if queued:
            store.transition_run_if_state(run["run_id"], "queued", "completed", output_text="Synthetic result")
            store.update_worker(worker_id, state="ready", last_run_id=run["run_id"])
        return queued

    monkeypatch.setattr(store, "has_queued_runs", completed_after_observation)
    monkeypatch.setattr(service, "_ensure_worker_processor", dispatches.append)
    try:
        service._reconcile_worker_row(observed)
        assert store.get_worker(worker["worker_id"])["state"] == "ready"
        assert store.get_run(run["run_id"])["state"] == "completed"
        assert dispatches == []
    finally:
        service.shutdown()


@pytest.mark.parametrize("change", ["completed", "paused", "closed", "claimed", "release", "last_run"])
def test_queued_recovery_observation_cannot_cross_newer_state(tmp_path, change):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Recovery", "Synthetic", "codex-cli")
    worker = store.create_worker(project_id=project["project_id"], owner_id="owner",
                                 name="Worker", role="general", profile="codex-cli",
                                 backend="openclaw", runtime="codex-cli", model="example-model")
    store.update_worker_state(worker["worker_id"], "ready")
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic", state="queued")
    observed = store.get_worker(worker["worker_id"])
    if change == "completed":
        store.transition_run_if_state(run["run_id"], "queued", "completed")
    elif change == "claimed":
        claimed = store.claim_next_queued_run(worker["worker_id"], executor_id="synthetic-executor")
        assert claimed is not None and claimed["state"] == "claimed"
    elif change == "paused":
        store.update_worker_state(worker["worker_id"], "paused")
    elif change == "closed":
        store.update_worker_state(worker["worker_id"], "terminated")
    elif change == "release":
        store.update_worker(worker["worker_id"], compute_release_token="synthetic-release")
    else:
        store.update_worker(worker["worker_id"], last_run_id="synthetic-newer-run")
    current = store.get_worker(worker["worker_id"])
    assert store.reconcile_queued_worker_start(observed) is None
    assert store.get_worker(worker["worker_id"]) == current


def test_current_queued_recovery_is_projected_without_changing_run(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Recovery", "Synthetic", "codex-cli")
    worker = store.create_worker(project_id=project["project_id"], owner_id="owner",
                                 name="Worker", role="general", profile="codex-cli",
                                 backend="openclaw", runtime="codex-cli", model="example-model")
    store.update_worker_state(worker["worker_id"], "ready")
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic", state="queued")
    observed = store.get_worker(worker["worker_id"])
    before_run = store.get_run(run["run_id"])
    result = store.reconcile_queued_worker_start(observed)
    assert result["state"] == "starting"
    assert store.get_run(run["run_id"]) == before_run


def test_benign_concurrent_worker_update_does_not_strand_queued_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Recovery", "Synthetic", "codex-cli")
    worker = store.create_worker(project_id=project["project_id"], owner_id="owner", name="Worker",
        role="general", profile="codex-cli", backend="openclaw", runtime="codex-cli", model="example-model")
    store.update_worker_state(worker["worker_id"], "ready")
    store.create_run(worker["worker_id"], project["project_id"], "Synthetic", state="queued")
    observed = store.get_worker(worker["worker_id"])
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    real = store.has_queued_runs
    updated = False
    def concurrent_update(worker_id):
        nonlocal updated
        result = real(worker_id)
        if result and not updated:
            updated = True
            store.update_worker(worker_id, name="Updated name")
        return result
    dispatches = []
    monkeypatch.setattr(store, "has_queued_runs", concurrent_update)
    monkeypatch.setattr(service, "_ensure_worker_processor", dispatches.append)
    try:
        service._reconcile_worker_row(observed)
        assert store.get_worker(worker["worker_id"])["state"] == "ready"
        assert store.get_worker(worker["worker_id"])["name"] == "Updated name"
        assert dispatches == [worker["worker_id"]]
    finally:
        service.shutdown()


@pytest.mark.parametrize("concurrent_change", ["metadata", "closed", "release"])
def test_released_paused_recovery_rechecks_after_cas_miss(tmp_path, monkeypatch, concurrent_change):
    from workers_projects_runtime.store import utc_now
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Recovery", "Synthetic", "codex-cli")
    worker = store.create_worker(project_id=project["project_id"], owner_id="owner", name="Worker",
        role="general", profile="codex-cli", backend="openclaw", runtime="codex-cli", model="example-model")
    wid = worker["worker_id"]
    store.update_worker(wid, state="paused", compute_released_at=utc_now())
    run = store.create_run(wid, project["project_id"], "Synthetic", state="queued")
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    original = store.reconcile_queued_worker_start
    observations = []
    def update_during_cas(observed):
        if not observations:
            changes = {"metadata": {"name": "Updated name"}, "closed": {"state": "terminated"},
                       "release": {"compute_release_token": "synthetic-release"}}
            store.update_worker(wid, **changes[concurrent_change])
        observations.append(observed)
        return original(observed)
    monkeypatch.setattr(store, "reconcile_queued_worker_start", update_during_cas)
    dispatches = []
    monkeypatch.setattr(service, "_ensure_worker_processor", dispatches.append)
    try:
        service._reconcile_worker_row(store.get_worker(wid))
        current = store.get_worker(wid)
        if concurrent_change == "metadata":
            assert len(observations) == 2
            assert current["state"] == "starting" and current["name"] == "Updated name"
            assert dispatches == [wid]
        else:
            assert len(observations) == 1 and dispatches == []
            assert current["state"] == ("terminated" if concurrent_change == "closed" else "paused")
        assert store.get_run(run["run_id"])["state"] == "queued"
    finally:
        service.shutdown()
