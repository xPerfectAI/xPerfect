from __future__ import annotations

import json
import hashlib
import errno
import os
from pathlib import Path
import time

import pytest

import workers_projects_runtime.service as service_module
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import RunRestorationState, Store
from workers_projects_runtime.workspace_files import FileAdmissionError
from workers_projects_runtime.workspace_files import artifact_worker_context


class TemporaryWorkspaceRuntime(StubRuntime):
    def __init__(self, root: Path) -> None:
        self.root = root

    def _runtime_info(self, worker: dict, *, pid: int | None) -> RuntimeInfo:
        worker_root = self.root / str(worker["worker_id"])
        return RuntimeInfo(
            runtime="openclaw-stub",
            model=str(worker.get("model") or self.resolve_model(str(worker.get("profile") or ""))),
            gateway_url=f"http://127.0.0.1/stub/{worker['worker_id']}",
            gateway_port=None,
            gateway_token=None,
            session_key=f"agent:main:test:{worker['worker_id']}",
            state_dir=str(worker_root / "state"),
            workspace_dir=str(worker_root / "workspace"),
            pid=pid,
        )


def test_duplicate_streaming_limit_catches_source_growth_and_cleans_partial_copy(
    tmp_path,
    monkeypatch,
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    source = source_root / "growing.bin"
    source.write_bytes(b"a")
    monkeypatch.setenv("GLASSHIVE_DUPLICATE_MAX_BYTES", "1024")

    def changed_after_preflight(_source_root):
        source.write_bytes(b"b" * 2048)
        return [(source, Path("growing.bin"))], 0, "copied"

    monkeypatch.setattr(service_module, "_workspace_copy_plan", changed_after_preflight)
    service = object.__new__(WorkersProjectsService)
    with pytest.raises(ValueError, match="byte limit"):
        service._copy_workspace_contents(
            {"workspace_dir": str(source_root)},
            {"workspace_dir": str(target_root)},
        )

    assert not (target_root / "growing.bin").exists()
    assert not list(target_root.rglob("*.glasshive-copy-*"))


def test_duplicate_has_no_default_file_count_or_byte_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_DUPLICATE_MAX_FILES", raising=False)
    monkeypatch.delenv("GLASSHIVE_DUPLICATE_MAX_BYTES", raising=False)
    source = tmp_path / "source"
    source.mkdir()
    with (source / "large.bin").open("wb") as stream:
        stream.truncate(513 * 1024 * 1024)
    for index in range(5_000):
        (source / f"small-{index:04d}.txt").touch()

    files, skipped, state = service_module._workspace_copy_plan(source)

    assert len(files) == 5_001
    assert skipped == 0
    assert state == "copied"


def test_duplicate_classifies_filesystem_exhaustion_and_removes_worker(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Source")
    target_project = create_project(store, "Target")
    service = WorkersProjectsService(
        store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False,
    )
    try:
        source = service.create_worker(
            project_id=source_project["project_id"], tenant_id="tenant-a",
            owner_id="owner-a", name="Source", role="main", profile="codex-cli",
            backend="codex-cli", workspace_kind="named", start_synchronously=False,
        )
        source_root = Path(source["workspace_dir"])
        source_root.mkdir(parents=True, exist_ok=True)
        (source_root / "input.bin").write_bytes(b"x")

        def no_space(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "synthetic filesystem full")

        monkeypatch.setattr(service_module, "_copy_regular_workspace_file", no_space)
        with pytest.raises(FileAdmissionError) as error:
            service.duplicate_worker(
                source["worker_id"], target_project["project_id"],
                "owner-a", "Copy", "main",
            )
        assert error.value.status_code == 507
        assert error.value.code == "storage_allocation_failed"
        assert store.list_workers(
            target_project["project_id"], tenant_id="tenant-a", owner_id="owner-a",
        ) == []
    finally:
        service.shutdown()


def create_project(store: Store, title: str = "Workspace Catalog", owner_id: str = "owner-a") -> dict:
    return store.create_project(
        owner_id=owner_id,
        title=title,
        goal="Exercise public-safe workspace behavior.",
        default_worker_profile="codex-cli",
        tenant_id="tenant-a",
    )


def create_stored_worker(
    store: Store,
    project: dict,
    *,
    name: str,
    owner_id: str = "owner-a",
    workspace_kind: str = "named",
    tags: list[str] | None = None,
) -> dict:
    return store.create_worker(
        project_id=project["project_id"],
        tenant_id="tenant-a",
        owner_id=owner_id,
        name=name,
        role="main",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="stub/codex-cli",
        workspace_kind=workspace_kind,
        tags=tags,
    )


def test_file_workspace_root_uses_admitted_owner_workspace(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = create_project(store)
    worker = create_stored_worker(store, project, name="Files")
    service = WorkersProjectsService(
        store, TemporaryWorkspaceRuntime(tmp_path / "runtime"),
        reconcile_on_startup=False,
    )
    try:
        with pytest.raises(RuntimeError, match="not prepared"):
            service.file_workspace_root(
                worker["worker_id"], tenant_id="tenant-a", owner_id="owner-a"
            )
        root = tmp_path / "files"
        root.mkdir()
        store.update_worker(worker["worker_id"], workspace_dir=str(root))
        assert service.file_workspace_root(
            worker["worker_id"], tenant_id="tenant-a", owner_id="owner-a"
        ) == root.resolve()
        with pytest.raises(ValueError, match="unavailable"):
            service.file_workspace_root(
                worker["worker_id"], tenant_id="tenant-a", owner_id="owner-b"
            )
    finally:
        service.shutdown()


def test_store_migrates_existing_workers_to_legacy_workspace_kind(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    project = create_project(store)
    worker = create_stored_worker(store, project, name="Existing Workspace")

    with store._connect() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_workers_workspace_catalog")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
        if "workspace_kind" in columns:
            conn.execute("ALTER TABLE workers DROP COLUMN workspace_kind")
        if "workspace_tags_json" in columns:
            conn.execute("ALTER TABLE workers DROP COLUMN workspace_tags_json")

    migrated = Store(str(db_path))
    migrated_worker = migrated.get_worker(worker["worker_id"])

    assert migrated_worker is not None
    assert migrated_worker["workspace_kind"] == "legacy"
    assert migrated_worker["tags"] == []


def test_workspace_catalog_is_owner_scoped_searchable_tagged_and_cursor_stable(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = create_project(store, "Quarterly Planning")
    favorite = create_stored_worker(
        store,
        project,
        name="Finance Review",
        tags=["Finance", "Quarterly"],
    )
    older = create_stored_worker(store, project, name="Research Notes", tags=["Research"])
    newer = create_stored_worker(store, project, name="Finance Forecast", tags=["Finance"])
    shared = store.create_execution_workspace(
        project_id=project["project_id"], tenant_id="tenant-a",
        owner_id="owner-a", execution_mode="docker",
    )
    create_stored_worker(store, project, name="One-off Draft", workspace_kind="ephemeral", tags=["Finance"])
    other_project = create_project(store, "Other Owner", owner_id="owner-b")
    create_stored_worker(store, other_project, name="Another Owner", owner_id="owner-b", tags=["Finance"])

    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET workspace_id = ? WHERE worker_id = ?",
            (shared["workspace_id"], favorite["worker_id"]),
        )
        conn.execute(
            "UPDATE workers SET favorite = 1, updated_at = ? WHERE worker_id = ?",
            ("2026-01-01T00:00:00+00:00", favorite["worker_id"]),
        )
        conn.execute(
            "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
            ("2026-01-02T00:00:00+00:00", older["worker_id"]),
        )
        conn.execute(
            "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
            ("2026-01-03T00:00:00+00:00", newer["worker_id"]),
        )
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        filtered = service.list_workspace_catalog(
            tenant_id="tenant-a",
            owner_id="owner-a",
            workspace_kinds={"named"},
            search="finance",
            tags=["FINANCE"],
            limit=10,
        )
        assert [item["worker_id"] for item in filtered["items"]] == [favorite["worker_id"], newer["worker_id"]]
        assert filtered["items"][0]["tags"] == ["finance", "quarterly"]
        assert filtered["items"][0]["last_activity_at"]
        assert filtered["items"][0]["execution_workspace_mode"] == "shared"
        assert filtered["items"][1]["execution_workspace_mode"] == "isolated"
        with store._connect() as conn:
            assert artifact_worker_context(conn, store.get_worker(favorite["worker_id"]))["_execution_workspace_mode"] == "shared"

        first_page = service.list_workspace_catalog(
            tenant_id="tenant-a",
            owner_id="owner-a",
            workspace_kinds={"named"},
            limit=2,
        )
        second_page = service.list_workspace_catalog(
            tenant_id="tenant-a",
            owner_id="owner-a",
            workspace_kinds={"named"},
            cursor=first_page["next_cursor"],
            limit=2,
        )
    finally:
        service.shutdown()

    first_ids = [item["worker_id"] for item in first_page["items"]]
    second_ids = [item["worker_id"] for item in second_page["items"]]
    assert first_ids == [favorite["worker_id"], newer["worker_id"]]
    assert second_ids == [older["worker_id"]]
    assert not set(first_ids) & set(second_ids)
    assert first_page["next_cursor"]
    assert second_page["next_cursor"] is None


def test_workspace_metadata_validates_kind_and_normalizes_tags(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = create_project(store)
    worker = create_stored_worker(store, project, name="Workspace")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        updated = service.update_worker_metadata(
            worker["worker_id"],
            workspace_kind="named",
            tags=[" Finance ", "finance", "Quarterly"],
        )
        with pytest.raises(ValueError, match="workspace kind"):
            service.update_worker_metadata(worker["worker_id"], workspace_kind="temporary")
        with pytest.raises(ValueError, match="catalog cursor"):
            service.list_workspace_catalog(
                tenant_id="tenant-a",
                owner_id="owner-a",
                cursor="not-a-valid-cursor",
            )
    finally:
        service.shutdown()

    assert updated["workspace_kind"] == "named"
    assert updated["tags"] == ["finance", "quarterly"]


def test_duplicate_copies_only_regular_project_files_and_returns_report(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Source Project")
    target_project = create_project(store, "Target Project")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
            name="Source Workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            tags=["Reusable"],
            bootstrap_bundle={
                "project_definition": "Build a synthetic report.",
                "env": {"EXAMPLE_TOKEN": "must-not-copy"},
                "claude_project_mcp": {"example": {"url": "https://example.invalid/mcp"}},
                "callbacks": {"url": "https://example.invalid/callback"},
            },
        )
        source_workspace = Path(source["workspace_dir"])
        source_workspace.mkdir(parents=True, exist_ok=True)
        (source_workspace / "report.txt").write_text("approved report")
        (source_workspace / "nested").mkdir()
        (source_workspace / "nested" / "data.csv").write_text("value\n42\n")
        (source_workspace / ".mcp.json").write_text('{"grant":"must-not-copy"}')
        (source_workspace / ".env.local").write_text("EXAMPLE_TOKEN=must-not-copy")
        (source_workspace / ".codex").mkdir()
        (source_workspace / ".codex" / "auth.json").write_text('{"token":"must-not-copy"}')
        (source_workspace / ".glasshive-runs").mkdir()
        (source_workspace / ".glasshive-runs" / "run.json").write_text("{}")
        (source_workspace / ".claude.json").write_text('{"session":"must-not-copy"}')
        (source_workspace / "session.json").write_text('{"session":"must-not-copy"}')
        (source_workspace / "Cookies").write_text("must-not-copy")
        os.symlink("report.txt", source_workspace / "safe-relative-link")
        source_home = source_workspace.parent / "home" / "browser-profile"
        source_home.mkdir(parents=True)
        (source_home / "Cookies").write_text("must-not-copy")
        store.add_event(
            source_project["project_id"],
            source["worker_id"],
            None,
            "source.private_audit",
            "Synthetic source-only audit event",
            tenant_id="tenant-a",
        )
        store.create_scheduled_run(
            worker_id=source["worker_id"],
            project_id=source_project["project_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
            instruction="Synthetic future run",
            run_at="2099-01-01T00:00:00+00:00",
        )
        store.create_run(
            source["worker_id"],
            source_project["project_id"],
            "Synthetic active source run",
            state=RunRestorationState.RUNNING,
        )

        duplicate = service.duplicate_worker(
            source_worker_id=source["worker_id"],
            project_id=target_project["project_id"],
            owner_id="owner-a",
            name="Duplicated Workspace",
            role="main",
        )
    finally:
        service.shutdown()

    target_workspace = Path(duplicate["workspace_dir"])
    assert duplicate["worker_id"] != source["worker_id"]
    assert duplicate["workspace_kind"] == "named"
    assert duplicate["tags"] == ["reusable"]
    assert duplicate["favorite"] == 0
    assert duplicate["last_run_id"] is None
    assert duplicate["duplication_report"] == {
        "source_state": "copied",
        "copied_files": 2,
        "skipped_items": 8,
    }
    assert (target_workspace / "report.txt").read_text() == "approved report"
    assert (target_workspace / "nested" / "data.csv").read_text() == "value\n42\n"
    assert not (target_workspace / ".mcp.json").exists()
    assert not (target_workspace / ".env.local").exists()
    assert not (target_workspace / ".codex").exists()
    assert not (target_workspace / ".glasshive-runs").exists()
    assert not (target_workspace / ".claude.json").exists()
    assert not (target_workspace / "session.json").exists()
    assert not (target_workspace / "Cookies").exists()
    assert not (target_workspace / "safe-relative-link").exists()
    assert not (target_workspace.parent / "home" / "browser-profile" / "Cookies").exists()
    target_bundle = json.loads(str(duplicate["bootstrap_bundle_json"]))
    assert target_bundle == {"project_definition": "Build a synthetic report."}
    assert store.list_schedules_for_worker(duplicate["worker_id"], include_done=True) == []
    assert store.list_runs_for_worker(duplicate["worker_id"]) == []
    assert "source.private_audit" not in {
        event["event_type"] for event in store.list_events(duplicate["worker_id"], tenant_id="tenant-a")
    }


def test_duplicate_keeps_exact_accepted_input_name_only_for_matching_bytes(tmp_path):
    store = Store(str(tmp_path / 'runtime.db'))
    project = create_project(store)
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / 'runtime'), reconcile_on_startup=False)
    try:
        source = service.create_worker(project_id=project['project_id'], tenant_id='tenant-a',
            owner_id='owner-a', name='Original', role='main', profile='codex-cli',
            backend='codex-cli', workspace_kind='named')
        root = Path(source['workspace_dir'])
        path = root / 'inputs/fil_synthetic/brief.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'EXACT-24')
        digest = hashlib.sha256(b'EXACT-24').hexdigest()
        with store._connect() as conn:
            conn.execute('INSERT INTO workspace_file_uploads '
                '(upload_id,tenant_id,owner_id,draft_id,request_key,name,size_bytes,received_bytes,state,sha256,expires_at,created_at,updated_at,relative_path) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('fil_synthetic','tenant-a','owner-a','draft','upload','brief.txt',8,8,'ready',digest,
                 time.time()+3600,time.time(),time.time(),'brief.txt'))
            conn.execute('INSERT INTO workspace_file_entries '
                '(file_id,tenant_id,owner_id,workspace_key,path,version_id) VALUES (?,?,?,?,?,?)',
                ('fen_source','tenant-a','owner-a',source['worker_id'],
                 'inputs/fil_synthetic/brief.txt','fil_synthetic'))
        copied = service.duplicate_worker(source['worker_id'], project['project_id'], 'owner-a', 'Copy', 'main')
        listing = service.files.list_files(copied['worker_id'], 'tenant-a', 'owner-a', directory='inputs')
        assert listing['items'][0]['display_name'] == 'brief.txt'
        assert (Path(copied['workspace_dir']) / 'inputs/fil_synthetic/brief.txt').read_bytes() == b'EXACT-24'
        path.write_bytes(b'CHANGED!')
        changed = service.duplicate_worker(source['worker_id'], project['project_id'], 'owner-a', 'Changed copy', 'main')
        listing = service.files.list_files(changed['worker_id'], 'tenant-a', 'owner-a', directory='inputs')
        assert 'display_name' not in listing['items'][0]
    finally:
        service.shutdown()


def test_duplicate_reports_an_explicit_empty_source(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Empty Source")
    target_project = create_project(store, "Empty Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
            name="Empty Workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
        )
        Path(source["workspace_dir"]).mkdir(parents=True, exist_ok=True)
        duplicate = service.duplicate_worker(
            source_worker_id=source["worker_id"],
            project_id=target_project["project_id"],
            owner_id="owner-a",
            name="Empty Duplicate",
            role="main",
        )
    finally:
        service.shutdown()

    assert duplicate["duplication_report"] == {
        "source_state": "empty",
        "copied_files": 0,
        "skipped_items": 0,
    }


@pytest.mark.parametrize("absolute_target", [True, False])
def test_duplicate_rejects_absolute_and_out_of_root_symlinks_and_rolls_back(tmp_path, absolute_target):
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Unsafe Source")
    target_project = create_project(store, "Unsafe Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
            name="Unsafe Workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
        )
        source_workspace = Path(source["workspace_dir"])
        source_workspace.mkdir(parents=True, exist_ok=True)
        (source_workspace / "approved.txt").write_text("must not copy after rejected preflight")
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        target = str(outside) if absolute_target else os.path.relpath(outside, source_workspace)
        os.symlink(target, source_workspace / "unsafe-link")

        with pytest.raises(ValueError, match="unsafe workspace symlink"):
            service.duplicate_worker(
                source_worker_id=source["worker_id"],
                project_id=target_project["project_id"],
                owner_id="owner-a",
                name="Rejected Duplicate",
                role="main",
            )

        target_workers = store.list_workers(target_project["project_id"], tenant_id="tenant-a", owner_id="owner-a")
    finally:
        service.shutdown()

    assert target_workers == []


def test_duplicate_rejects_looping_symlink_and_rolls_back(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Looping Source")
    target_project = create_project(store, "Looping Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"], tenant_id="tenant-a", owner_id="owner-a",
            name="Looping Workspace", role="main", profile="codex-cli", backend="codex-cli",
            workspace_kind="named",
        )
        source_workspace = Path(source["workspace_dir"])
        source_workspace.mkdir(parents=True, exist_ok=True)
        looping_link = source_workspace / "loop"
        os.symlink("loop", looping_link)
        original_resolve = Path.resolve

        def resolve_without_loop_error(path, strict=False):
            if path == looping_link and not strict:
                return path.absolute()
            return original_resolve(path, strict=strict)

        # Python 3.13+ no longer raises from resolve(strict=False) for a loop.
        # Simulate that behavior so the regression is covered by the pinned 3.12 venv.
        monkeypatch.setattr(Path, "resolve", resolve_without_loop_error)

        with pytest.raises(ValueError, match="unsafe workspace symlink"):
            service.duplicate_worker(
                source_worker_id=source["worker_id"], project_id=target_project["project_id"],
                owner_id="owner-a", name="Rejected Looping Duplicate", role="main",
            )
        target_workers = store.list_workers(
            target_project["project_id"], tenant_id="tenant-a", owner_id="owner-a"
        )
    finally:
        service.shutdown()

    assert target_workers == []


def test_duplicate_skips_common_generated_trees_before_inspecting_their_symlinks(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Generated Source")
    target_project = create_project(store, "Generated Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"], tenant_id="tenant-a", owner_id="owner-a",
            name="Generated Workspace", role="main", profile="codex-cli", backend="codex-cli",
            workspace_kind="named",
        )
        source_workspace = Path(source["workspace_dir"])
        (source_workspace / ".venv" / "bin").mkdir(parents=True)
        os.symlink("/usr/bin/python3", source_workspace / ".venv" / "bin" / "python")
        (source_workspace / "node_modules" / "package").mkdir(parents=True)
        (source_workspace / "node_modules" / "package" / "index.js").write_text("generated")
        (source_workspace / "main.py").write_text("print('safe')")

        duplicate = service.duplicate_worker(
            source_worker_id=source["worker_id"], project_id=target_project["project_id"],
            owner_id="owner-a", name="Generated Duplicate", role="main",
        )
    finally:
        service.shutdown()

    duplicate_workspace = Path(duplicate["workspace_dir"])
    assert (duplicate_workspace / "main.py").read_text() == "print('safe')"
    assert not (duplicate_workspace / ".venv").exists()
    assert not (duplicate_workspace / "node_modules").exists()


def test_duplicate_fails_loud_when_copy_bounds_are_exceeded(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DUPLICATE_MAX_FILES", "1")
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Bounded Source")
    target_project = create_project(store, "Bounded Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
            name="Bounded Workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
        )
        source_workspace = Path(source["workspace_dir"])
        source_workspace.mkdir(parents=True, exist_ok=True)
        (source_workspace / "one.txt").write_text("one")
        (source_workspace / "two.txt").write_text("two")

        with pytest.raises(ValueError, match="file limit"):
            service.duplicate_worker(
                source_worker_id=source["worker_id"],
                project_id=target_project["project_id"],
                owner_id="owner-a",
                name="Rejected Bounded Duplicate",
                role="main",
            )
    finally:
        service.shutdown()


def test_duplicate_fails_closed_for_special_files_and_rolls_back_destination(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Special Source")
    target_project = create_project(store, "Special Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"], tenant_id="tenant-a", owner_id="owner-a",
            name="Special Workspace", role="main", profile="codex-cli", backend="codex-cli",
            workspace_kind="named",
        )
        source_workspace = Path(source["workspace_dir"])
        source_workspace.mkdir(parents=True, exist_ok=True)
        (source_workspace / "approved.txt").write_text("must roll back")
        os.mkfifo(source_workspace / "unsafe.fifo")

        with pytest.raises(ValueError, match="not a regular file or directory"):
            service.duplicate_worker(
                source_worker_id=source["worker_id"], project_id=target_project["project_id"],
                owner_id="owner-a", name="Rejected Special Duplicate", role="main",
            )
        target_workers = store.list_workers(
            target_project["project_id"], tenant_id="tenant-a", owner_id="owner-a"
        )
    finally:
        service.shutdown()

    assert target_workers == []


def test_duplicate_fails_closed_when_a_source_item_cannot_be_inspected(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Unreadable Source")
    target_project = create_project(store, "Unreadable Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    original_stat = Path.stat

    def fail_selected_stat(path, *args, **kwargs):
        if path.name == "unreadable.txt" and kwargs.get("follow_symlinks") is False:
            raise OSError("synthetic stat failure")
        return original_stat(path, *args, **kwargs)

    try:
        source = service.create_worker(
            project_id=source_project["project_id"], tenant_id="tenant-a", owner_id="owner-a",
            name="Unreadable Workspace", role="main", profile="codex-cli", backend="codex-cli",
            workspace_kind="named",
        )
        source_workspace = Path(source["workspace_dir"])
        source_workspace.mkdir(parents=True, exist_ok=True)
        (source_workspace / "unreadable.txt").write_text("synthetic")
        monkeypatch.setattr(Path, "stat", fail_selected_stat)

        with pytest.raises(ValueError, match="could not be inspected"):
            service.duplicate_worker(
                source_worker_id=source["worker_id"], project_id=target_project["project_id"],
                owner_id="owner-a", name="Rejected Unreadable Duplicate", role="main",
            )
        target_workers = store.list_workers(
            target_project["project_id"], tenant_id="tenant-a", owner_id="owner-a"
        )
    finally:
        service.shutdown()

    assert target_workers == []


def test_duplicate_enforces_directory_depth_limit_and_rolls_back_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DUPLICATE_MAX_DEPTH", "2")
    store = Store(str(tmp_path / "runtime.db"))
    source_project = create_project(store, "Deep Source")
    target_project = create_project(store, "Deep Target")
    service = WorkersProjectsService(store, TemporaryWorkspaceRuntime(tmp_path / "runtime"), reconcile_on_startup=False)
    try:
        source = service.create_worker(
            project_id=source_project["project_id"], tenant_id="tenant-a", owner_id="owner-a",
            name="Deep Workspace", role="main", profile="codex-cli", backend="codex-cli",
            workspace_kind="named",
        )
        source_workspace = Path(source["workspace_dir"])
        deep = source_workspace / "one" / "two"
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "too-deep.txt").write_text("synthetic")

        with pytest.raises(ValueError, match="depth limit"):
            service.duplicate_worker(
                source_worker_id=source["worker_id"], project_id=target_project["project_id"],
                owner_id="owner-a", name="Rejected Deep Duplicate", role="main",
            )
        target_workers = store.list_workers(
            target_project["project_id"], tenant_id="tenant-a", owner_id="owner-a"
        )
    finally:
        service.shutdown()

    assert target_workers == []


def test_execution_workspace_members_are_owner_scoped_and_do_not_expose_bootstrap(tmp_path):
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.service import WorkersProjectsService
    from workers_projects_runtime.store import Store
    store = Store(str(tmp_path / 'members.db'))
    project = store.create_project('owner-a', 'Synthetic workspace', 'Synthetic goal', 'codex-cli')
    worker = store.create_worker(
        project['project_id'], 'owner-a', 'Synthetic member', 'main',
        'codex-cli', 'codex-cli', 'fixture-runtime', 'fixture-model',
        bootstrap_bundle={'env': {'SYNTHETIC_PRIVATE': 'private'}},
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        result = service.execution_workspace_members(worker['workspace_id'], tenant_id='local', owner_id='owner-a')
        assert result['mode'] == 'isolated'
        assert result['default_worker_id'] == worker['worker_id']
        assert len(result['members']) == 1
        assert 'bootstrap_bundle_json' not in result['members'][0]
        assert 'workspace_dir' not in result['members'][0]
        with pytest.raises(KeyError):
            service.execution_workspace_members(worker['workspace_id'], tenant_id='local', owner_id='owner-b')
    finally:
        service.shutdown()
