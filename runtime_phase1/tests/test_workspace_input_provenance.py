"""Accepted user input versions must not become worker deliverables on upload."""
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from workers_projects_runtime.api import create_app
from workers_projects_runtime.deliverables import candidate_artifact_paths, deliverable_payload, _matches_input_version
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.run_evidence import _artifact_inventory
from workers_projects_runtime.store import Store, work_artifact_observation
from workers_projects_runtime.workspace_files import WorkspaceFiles, materialize_managed_file


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_OWNER_STORAGE_BYTES", raising=False)
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-test-provenance-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "local")
    class Runtime(StubRuntime):
        def _runtime_info(self, worker, *, pid):
            result = super()._runtime_info(worker, pid=pid)
            result.workspace_dir = str(tmp_path / worker["worker_id"] / "workspace")
            result.state_dir = str(tmp_path / worker["worker_id"] / "state")
            return result
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=Runtime())
    client = TestClient(app)
    project = client.post("/v1/projects", json={"owner_id": "demo-owner", "title": "Files", "goal": "Preserve file origin"}).json()
    worker = client.post(f"/v1/projects/{project['project_id']}/workers", json={
        "owner_id": "demo-owner", "name": "Files", "role": "operator", "profile": "codex-cli", "bootstrap_profile": "none"}).json()
    root = Path(worker["workspace_dir"])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield app, client, worker, root
    finally:
        app.state.service.shutdown()


def upload(workspace, relative, content, *, attach=True, request_key=None):
    app, client, worker, _ = workspace
    created = client.post("/v1/file-uploads", json={"draft_id": "draft", "name": Path(relative).name,
        "relative_path": relative, "size_bytes": len(content), "idempotency_key": request_key or relative})
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    response = client.put(f"/v1/file-uploads/{upload_id}/content", content=content)
    assert response.status_code == 200, response.text
    if attach:
        bound = client.post(f"/v1/workers/{worker['worker_id']}/files", json={
            "upload_ids": [upload_id], "idempotency_key": request_key or relative, "directory": ""})
        assert bound.status_code == 201, bound.text
    return upload_id


def current_worker(workspace):
    app, _, worker, _ = workspace
    return app.state.service.files.artifact_worker(app.state.store.get_worker(worker["worker_id"]))


def test_default_attached_input_keeps_exact_path_and_shows_readable_name(workspace):
    _, client, worker, _ = workspace
    upload_id = upload(workspace, "notes/brief.txt", b"synthetic input", attach=False)
    bound = client.post(f"/v1/workers/{worker['worker_id']}/files", json={
        "upload_ids": [upload_id], "idempotency_key": "readable-input",
    })
    assert bound.status_code == 201, bound.text
    base = f"/v1/workers/{worker['worker_id']}/files"
    root = client.get(base).json()
    assert next(item for item in root["items"] if item["path"] == "inputs")["name"] == "inputs"
    inputs = client.get(base, params={"directory": "inputs"}).json()
    folder = next(item for item in inputs["items"] if item["path"] == f"inputs/{upload_id}")
    assert folder["display_name"] == "notes/brief.txt"
    inside = client.get(base, params={"directory": folder["path"]}).json()
    assert inside["directory_display_name"] == "notes/brief.txt"
    nested = client.get(base, params={"directory": folder["path"] + "/notes"}).json()
    assert nested["items"][0]["path"] == f"inputs/{upload_id}/notes/brief.txt"
    assert nested["items"][0]["name"] == "brief.txt"


