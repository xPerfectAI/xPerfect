"""Injected adapter contracts; these tests do not claim native XFS enforcement."""
import asyncio
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles, materialize_managed_file
from workers_projects_runtime import workspace_file_storage as adapter_module

TENANT, OWNER = "tenant", "owner"


class SnapshotBackend:
    def __init__(self, root, limit):
        self.root, self.limit = root, limit
        self.extra_used = 0
        self.error = None
        self.calls = []

    def snapshot(self, tenant, owner, requested):
        self.calls.append((tenant, owner, requested))
        if self.error:
            raise RuntimeError(self.error)
        if (tenant, owner) != (TENANT, OWNER):
            raise RuntimeError("Owner is not provisioned")
        allocated, seen = 0, set()
        for path in self.root.rglob("*"):
            if path.is_file():
                metadata = path.stat()
                identity = (metadata.st_dev, metadata.st_ino)
                if identity not in seen:
                    seen.add(identity)
                    allocated += metadata.st_blocks * 512
        return SimpleNamespace(root=self.root, limit_bytes=self.limit,
            kernel_hard_limit_bytes=(self.limit // 4096) * 4096 if self.limit is not None else None,
            used_allocated_bytes=allocated + self.extra_used, filesystem_block_bytes=4096,
            hard_enforced=self.limit is not None, accounting_basis="xfs_project_allocated_bytes",
            enforcement_scope="owner_root", project_id=17)


@pytest.fixture
def injected(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "16384")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    # Simulate the required separate quota/control filesystems. Actual mount,
    # project-ID, and kernel enforcement tests belong to the native backend.
    monkeypatch.setattr(adapter_module, "_same_device", lambda *_: False)
    root = tmp_path / "quota-owner"
    root.mkdir()
    backend = SnapshotBackend(root, 16384)
    store = Store(str(tmp_path / "runtime.db"))
    files = WorkspaceFiles(store, quota_backend=backend,
        managed_root_resolver=lambda tenant, owner, snapshot: snapshot.root / "managed",
        native_coverage=lambda *_: False, control_root=tmp_path / "control")
    project = store.create_project(OWNER, "Files", "Allocated storage", "codex-cli", tenant_id=TENANT)
    worker = store.create_worker(project["project_id"], OWNER, "Files", "operator", "codex-cli", "openclaw", "codex-cli", "stub/codex-cli", tenant_id=TENANT)
    workspace = root / "workspace"
    workspace.mkdir()
    worker = store.update_worker(worker["worker_id"], workspace_dir=str(workspace))
    yield files, backend, worker, workspace
    store.close()


def draft(files, size, key="first"):
    return files.create_upload(TENANT, OWNER, draft_id="draft", name="input.bin", size_bytes=size, idempotency_key=key)


def receive(files, receipt, data):
    async def chunks():
        yield data
    return asyncio.run(files.receive(TENANT, OWNER, receipt["upload_id"], chunks()))


def test_allocation_rounding_and_sparse_logical_length_are_separate(injected):
    files, backend, _, root = injected
    with (root / "sparse.bin").open("wb") as output:
        output.truncate(64 * 1024 * 1024)
    receipt = draft(files, 1)
    meter = files.storage(TENANT, OWNER)
    assert meter["used_logical_bytes"] == 64 * 1024 * 1024
    assert meter["used_allocated_bytes"] == (root / "sparse.bin").stat().st_blocks * 512
    assert meter["reserved_allocated_bytes"] == 4096
    assert meter["limit_bytes"] == 16384
    assert meter["kernel_hard_limit_bytes"] == 16384
    assert meter["quota_hard_enforced"] is True
    assert meter["native_hard_enforcement"] is False
    assert receipt["size_bytes"] == 1


def test_receiving_subtracts_actual_staging_allocation_not_logical_progress(injected):
    files, _, _, _ = injected
    receipt = draft(files, 4097)
    observations = []
    async def chunks():
        yield b"x"
        observations.append(files.storage(TENANT, OWNER))
        yield b"y" * 4096
    asyncio.run(files.receive(TENANT, OWNER, receipt["upload_id"], chunks()))
    assert observations[0]["used_allocated_bytes"] == 4096
    assert observations[0]["reserved_allocated_bytes"] == 4096
    final = files.storage(TENANT, OWNER)
    assert final["used_allocated_bytes"] == 8192
    assert final["reserved_allocated_bytes"] == 0
    assert final["used_logical_bytes"] == 4097


def test_projection_reservation_counts_allocated_copy_and_cancel_releases_it(injected):
    files, _, worker, _ = injected
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    schedule = files.store.create_scheduled_run(
        worker_id=worker["worker_id"], project_id=worker["project_id"], tenant_id=TENANT, owner_id=OWNER,
        instruction="Use input", run_at="2027-01-01T00:00:00+00:00", file_manifest=files.manifest(TENANT, OWNER, [receipt["upload_id"]]))
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 4096
    assert files.store.claim_schedule(schedule["schedule_id"])
    run, _ = files.store.create_or_get_run_for_schedule(schedule["schedule_id"])
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 4096
    files.store.finalize_schedule(schedule["schedule_id"], state="cancelled")
    # The accepted run still owns the same projection; it is counted once.
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 4096
    assert files.store.get_run_file_manifest(run["run_id"], worker_id=worker["worker_id"], tenant_id=TENANT, owner_id=OWNER)


def test_scheduled_admission_reuses_injected_kernel_capacity(injected, monkeypatch):
    files, backend, worker, _ = injected
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    backend.extra_used = 12288
    with pytest.raises(FileAdmissionError, match="workspace copy"):
        files.store.create_scheduled_run(worker_id=worker["worker_id"], project_id=worker["project_id"], tenant_id=TENANT, owner_id=OWNER,
            instruction="Use input", run_at="2027-01-01T00:00:00+00:00", file_manifest=files.manifest(TENANT, OWNER, [receipt["upload_id"]]))
    with files.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0] == 0


