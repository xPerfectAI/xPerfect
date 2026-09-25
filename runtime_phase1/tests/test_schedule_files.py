"""Durable scheduled inputs retain exact versions through dispatch and recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest

from workers_projects_runtime.models import ScheduleRunRequest
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import FileAdmissionError


TENANT = "tenant-one"
OWNER = "owner-one"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-schedule-files-secret"
    )
    monkeypatch.delenv("GLASSHIVE_OWNER_STORAGE_BYTES", raising=False)
    store = Store(str(tmp_path / "runtime.db"))
    result = WorkersProjectsService(
        store,
        StubRuntime(),
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    yield result
    result.shutdown()


def _worker(service):
    project = service.store.create_project(
        OWNER,
        "Scheduled files",
        "Use accepted input versions",
        "codex-cli",
        tenant_id=TENANT,
    )
    return service.store.create_worker(
        project_id=project["project_id"],
        owner_id=OWNER,
        tenant_id=TENANT,
        name="File worker",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )


def _upload(service, data, *, owner=OWNER, relative_path=""):
    upload = service.files.create_upload(
        TENANT,
        owner,
        draft_id="scheduled-files",
        name="brief.txt",
        size_bytes=len(data),
        idempotency_key=uuid.uuid4().hex,
        relative_path=relative_path,
    )

    async def chunks():
        for offset in range(0, len(data), 2):
            yield data[offset : offset + 2]

    return asyncio.run(
        service.files.receive(TENANT, owner, upload["upload_id"], chunks())
    )


def _schedule(service, worker, *uploads):
    return service.schedule_run(
        worker["worker_id"],
        "Use the accepted files",
        run_at="2027-01-02T03:04:05+00:00",
        file_upload_ids=[item["upload_id"] for item in uploads],
    )


def _past_expiry(store):
    with store._connect() as conn:
        conn.execute("UPDATE workspace_file_uploads SET expires_at = 1")


def _manifest(store, run, worker):
    return store.get_run_file_manifest(
        run["run_id"],
        worker_id=worker["worker_id"],
        tenant_id=TENANT,
        owner_id=OWNER,
    )


def _dispatch(store, scheduled):
    assert store.claim_schedule(scheduled["schedule_id"])
    run, created = store.create_or_get_run_for_schedule(scheduled["schedule_id"])
    assert created
    return run


def test_two_schedules_keep_exact_same_name_inputs_after_expiry_and_restart(service):
    worker = _worker(service)
    first = _upload(service, b"first bytes")
    second = _upload(service, b"different second bytes")
    schedule_one = _schedule(service, worker, first)
    schedule_two = _schedule(service, worker, second)
    # Accepting files does not change mutable worker defaults or start a run.
    assert (
        service.store.get_worker(worker["worker_id"])["bootstrap_bundle_json"]
        == worker["bootstrap_bundle_json"]
    )
    with service.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    _past_expiry(service.store)
    service.files.expire(TENANT, OWNER)
    service.store.close()
    restarted = Store(service.store.db_path)
    try:
        run_one = _dispatch(restarted, schedule_one)
        run_two = _dispatch(restarted, schedule_two)
        for run, schedule, upload, expected in (
            (run_one, schedule_one, first, b"first bytes"),
            (run_two, schedule_two, second, b"different second bytes"),
        ):
            manifest = _manifest(restarted, run, worker)
            assert len(manifest) == 1
            entry = manifest[0]
            assert entry["upload_id"] == upload["upload_id"]
            assert entry["sha256"] == hashlib.sha256(expected).hexdigest()
            assert (
                entry["path"]
                == f"inputs/{schedule['schedule_id']}/{upload['upload_id']}/brief.txt"
            )
            # Exercise the actual run-local runtime handoff; accepted inputs are
            # added to an ephemeral invocation, not future worker defaults.
            invocation = service._run_local_worker(worker, run)
            projected = json.loads(invocation["bootstrap_bundle_json"])["files"]
            accepted = [item for item in projected if item.get("managed_upload_id")]
            assert len(accepted) == 1
            assert accepted[0]["managed_upload_id"] == upload["upload_id"]
            assert accepted[0]["sha256"] == hashlib.sha256(expected).hexdigest()
            index_path = invocation["_run_input_manifest_path"]
            index = next(item for item in projected if item["path"] == index_path)
            assert json.loads(index["content"])["files"] == [
                {
                    "name": "brief.txt",
                    "path": entry["path"],
                    "sha256": hashlib.sha256(expected).hexdigest(),
                    "size_bytes": len(expected),
                }
            ]
            assert index_path in service._runtime_instruction_for_run(
                invocation, run["instruction"]
            )
            assert run["instruction"] == "Use the accepted files"
            from pathlib import Path

            assert Path(accepted[0]["source_path"]).read_bytes() == expected
            replay, created = restarted.create_or_get_run_for_schedule(
                schedule["schedule_id"]
            )
            assert not created
            assert replay["run_id"] == run["run_id"]
            assert _manifest(restarted, replay, worker) == manifest
        assert (
            service.store.get_worker(worker["worker_id"])["bootstrap_bundle_json"]
            == worker["bootstrap_bundle_json"]
        )
    finally:
        restarted.close()


def test_reusing_upload_in_later_schedule_creates_distinct_projection(service):
    worker = _worker(service)
    upload = _upload(service, b"immutable version")
    first = _schedule(service, worker, upload)
    second = _schedule(service, worker, upload)
    a = _manifest(service.store, _dispatch(service.store, first), worker)[0]
    b = _manifest(service.store, _dispatch(service.store, second), worker)[0]
    assert a["upload_id"] == b["upload_id"]
    assert a["sha256"] == b["sha256"]
    assert a["path"] != b["path"]
    assert a["projection_id"] != b["projection_id"]


def test_run_without_selected_files_keeps_exact_instruction(service):
    instruction = "Use the workspace as needed"
    assert service._runtime_instruction_for_run(
        {"_run_input_manifest_path": ""}, instruction
    ) == instruction


def test_run_input_instruction_comes_only_from_its_prompt_manifest(service):
    from pathlib import Path

    import workers_projects_runtime.service as service_module

    manifest = service._run_inputs_prompt()
    raw = (
        Path(service_module.__file__).with_name("prompts") / "worker-run-inputs.json"
    ).read_bytes()
    assert manifest["id"] == "xperfect.worker-run-inputs"
    assert manifest["sha256"] == hashlib.sha256(raw).hexdigest()
    index = ".xperfect/run-inputs/abc.json"
    rendered = service._runtime_instruction_for_run(
        {"_run_input_manifest_path": index}, "Goal"
    )
    assert rendered == "Goal\n\n" + manifest["instruction"].format(index_path=index)
    # The source carries no second copy of the model-facing wording.
    source = Path(service_module.__file__).read_text(encoding="utf-8")
    assert "same-named workspace file may differ" not in source


def test_schedule_requires_exact_file_revision_and_snapshots_it(service):
    worker = _worker(service)
    upload = _upload(service, b"revisioned input", relative_path="brief.txt")
    manifest = service.files.manifest(
        TENANT,
        OWNER,
        [upload["upload_id"]],
        [upload["revision"]],
    )
    assert manifest[0]["revision"] == upload["revision"]
    with pytest.raises(FileAdmissionError, match="selected file version changed"):
        service.files.manifest(
            TENANT,
            OWNER,
            [upload["upload_id"]],
            ["stale-revision"],
        )
    scheduled = service.schedule_run(
        worker["worker_id"],
        "Use this exact file version",
        run_at="2027-01-02T03:04:05+00:00",
        file_upload_ids=[upload["upload_id"]],
        file_upload_revisions=[upload["revision"]],
    )
    stored = service.store.get_schedule(scheduled["schedule_id"])
    assert stored is not None
    assert json.loads(stored["file_manifest_json"])[0]["revision"] == upload["revision"]
    moved = service.files.move_upload(
        TENANT,
        OWNER,
        upload["upload_id"],
        relative_path="renamed.txt",
        revision=upload["revision"],
    )
    assert moved["revision"] != upload["revision"]
    replayed = service.store.get_schedule(scheduled["schedule_id"])
    assert replayed is not None
    replayed_manifest = json.loads(replayed["file_manifest_json"])
    assert replayed_manifest[0]["revision"] == upload["revision"]
    assert replayed_manifest[0]["relative_path"] == "brief.txt"


def test_cancel_before_dispatch_releases_only_schedule_pin_and_allows_expiry(service):
    worker = _worker(service)
    upload = _upload(service, b"cancelled input")
    schedule = _schedule(service, worker, upload)
    _past_expiry(service.store)
    service.files.expire(TENANT, OWNER)
    assert service.files.manifest(TENANT, OWNER, [upload["upload_id"]])
    service.store.finalize_schedule(schedule["schedule_id"], state="cancelled")
    service.files.expire(TENANT, OWNER)
    with service.store._connect() as conn:
        assert (
            conn.execute(
                "SELECT state FROM workspace_file_uploads WHERE upload_id = ?",
                (upload["upload_id"],),
            ).fetchone()[0]
            == "expired"
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM workspace_file_pins").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_cancel_after_dispatch_keeps_run_inputs_pinned(service):
    worker = _worker(service)
    upload = _upload(service, b"run history")
    schedule = _schedule(service, worker, upload)
    run = _dispatch(service.store, schedule)
    accepted = _manifest(service.store, run, worker)
    # Other cancellation paths also write state directly; the migration trigger
    # covers these in the same transaction as their existing state changes.
    with service.store._connect() as conn:
        conn.execute(
            "UPDATE scheduled_runs SET state = 'cancelled' WHERE schedule_id = ?",
            (schedule["schedule_id"],),
        )
    _past_expiry(service.store)
    service.files.expire(TENANT, OWNER)
    assert _manifest(service.store, run, worker) == accepted
    with service.store._connect() as conn:
        pins = [
            tuple(row)
            for row in conn.execute(
                "SELECT kind, reference_id FROM workspace_file_pins"
            )
        ]
    assert pins == [("run", run["run_id"])]
    assert service.files.manifest(TENANT, OWNER, [upload["upload_id"]])


def test_manifest_failure_rolls_back_schedule_and_pins(service):
    worker = _worker(service)
    upload = _upload(service, b"accepted")
    manifest = service.files.manifest(TENANT, OWNER, [upload["upload_id"]])
    manifest[0]["sha256"] = "0" * 64
    with pytest.raises(FileAdmissionError, match="no longer ready"):
        service.store.create_scheduled_run(
            worker_id=worker["worker_id"],
            project_id=worker["project_id"],
            owner_id=OWNER,
            tenant_id=TENANT,
            instruction="Use file",
            run_at="2027-01-02T03:04:05+00:00",
            file_manifest=manifest,
        )
    with service.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM workspace_file_pins").fetchone()[0] == 0
        )


def test_dispatch_failure_rolls_back_run_and_does_not_lose_schedule(service):
    worker = _worker(service)
    upload = _upload(service, b"accepted")
    schedule = _schedule(service, worker, upload)
    assert service.store.claim_schedule(schedule["schedule_id"])
    with service.store._connect() as conn:
        conn.execute(
            "UPDATE workspace_file_uploads SET state = 'failed' WHERE upload_id = ?",
            (upload["upload_id"],),
        )
    with pytest.raises(FileAdmissionError, match="no longer ready"):
        service.store.create_or_get_run_for_schedule(schedule["schedule_id"])
    with service.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM workspace_file_run_inputs").fetchone()[0]
            == 0
        )
    persisted = service.store.get_schedule(schedule["schedule_id"])
    assert persisted["state"] == "running"
    assert persisted["queued_run_id"] is None


def test_schedule_cannot_accept_another_owners_upload_or_read_run_manifest(service):
    worker = _worker(service)
    foreign = _upload(service, b"private", owner="owner-two")
    with pytest.raises(FileAdmissionError, match="not found"):
        _schedule(service, worker, foreign)
    mine = _upload(service, b"mine")
    run = _dispatch(service.store, _schedule(service, worker, mine))
    with pytest.raises(ValueError, match="does not belong"):
        service.store.get_run_file_manifest(
            run["run_id"],
            worker_id=worker["worker_id"],
            tenant_id=TENANT,
            owner_id="owner-two",
        )


def test_schedule_request_retains_all_upload_ids():
    request = ScheduleRunRequest(
        instruction="Use files", file_upload_ids=[f"fil_{n}" for n in range(25)]
    )
    assert len(request.file_upload_ids) == 25
    assert ScheduleRunRequest(instruction="No files").file_upload_ids == []


def test_schedule_reserves_copy_capacity_and_cancellation_releases_it(
    service, monkeypatch
):
    worker = _worker(service)
    upload = _upload(service, b"data")
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "8")
    scheduled = _schedule(service, worker, upload)
    storage = service.files.storage(TENANT, OWNER)
    assert storage["used_logical_bytes"] == 4
    assert storage["reserved_bytes"] == 4
    with pytest.raises(FileAdmissionError, match="workspace copy"):
        _schedule(service, worker, upload)
    service.store.finalize_schedule(scheduled["schedule_id"], state="cancelled")
    assert service.files.storage(TENANT, OWNER)["reserved_bytes"] == 0
    assert _schedule(service, worker, upload)["state"] == "pending"


def test_due_run_does_not_double_reserve_same_schedule_projection(service, monkeypatch):
    worker = _worker(service)
    upload = _upload(service, b"data")
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "8")
    scheduled = _schedule(service, worker, upload)
    run = _dispatch(service.store, scheduled)
    assert _manifest(service.store, run, worker)
    assert service.files.storage(TENANT, OWNER)["reserved_bytes"] == 4
    service.store.finalize_schedule(scheduled["schedule_id"], state="cancelled")
    assert service.files.storage(TENANT, OWNER)["reserved_bytes"] == 4


def test_schedule_keeps_selected_folder_relative_paths(service):
    worker = _worker(service)
    upload = _upload(service, b"nested", relative_path="folder/nested/brief.txt")
    scheduled = _schedule(service, worker, upload)
    entry = _manifest(service.store, _dispatch(service.store, scheduled), worker)[0]
    assert (
        entry["path"]
        == f"inputs/{scheduled['schedule_id']}/{upload['upload_id']}/folder/nested/brief.txt"
    )