def test_watch_upload_does_not_replace_completed_output_or_create_run(workspace):
    app, client, worker, root = workspace
    (root / "fixture.txt").write_bytes(b"worker output")
    run = app.state.store.create_run(worker["worker_id"], worker["project_id"], "Prepare output")
    app.state.store.finalize_run(run["run_id"], state="completed", output_text="Delivered")
    before = client.get(f"/v1/workers/{worker['worker_id']}/live").json()
    assert before["deliverable"]["workspace_path"] == "fixture.txt"
    for relative, content in [("empty.txt", b""), ("notes.txt", b"input notes"),
                              ("binary.bin", bytes(range(256))), ("Folder/data.csv", b"a,b\n1,2\n"),
                              ("page.html", b"<html>user input</html>"), ("image.png", b"synthetic image")]:
        upload(workspace, relative, content)
    after = client.get(f"/v1/workers/{worker['worker_id']}/live").json()
    assert after["latest_run"]["run_id"] == before["latest_run"]["run_id"]
    assert after["latest_run"]["state"] == "completed"
    assert len(after["runs"]) == len(before["runs"])
    assert after["active_run"] is None
    assert after["deliverable"]["workspace_path"] == "fixture.txt"
    assert [item["path"] for item in after["artifacts"]["items"]] == ["fixture.txt"]
    assert after["artifacts"]["latest_image_name"] is None
    for params in ({}, {"limit": 100}):
        response = client.get(f"/v1/workers/{worker['worker_id']}/artifacts", params=params)
        assert [item["path"] for item in response.json()["items"]] == ["fixture.txt"]
    assert len(client.get(f"/v1/workers/{worker['worker_id']}/files").json()["items"]) == 7
    projected = current_worker(workspace)
    assert [path.name for path in candidate_artifact_paths(projected)] == ["fixture.txt"]
    assert [row["path"] for row in _artifact_inventory(root, worker=projected)["items"]] == ["fixture.txt"]
    # Unchanged revisions use the bounded cache on subsequent polls.
    hits = _matches_input_version.cache_info().hits
    candidate_artifact_paths(projected)
    assert _matches_input_version.cache_info().hits > hits


def test_move_reload_undo_preserve_input_identity_and_changed_bytes_become_output(workspace):
    app, client, worker, root = workspace
    upload(workspace, "source.txt", b"original")
    endpoint = f"/v1/workers/{worker['worker_id']}/files"
    item = client.get(endpoint).json()["items"][0]
    moved = client.patch(endpoint + "/" + item["file_id"], json={"path": "renamed.txt", "revision": item["revision"]})
    assert moved.status_code == 200, moved.text
    assert deliverable_payload(current_worker(workspace), None, "") is None
    fresh_store = Store(app.state.store.db_path)
    try:
        fresh_files = WorkspaceFiles(fresh_store)
        projected = fresh_files.artifact_worker(fresh_store.get_worker(worker["worker_id"]))
        assert deliverable_payload(projected, None, "") is None
        renamed = fresh_files.list_files(worker["worker_id"], "local", "demo-owner")["items"][0]
        assert renamed["file_id"] == item["file_id"]
        removed = fresh_files.mutate(worker["worker_id"], "local", "demo-owner", item["file_id"], revision=renamed["revision"], delete=True)
        fresh_files.undo(worker["worker_id"], "local", "demo-owner", item["file_id"], removed["undo_id"])
        assert deliverable_payload(fresh_files.artifact_worker(worker), None, "") is None
    finally:
        fresh_store.close()
    original = (root / "renamed.txt").stat()
    (root / "renamed.txt").write_bytes(b"modified")
    os.utime(root / "renamed.txt", ns=(original.st_atime_ns, original.st_mtime_ns))
    projected = current_worker(workspace)
    assert deliverable_payload(projected, {"run_id": "run"}, "")["workspace_path"] == "renamed.txt"
    assert app.state.service._completion_deliverable(worker, {"run_id": "run"}, "")["workspace_path"] == "renamed.txt"
    (root / "renamed.txt").write_bytes(b"original")
    assert deliverable_payload(current_worker(workspace), None, "") is None


def test_unmodified_inputs_do_not_become_terminal_artifact_observations(workspace):
    app, _, worker, _ = workspace
    upload(workspace, "data.csv", b"a,b\n1,2\n")
    projected = current_worker(workspace)
    assert app.state.service._completion_deliverable(worker, {"run_id": "run"}, "Completed") is None
    assert work_artifact_observation(projected, {"run_id": "run"}, output_text="Completed", error_text="")["available"] is False
    run = app.state.store.create_run(worker["worker_id"], worker["project_id"], "Process input")
    result = app.state.store.finalize_run(run["run_id"], state="completed", output_text="Completed")
    assert result["state"] == "completed"