def test_at_limit_read_and_projection_receipt_use_private_control_storage(injected):
    files, backend, worker, root = injected
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    backend.extra_used = 8192
    bound = files.bind(worker["worker_id"], TENANT, OWNER, [receipt["upload_id"]], "binding", directory="")
    assert files.storage(TENANT, OWNER)["available_bytes"] == 0
    projected = bound["items"][0]
    control = files.storage_adapter.receipt(TENANT, OWNER, projected["projection_id"])
    assert control.is_file()
    assert not control.is_relative_to(backend.root)
    assert not list(files._owner_root(TENANT, OWNER).glob("*.receipt"))
    item = files.list_files(worker["worker_id"], TENANT, OWNER)["items"][0]
    descriptor, _, size = files.open_download(worker["worker_id"], TENANT, OWNER, item["file_id"])
    try:
        assert size == 1 and os.read(descriptor, 10) == b"x"
    finally:
        os.close(descriptor)
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 0


def test_missing_multi_user_source_key_refuses_bind_without_durable_projection(injected, monkeypatch):
    files, _, worker, workspace = injected
    upload = draft(files, 1)
    receive(files, upload, b"x")
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.delenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", raising=False)

    with pytest.raises(FileAdmissionError) as error:
        files.bind(worker["worker_id"], TENANT, OWNER, [upload["upload_id"]], "retry-key", directory="")
    assert error.value.status_code == 503
    assert error.value.code == "stored_file_source_authority_unavailable"
    with files.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_bindings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_projection_targets").fetchone()[0] == 0
        bundle = json.loads(conn.execute("SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?", (worker["worker_id"],)).fetchone()[0] or "{}")
    assert not bundle.get("files")
    assert not list(workspace.rglob("input.bin"))

    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-projection-source-key")
    bound = files.bind(worker["worker_id"], TENANT, OWNER, [upload["upload_id"]], "retry-key", directory="")
    assert (workspace / bound["items"][0]["path"]).read_bytes() == b"x"
    assert files.storage_adapter.receipt(TENANT, OWNER, bound["items"][0]["projection_id"]).exists()


