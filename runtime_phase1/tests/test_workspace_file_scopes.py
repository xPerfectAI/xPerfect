"""Typed runtime physical scopes own file identity, provenance and write fences."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from workers_projects_runtime.deliverables import candidate_artifact_paths
from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles, artifact_worker_context


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_OWNER_STORAGE_BYTES", raising=False)
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-scope-test-secret")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Shared project", "Keep scopes separate", "codex-cli", tenant_id="tenant")
    workers, scopes = [], {}
    for name in ("one", "two"):
        worker = store.create_worker(project["project_id"], "owner", name, "operator", "codex-cli", "openclaw", "codex-cli", "stub/codex", tenant_id="tenant")
        root = tmp_path / name
        root.mkdir()
        # The raw stored path is not the admitted Files root.
        store.update_worker(worker["worker_id"], workspace_dir=str(tmp_path / "native-home" / name))
        worker = store.get_worker(worker["worker_id"])
        workers.append(worker)
        scopes[worker["worker_id"]] = {"scope_id": "member:" + worker["worker_id"], "kind": "member",
            "workspace_id": "shared-logical-workspace", "member_id": worker["worker_id"], "root": root}
    def resolve(worker_id, *, tenant_id, owner_id):
        assert (tenant_id, owner_id) == ("tenant", "owner")
        return dict(scopes[worker_id])
    files = WorkspaceFiles(store, scope_resolver=resolve)
    try:
        yield files, workers, scopes, tmp_path
    finally:
        store.close()


def listing(files, worker):
    return files.list_files(worker["worker_id"], "tenant", "owner")


def upload(files, worker, content, key="upload"):
    created = files.create_upload("tenant", "owner", draft_id="draft", name="same.txt", size_bytes=len(content), idempotency_key=key)
    async def chunks():
        yield content
    asyncio.run(files.receive("tenant", "owner", created["upload_id"], chunks()))
    result = files.bind(worker["worker_id"], "tenant", "owner", [created["upload_id"]], key, directory="")
    return created["upload_id"], result


def test_member_private_roots_have_distinct_ids_and_cannot_read_each_others_ids(scoped):
    files, workers, scopes, _ = scoped
    for index, worker in enumerate(workers):
        (scopes[worker["worker_id"]]["root"] / "same.txt").write_text(str(index))
    a, b = [listing(files, worker) for worker in workers]
    assert a["scope"]["workspace_id"] == b["scope"]["workspace_id"]
    assert a["scope"]["scope_id"] != b["scope"]["scope_id"]
    assert a["items"][0]["file_id"] != b["items"][0]["file_id"]
    assert a["items"][0]["revision"] != b["items"][0]["revision"]
    assert "root" not in a["scope"]
    assert str(scopes[workers[0]["worker_id"]]["root"]) not in json.dumps(a)
    with pytest.raises(FileAdmissionError) as error:
        files.open_download(workers[1]["worker_id"], "tenant", "owner", a["items"][0]["file_id"])
    assert error.value.status_code == 404
    for worker, item, expected in zip(workers, (a, b), (b"0", b"1")):
        fd, _, _ = files.open_download(worker["worker_id"], "tenant", "owner", item["items"][0]["file_id"])
        try:
            assert os.read(fd, 10) == expected
        finally:
            os.close(fd)


def test_explicit_common_scope_shares_ids_content_and_input_origin(scoped):
    files, workers, scopes, tmp_path = scoped
    common = tmp_path / "common"
    common.mkdir()
    for worker in workers:
        scopes[worker["worker_id"]].update(scope_id="shared-logical-workspace", kind="workspace", root=common)
    _, bound = upload(files, workers[0], b"input")
    assert bound["items"][0]["file_scope_id"] == "shared-logical-workspace"
    a, b = [listing(files, worker) for worker in workers]
    assert a["items"][0]["file_id"] == b["items"][0]["file_id"]
    assert files.control_lock_root(workers[0]) == files.control_lock_root(workers[1])
    for worker in workers:
        assert candidate_artifact_paths(files.artifact_worker(worker)) == []


def test_private_input_does_not_hide_identical_named_output_in_other_member(scoped):
    files, workers, scopes, _ = scoped
    upload(files, workers[0], b"identical bytes")
    other = scopes[workers[1]["worker_id"]]["root"] / "same.txt"
    other.write_bytes(b"identical bytes")
    assert candidate_artifact_paths(files.artifact_worker(workers[0])) == []
    assert candidate_artifact_paths(files.artifact_worker(workers[1])) == [other]
    with files.store._connect() as conn:
        projected = artifact_worker_context(conn, workers[1], files=files.store._workspace_files_adapter)
    assert candidate_artifact_paths(projected) == [other]


def test_native_guard_uses_physical_identity_before_directory_creation(scoped):
    files, workers, scopes, tmp_path = scoped
    scopes[workers[0]["worker_id"]]["root"] = tmp_path / "not-prepared"
    with files.native_start_guard(workers[0]):
        assert not scopes[workers[0]["worker_id"]]["root"].exists()
    assert files.control_lock_root(workers[0]) != files.control_lock_root(workers[1])


@pytest.mark.parametrize("common", [False, True])
def test_mutation_busy_fence_only_blocks_same_physical_scope(scoped, common):
    files, workers, scopes, _ = scoped
    if common:
        scopes[workers[1]["worker_id"]].update(scope_id=scopes[workers[0]["worker_id"]]["scope_id"], root=scopes[workers[0]["worker_id"]]["root"], kind="workspace")
    (scopes[workers[0]["worker_id"]]["root"] / "same.txt").write_bytes(b"output")
    entry = listing(files, workers[0])["items"][0]
    files.store.create_run(workers[1]["worker_id"], workers[1]["project_id"], "Busy")
    files.store.claim_next_queued_run(workers[1]["worker_id"], executor_id="synthetic-scope-test")
    assert files.store.get_active_run(workers[1]["worker_id"])
    if common:
        with pytest.raises(FileAdmissionError, match="editing this workspace"):
            files.mutate(workers[0]["worker_id"], "tenant", "owner", entry["file_id"], revision=entry["revision"], path="moved.txt")
    else:
        assert files.mutate(workers[0]["worker_id"], "tenant", "owner", entry["file_id"], revision=entry["revision"], path="moved.txt")["path"] == "moved.txt"


def test_binding_retry_and_schedule_dispatch_reject_changed_physical_scope(scoped):
    files, workers, scopes, _ = scoped
    upload_id, _ = upload(files, workers[0], b"input")
    schedule = files.store.create_scheduled_run(worker_id=workers[0]["worker_id"], project_id=workers[0]["project_id"],
        tenant_id="tenant", owner_id="owner", instruction="Use input", run_at="2027-01-01T00:00:00+00:00",
        file_manifest=files.manifest("tenant", "owner", [upload_id]))
    stored = files.store.get_schedule(schedule["schedule_id"])
    manifest = json.loads(stored["file_manifest_json"])
    original = scopes[workers[0]["worker_id"]]["scope_id"]
    assert manifest[0]["file_scope_id"] == original
    scopes[workers[0]["worker_id"]]["scope_id"] = "different-scope"
    with pytest.raises(FileAdmissionError, match="scope changed"):
        files.bind(workers[0]["worker_id"], "tenant", "owner", [upload_id], "upload", directory="")
    with pytest.raises(FileAdmissionError, match="scope changed"):
        files.register_run_input_versions(workers[0], manifest)


def test_scope_access_uses_open_fds_and_mkdir_failure_removes_new_empty_directory(scoped):
    files, workers, scopes, _ = scoped
    root = scopes[workers[0]["worker_id"]]["root"]
    seen = []
    def access(worker_id, *, tenant_id, owner_id, scope_id, descriptor, is_directory):
        assert (worker_id, tenant_id, owner_id, scope_id) == (workers[0]["worker_id"], "tenant", "owner", scopes[worker_id]["scope_id"])
        assert is_directory
        seen.append(os.fstat(descriptor).st_ino)
        if len(seen) == 2:
            raise PermissionError("synthetic ACL failure")
    files.scope_access = access
    with pytest.raises(FileAdmissionError, match="access setup failed"):
        files.mkdir(workers[0]["worker_id"], "tenant", "owner", "folder")
    assert len(seen) == 2
    assert not (root / "folder").exists()
    files.scope_access = lambda *args, **kwargs: os.fchmod(kwargs["descriptor"], 0o750)
    assert files.mkdir(workers[0]["worker_id"], "tenant", "owner", "folder")["path"] == "folder"
    assert (root / "folder").stat().st_mode & 0o777 == 0o750


def test_no_resolver_does_not_treat_logical_workspace_id_as_shared_authority(scoped):
    files, workers, _, _ = scoped
    raw = [{**worker, "workspace_id": "same-logical-workspace"} for worker in workers]
    assert files._workspace_key(raw[0]) != files._workspace_key(raw[1])
    files.scope_resolver = None
    for worker in raw:
        scope = files._scope_worker(worker)["_file_scope"]
        assert scope["kind"] == "member"
        assert scope["member_id"] == worker["worker_id"]


def test_resolver_rejects_unknown_public_scope_kind(scoped):
    files, workers, scopes, _ = scoped
    scopes[workers[0]["worker_id"]]["kind"] = "unknown"
    with pytest.raises(FileAdmissionError, match="scope identity"):
        listing(files, workers[0])


def test_new_keep_both_upload_publishes_while_common_member_is_active(scoped):
    files, workers, scopes, _ = scoped
    root = scopes[workers[0]["worker_id"]]["root"]
    scopes[workers[1]["worker_id"]].update(scope_id=scopes[workers[0]["worker_id"]]["scope_id"], root=root, kind="workspace")
    (root / "same.txt").write_bytes(b"existing worker output")
    run = files.store.create_run(workers[1]["worker_id"], workers[1]["project_id"], "Keep working")
    files.store.claim_next_queued_run(workers[1]["worker_id"], executor_id="synthetic-live-scope-test")
    before = files.store.get_active_run(workers[1]["worker_id"])
    assert before["run_id"] == run["run_id"]
    _, bound = upload(files, workers[0], b"new input")
    assert bound["state"] == "available"
    assert bound["items"][0]["path"] == "same-2.txt"
    assert (root / "same.txt").read_bytes() == b"existing worker output"
    assert (root / "same-2.txt").read_bytes() == b"new input"
    after = files.store.get_active_run(workers[1]["worker_id"])
    assert (after["run_id"], after["state"]) == (before["run_id"], before["state"])
    assert files.store.list_runs_for_worker(workers[0]["worker_id"]) == []


def test_artifact_download_and_preview_read_the_admitted_scope_root(scoped):
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app
    from workers_projects_runtime.openclaw_runtime import StubRuntime

    files, workers, scopes, _ = scoped
    root = scopes[workers[0]["worker_id"]]["root"]
    (root / "output.txt").write_bytes(b"admitted root output")
    app = create_app(files.store.db_path, runtime_backend="stub", runtime=StubRuntime())
    app.state.service.files = WorkspaceFiles(app.state.store, scope_resolver=files.scope_resolver)
    try:
        client = TestClient(app)
        base = f"/v1/workers/{workers[0]['worker_id']}/artifacts"
        downloaded = client.get(base + "/download", params={"path": "output.txt"})
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == b"admitted root output"
        opened = client.get(base + "/open", params={"path": "output.txt"})
        assert opened.status_code == 200, opened.text
        assert "admitted root output" in opened.text
    finally:
        app.state.service.shutdown()