def test_scheduled_input_context_is_typed_and_scope_specific(workspace):
    app, _, worker, root = workspace
    upload_id = upload(workspace, "scheduled.csv", b"input", attach=False)
    files = app.state.service.files
    manifest = files.manifest("local", "demo-owner", [upload_id])
    scheduled = app.state.store.create_scheduled_run(
        worker_id=worker["worker_id"], project_id=worker["project_id"], tenant_id="local", owner_id="demo-owner",
        instruction="Use scheduled input", run_at="2027-01-01T00:00:00+00:00", file_manifest=manifest,
    )
    assert app.state.store.claim_schedule(scheduled["schedule_id"])
    run, _ = app.state.store.create_or_get_run_for_schedule(scheduled["schedule_id"])
    manifest = app.state.store.get_run_file_manifest(run["run_id"], worker_id=worker["worker_id"], tenant_id="local", owner_id="demo-owner")
    files.register_run_input_versions(worker, manifest)
    entries = files.bootstrap_entries("local", "demo-owner", manifest)
    for entry in entries:
        materialize_managed_file(root, entry, worker)
    native_worker = {**worker, "bootstrap_bundle_json": json.dumps({"files": entries})}
    assert candidate_artifact_paths(native_worker) == []
    assert candidate_artifact_paths(current_worker(workspace)) == []
    selected = root / manifest[0]["path"]
    selected.write_bytes(b"edited")
    assert candidate_artifact_paths(native_worker) == [selected]
    assert candidate_artifact_paths(current_worker(workspace)) == [selected]


def test_existing_accepted_manifest_without_version_index_still_excludes_input(workspace):
    app, client, worker, _ = workspace
    upload(workspace, "legacy.txt", b"accepted")
    with app.state.store._connect() as conn:
        conn.execute("UPDATE workspace_file_entries SET version_id='' WHERE workspace_key=?", (worker["worker_id"],))
    assert deliverable_payload(current_worker(workspace), None, "") is None
    endpoint = f"/v1/workers/{worker['worker_id']}/files"
    item = client.get(endpoint).json()["items"][0]
    moved = client.patch(endpoint + "/" + item["file_id"], json={"path": "moved.txt", "revision": item["revision"]})
    assert moved.status_code == 200, moved.text
    assert deliverable_payload(current_worker(workspace), None, "") is None


def test_input_version_index_does_not_classify_another_owner_or_workspace(workspace):
    app, _, worker, root = workspace
    upload(workspace, "source.txt", b"original")
    other_root = root.parent / "other-workspace"
    other_root.mkdir()
    (other_root / "source.txt").write_bytes(b"original")
    other = {**worker, "worker_id": "other-worker", "owner_id": "other-owner", "workspace_dir": str(other_root)}
    projected = app.state.service.files.artifact_worker(other)
    assert candidate_artifact_paths(projected) == [other_root / "source.txt"]


def test_native_delete_then_watch_same_name_upload_creates_new_input_identity(workspace):
    app, client, worker, root = workspace
    upload(workspace, "source.txt", b"first accepted")
    endpoint = f"/v1/workers/{worker['worker_id']}/files"
    first = client.get(endpoint).json()["items"][0]
    (root / "source.txt").unlink()
    upload(workspace, "source.txt", b"second accepted", request_key="second")
    second = client.get(endpoint).json()["items"][0]
    assert second["file_id"] != first["file_id"]
    assert second["path"] == first["path"]
    assert deliverable_payload(current_worker(workspace), None, "") is None
    assert client.get(endpoint + "/" + first["file_id"] + "/content").status_code == 404
    with app.state.store._connect() as conn:
        old = conn.execute("SELECT deleted,version_id FROM workspace_file_entries WHERE file_id=?", (first["file_id"],)).fetchone()
    assert old["deleted"] == 1
    assert old["version_id"]


def test_new_worker_output_at_moved_input_old_path_is_not_hidden_by_bootstrap(workspace):
    _, client, worker, root = workspace
    upload(workspace, "source.txt", b"original")
    endpoint = f"/v1/workers/{worker['worker_id']}/files"
    first = client.get(endpoint).json()["items"][0]
    response = client.patch(endpoint + "/" + first["file_id"], json={"path": "moved.txt", "revision": first["revision"]})
    assert response.status_code == 200
    (root / "source.txt").write_bytes(b"original")
    assert candidate_artifact_paths(current_worker(workspace)) == [root / "source.txt"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_replaced_input_link_is_not_promoted_as_worker_output(workspace, kind):
    _, _, _, root = workspace
    upload(workspace, "source.txt", b"original")
    outside = root.parent / "outside.txt"
    outside.write_bytes(b"outside private bytes")
    (root / "source.txt").unlink()
    if kind == "symlink":
        (root / "source.txt").symlink_to(outside)
    else:
        os.link(outside, root / "source.txt")
    assert candidate_artifact_paths(current_worker(workspace)) == []