def test_invalid_source_token_cannot_register_projection_target(injected, monkeypatch):
    files, _, worker, workspace = injected
    item, _ = deferred_projection(injected)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-projection-source-key")
    entry = files.bootstrap_entries(TENANT, OWNER, [item])[0]
    forged = {**entry, "source_path_token": "v1:" + "0" * 64}

    with pytest.raises(FileAdmissionError) as error:
        materialize_managed_file(workspace, forged, worker)
    assert error.value.status_code == 403
    assert error.value.code == "stored_file_source_unauthorized"
    with files.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_projection_targets").fetchone()[0] == 0
    assert not (workspace / item["path"]).exists()
    assert not files.storage_adapter.receipt(TENANT, OWNER, item["projection_id"]).exists()

    materialize_managed_file(workspace, entry, worker)
    assert (workspace / item["path"]).read_bytes() == b"x"


@pytest.mark.parametrize("bad", ["mismatch", "unavailable", "root", "control"])
def test_quota_unavailable_never_becomes_an_empty_fallback_meter(injected, monkeypatch, bad):
    files, backend, _, _ = injected
    if bad == "mismatch":
        backend.limit = 8192
    elif bad == "unavailable":
        backend.error = "Kernel quota is unavailable"
    elif bad == "root":
        files.storage_adapter.managed_root_resolver = lambda *_: backend.root.parent / "outside"
    else:
        monkeypatch.setattr(adapter_module, "_same_device", lambda *_: True)
    with pytest.raises(FileAdmissionError) as error:
        files.storage(TENANT, OWNER)
    assert error.value.status_code == 503
    with pytest.raises(FileAdmissionError):
        draft(files, 1)


def test_missing_injection_cannot_downgrade_persisted_quota_mode(injected):
    files, _, _, _ = injected
    with pytest.raises(FileAdmissionError, match="requires its configured quota"):
        WorkspaceFiles(files.store)
    restarted = Store(files.store.db_path)
    try:
        with pytest.raises(FileAdmissionError, match="requires its configured quota"):
            WorkspaceFiles(restarted)
    finally:
        restarted.close()


@pytest.mark.parametrize("limit", [0, 1, 4095])
def test_zero_and_subblock_policy_is_delegated_without_becoming_unlimited(injected, limit):
    files, backend, _, _ = injected
    seen = []
    def update(tenant, owner, values, **options):
        seen.append((tenant, owner, values, options))
        raise FileAdmissionError("Native quota must cover one filesystem block", 422)
    files.storage_adapter.policy_updater = update
    with pytest.raises(FileAdmissionError) as error:
        files.set_policy(TENANT, OWNER, {"storage_limit_bytes": limit}, administrator=True)
    assert error.value.status_code == 422
    assert seen[0][2] == {"storage_limit_bytes": limit}
    assert backend.limit == 16384
    with files.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_policies").fetchone()[0] == 0


def test_policy_requires_runtime_owner_and_detects_failed_kernel_db_transition(injected):
    files, _, _, _ = injected
    with pytest.raises(FileAdmissionError, match="trusted runtime"):
        files.set_policy(TENANT, OWNER, {"storage_limit_bytes": 8192}, administrator=True)
    def update(tenant, owner, values, **options):
        with files.store._connect() as conn:
            conn.execute("INSERT INTO workspace_file_policies VALUES (?,?,?,NULL,NULL,NULL)", (tenant, owner, values["storage_limit_bytes"]))
    files.storage_adapter.policy_updater = update
    with pytest.raises(FileAdmissionError, match="does not match"):
        files.set_policy(TENANT, OWNER, {"storage_limit_bytes": 8192}, administrator=True)


def test_owner_exhaustion_and_host_exhaustion_are_distinct(injected, monkeypatch):
    files, backend, _, _ = injected
    backend.extra_used = 16384
    exhausted = files.storage_adapter.failure(TENANT, OWNER, OSError(errno.ENOSPC, "full"))
    assert exhausted.status_code == 413 and "Owner storage quota" in str(exhausted)
    backend.extra_used = 0
    monkeypatch.setattr(os, "statvfs", lambda *_: SimpleNamespace(f_bavail=0, f_frsize=4096))
    host = files.storage_adapter.failure(TENANT, OWNER, OSError(errno.ENOSPC, "full"))
    assert host.status_code == 507 and "filesystem is full" in str(host)


def test_receive_failure_cleans_partial_and_unused_reservation(injected, monkeypatch):
    files, backend, _, _ = injected
    receipt = draft(files, 4097)
    async def failed():
        yield b"x"
        backend.extra_used = 12288
        raise OSError(errno.ENOSPC, "synthetic quota boundary")
    with pytest.raises(FileAdmissionError) as error:
        asyncio.run(files.receive(TENANT, OWNER, receipt["upload_id"], failed()))
    assert error.value.status_code == 413
    backend.extra_used = 0
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 0
    assert not list(files._owner_root(TENANT, OWNER).glob("*.part"))


def test_full_native_claim_requires_independent_coverage_callback(injected):
    files, _, _, _ = injected
    assert files.storage(TENANT, OWNER)["native_hard_enforcement"] is False
    files.storage_adapter.native_coverage = lambda *_: True
    assert files.storage(TENANT, OWNER)["native_hard_enforcement"] is True


def test_receipt_authority_rejects_paths_wrong_owner_and_detached_adapter(injected, monkeypatch):
    files, _, worker, root = injected
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    bound = files.bind(worker["worker_id"], TENANT, OWNER, [receipt["upload_id"]], "binding", directory="")
    entry = files.bootstrap_entries(TENANT, OWNER, bound["items"])[0]
    forged = {**entry, "managed_receipt_path": str(root / "untrusted.receipt")}
    with pytest.raises(FileAdmissionError, match="Caller-supplied"):
        materialize_managed_file(root, forged, worker)
    with pytest.raises(FileAdmissionError):
        materialize_managed_file(root, entry, {**worker, "owner_id": "other"})
    monkeypatch.setattr(adapter_module, "_ADAPTERS", {})
    with pytest.raises(FileAdmissionError, match="initialized Files control owner"):
        materialize_managed_file(root, entry, worker)
    assert not (root / "untrusted.receipt").exists()


def test_partially_allocated_projection_is_not_reserved_twice(injected):
    files, _, worker, root = injected
    receipt = draft(files, 4097)
    receive(files, receipt, b"x" * 4097)
    schedule = files.store.create_scheduled_run(worker_id=worker["worker_id"], project_id=worker["project_id"], tenant_id=TENANT, owner_id=OWNER,
        instruction="Use input", run_at="2027-01-01T00:00:00+00:00", file_manifest=files.manifest(TENANT, OWNER, [receipt["upload_id"]]))
    manifest = json.loads(files.store.get_schedule(schedule["schedule_id"])["file_manifest_json"])
    item = manifest[0]
    destination = root / item["path"]
    destination.parent.mkdir(parents=True)
    partial = files._owner_root(TENANT, OWNER) / f".xperfect-transfer-{item['projection_id']}"
    partial.write_bytes(b"x")
    meter = files.storage(TENANT, OWNER)
    assert meter["used_allocated_bytes"] == 12288
    assert meter["reserved_allocated_bytes"] == 4096
    assert meter["available_bytes"] == 0


def test_published_copy_without_receipt_gets_allocation_credit_and_recovers(injected, monkeypatch):
    files, _, worker, _ = injected
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    original_open = os.open
    def crash(path, flags, *args, **kwargs):
        if str(path).endswith(".receipt") and flags & os.O_CREAT:
            raise RuntimeError("synthetic control write interruption")
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", crash)
    with pytest.raises(RuntimeError, match="control write"):
        files.bind(worker["worker_id"], TENANT, OWNER, [receipt["upload_id"]], "binding", directory="")
    meter = files.storage(TENANT, OWNER)
    assert meter["used_allocated_bytes"] == 8192
    assert meter["reserved_allocated_bytes"] == 0
    monkeypatch.setattr(os, "open", original_open)
    restored = files.bind(worker["worker_id"], TENANT, OWNER, [receipt["upload_id"]], "binding", directory="")
    assert files.storage_adapter.receipt(TENANT, OWNER, restored["items"][0]["projection_id"]).exists()


def test_accounting_does_not_read_kernel_once_per_pending_projection(injected, monkeypatch):
    files, backend, worker, _ = injected
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "65536")
    backend.limit = 65536
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    for _ in range(10):
        files.store.create_scheduled_run(worker_id=worker["worker_id"], project_id=worker["project_id"], tenant_id=TENANT, owner_id=OWNER,
            instruction="Use input", run_at="2027-01-01T00:00:00+00:00", file_manifest=files.manifest(TENANT, OWNER, [receipt["upload_id"]]))
    backend.calls.clear()
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 10 * 4096
    assert len(backend.calls) <= 3


def test_pre_migration_receipt_is_read_only_compatibility_not_new_authority(injected):
    files, _, worker, root = injected
    root.rmdir()
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    bound = files.bind(worker["worker_id"], TENANT, OWNER, [receipt["upload_id"]], "binding")
    item = bound["items"][0]
    legacy = files._owner_root(TENANT, OWNER) / f"{item['projection_id']}.receipt"
    legacy.touch()
    # A new receipt placed in payload storage cannot release a reservation.
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 4096
    before_migration = files.storage_adapter.migration_at - 10
    os.utime(legacy, (before_migration, before_migration))
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 4096
    with files.store._connect() as conn:
        conn.execute("UPDATE workspace_file_uploads SET created_at=? WHERE upload_id=?", (before_migration, receipt["upload_id"]))
    assert files.storage(TENANT, OWNER)["reserved_allocated_bytes"] == 0
    assert not files.storage_adapter.receipt(TENANT, OWNER, item["projection_id"]).exists()


def test_receipt_cannot_skip_projection_into_another_root_or_generation(injected):
    files, backend, worker, root = injected
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    bound = files.bind(worker["worker_id"], TENANT, OWNER, [receipt["upload_id"]], "binding", directory="")
    entry = files.bootstrap_entries(TENANT, OWNER, bound["items"])[0]
    other = backend.root / "another-workspace"
    other.mkdir()
    with pytest.raises(FileAdmissionError, match="canonical workspace"):
        materialize_managed_file(other, entry, {**worker, "workspace_dir": str(other)})
    old_root = root.with_name("previous-workspace")
    root.rename(old_root)
    root.mkdir()
    with pytest.raises(FileAdmissionError, match="another workspace root"):
        materialize_managed_file(root, entry, worker)
    assert list(root.iterdir()) == []


def test_backend_error_text_is_private(injected):
    files, backend, _, _ = injected
    backend.error = "synthetic-private-command-and-path"
    with pytest.raises(FileAdmissionError) as error:
        files.storage(TENANT, OWNER)
    assert str(error.value) == "Owner storage quota backend is unavailable"
    assert error.value.__cause__ is not None


def test_nominal_limit_stays_separate_from_floored_kernel_limit(injected, monkeypatch):
    files, backend, _, _ = injected
    monkeypatch.setenv("GLASSHIVE_OWNER_STORAGE_BYTES", "16385")
    backend.limit = 16385
    meter = files.storage(TENANT, OWNER)
    assert meter["limit_bytes"] == 16385
    assert meter["kernel_hard_limit_bytes"] == 16384
    with pytest.raises(FileAdmissionError, match="Not enough storage"):
        draft(files, 16385)


def deferred_projection(injected):
    files, _, worker, root = injected
    root.rmdir()
    receipt = draft(files, 1)
    receive(files, receipt, b"x")
    bound = files.bind(worker["worker_id"], TENANT, OWNER, [receipt["upload_id"]], "binding")
    item = bound["items"][0]
    root.mkdir()
    return item, files.bootstrap_entries(TENANT, OWNER, [item])[0]


def test_scope_change_rejected_before_first_projection(injected):
    files, _, worker, root = injected
    scope = {"scope_id": "member-scope", "kind": "member", "workspace_id": "project",
             "member_id": worker["worker_id"], "root": root}
    files.scope_resolver = lambda *args, **kwargs: dict(scope)
    item, entry = deferred_projection(injected)
    scope.update(scope_id="common-scope", kind="workspace", member_id=None)
    with pytest.raises(FileAdmissionError, match="Accepted file scope changed"):
        materialize_managed_file(root, entry, worker)
    assert list(root.iterdir()) == []
    assert not files.storage_adapter.receipt(TENANT, OWNER, item["projection_id"]).exists()
    with files.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_projection_targets").fetchone()[0] == 0


def test_acl_failure_prevents_publication_and_receipt_then_retry_succeeds(injected):
    files, _, worker, root = injected
    item, entry = deferred_projection(injected)
    destination = root / item["path"]
    events = []
    def access(worker_id, **kwargs):
        assert worker_id == worker["worker_id"]
        assert kwargs["tenant_id"] == TENANT and kwargs["owner_id"] == OWNER
        os.fstat(kwargs["descriptor"])
        events.append(kwargs["is_directory"])
        if not kwargs["is_directory"]:
            assert not destination.exists()
            raise RuntimeError("synthetic ACL unavailable")
    files.scope_access = access
    with pytest.raises(FileAdmissionError, match="access"):
        materialize_managed_file(root, entry, worker)
    assert True in events and False in events
    assert not destination.exists()
    assert not files.storage_adapter.receipt(TENANT, OWNER, item["projection_id"]).exists()
    assert not list(files._owner_root(TENANT, OWNER).glob(".xperfect-transfer-*"))
    files.scope_access = lambda worker_id, **kwargs: os.fstat(kwargs["descriptor"])
    materialize_managed_file(root, entry, worker)
    assert destination.read_bytes() == b"x"


def test_link_before_unlink_crash_credits_one_allocation_and_recovers(injected):
    files, _, worker, root = injected
    item, entry = deferred_projection(injected)
    stage = files._owner_root(TENANT, OWNER) / (".xperfect-transfer-" + item["projection_id"])
    stage.write_bytes(b"x")
    destination = root / item["path"]
    destination.parent.mkdir(parents=True)
    os.link(stage, destination)
    meter = files.storage(TENANT, OWNER)
    assert meter["used_allocated_bytes"] == 8192
    assert meter["reserved_allocated_bytes"] == 0
    materialize_managed_file(root, entry, worker)
    assert destination.read_bytes() == b"x" and destination.stat().st_nlink == 1
    assert not stage.exists()
    assert files.storage_adapter.receipt(TENANT, OWNER, item["projection_id"]).exists()


def test_unsafe_staging_object_is_retained_and_rejected(injected):
    files, _, worker, root = injected
    item, entry = deferred_projection(injected)
    stage = files._owner_root(TENANT, OWNER) / (".xperfect-transfer-" + item["projection_id"])
    stage.symlink_to(root / "missing")
    with pytest.raises(FileAdmissionError, match="staging identity"):
        materialize_managed_file(root, entry, worker)
    assert stage.is_symlink()
    assert not (root / item["path"]).exists()
