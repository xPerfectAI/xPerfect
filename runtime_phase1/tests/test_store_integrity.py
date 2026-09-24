from __future__ import annotations

import gc
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import workers_projects_runtime.store as store_module
from workers_projects_runtime.store import ProviderAdmissionConflictError, Store


def _open_file_descriptor_count() -> int:
    for directory in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(directory):
            return len(os.listdir(directory))
    pytest.skip("This platform does not expose process file descriptors")


def _assert_connection_enforces_foreign_keys(
    store: Store,
    *,
    project_id: str,
) -> None:
    with store._connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS foreign_key_cascade_probe (
                probe_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "INSERT INTO foreign_key_cascade_probe (probe_id, project_id) "
            "VALUES (?, ?)",
            (f"probe-{project_id}", project_id),
        )
        conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM foreign_key_cascade_probe WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="FOREIGN KEY constraint failed",
        ):
            conn.execute(
                "INSERT INTO foreign_key_cascade_probe (probe_id, project_id) "
                "VALUES (?, ?)",
                (f"orphan-{project_id}", "missing-project"),
            )


def _project(store: Store, suffix: str) -> dict:
    return store.create_project(
        "owner",
        f"Foreign-key integrity {suffix}",
        "Reject orphans and honor declared cascades",
        "codex-cli",
    )


@pytest.mark.parametrize("migration_mode", ["normal", "rollback", "concurrent"])
def test_provider_session_scope_migration_preserves_legacy_identity_and_dependants(tmp_path, monkeypatch, migration_mode):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    project = _project(store, "session scopes")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner", name="Legacy conversation",
        role="conversation-agent", profile="codex-cli", backend="", runtime="codex-cli", model="stub",
    )
    legacy = store.upsert_provider_session(
        tenant_id="local", owner_id="owner", conversation_id="same-conversation", agent_id="same-agent",
        model_id="codex-cli:gpt-5.6-sol", project_id=project["project_id"], worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path), access_mode="workspace", history_count=7,
        context_manifest={"messages": 7, "stable_authority_sha256": "a" * 64},
    )
    request, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner", session_id=legacy["session_id"],
        idempotency_key="retained-request", requested_history_count=7,
        message_id="retained-message", stream_id="retained-stream",
    )
    with store._connect() as conn:
        conn.execute("INSERT INTO provider_session_visible_admissions VALUES (?, ?, ?, ?)",
                     (legacy["session_id"], "msg:retained", "retained-turn:1", "2026-01-01T00:00:00Z"))
    # Materialize the actual pre-scope table, retaining its rows and referencing request.
    store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("CREATE TABLE legacy_sessions (session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'local', owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL, agent_id TEXT NOT NULL, model_id TEXT NOT NULL, project_id TEXT NOT NULL, worker_id TEXT NOT NULL, workspace_dir TEXT NOT NULL, access_mode TEXT NOT NULL, history_count INTEGER NOT NULL DEFAULT 0, context_manifest_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(tenant_id,owner_id,conversation_id,agent_id), FOREIGN KEY(project_id) REFERENCES projects(project_id), FOREIGN KEY(worker_id) REFERENCES workers(worker_id))")
        fields = [row[1] for row in conn.execute("PRAGMA table_info(legacy_sessions)")]
        columns = ",".join(fields)
        conn.execute(f"INSERT INTO legacy_sessions ({columns}) SELECT {columns} FROM provider_sessions")
        conn.execute("DROP TABLE provider_sessions")
        conn.execute("ALTER TABLE legacy_sessions RENAME TO provider_sessions")
        conn.execute("UPDATE glasshive_schema_versions SET version=7 WHERE component='runtime_store'")
        conn.execute("CREATE INDEX scoped_legacy_index ON provider_sessions(history_count)")
        conn.execute("CREATE TABLE scope_migration_audit(session_id TEXT)")
        conn.execute("CREATE TRIGGER scoped_legacy_trigger AFTER UPDATE ON provider_sessions BEGIN INSERT INTO scope_migration_audit VALUES (NEW.session_id); END")

    if migration_mode == "rollback":
        migrate = Store._migrate_provider_session_scopes

        def fail_after_rebuild(conn):
            migrate(conn)
            raise RuntimeError("Synthetic failure after rebuilding sessions")

        with monkeypatch.context() as patch:
            patch.setattr(Store, "_migrate_provider_session_scopes", staticmethod(fail_after_rebuild))
            with pytest.raises(RuntimeError, match="Synthetic failure"):
                Store(str(db_path))
        with sqlite3.connect(db_path) as conn:
            assert "actor_kind" not in {row[1] for row in conn.execute("PRAGMA table_info(provider_sessions)")}
            assert conn.execute("SELECT version FROM glasshive_schema_versions WHERE component='runtime_store'").fetchone()[0] == 7
            assert conn.execute("SELECT COUNT(*) FROM provider_session_visible_admissions").fetchone()[0] == 1
    if migration_mode == "concurrent":
        begin = store_module.begin_schema_migration
        barrier = Barrier(2)

        def simultaneous_begin(conn):
            barrier.wait(timeout=5)
            begin(conn)

        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
            patch.setattr(store_module, "begin_schema_migration", simultaneous_begin)
            instances = list(pool.map(lambda _: Store(str(db_path)), range(2)))
        reopened = instances[0]
        instances[1].close()
    else:
        reopened = Store(str(db_path))
    try:
        migrated = reopened.get_provider_session_by_id(legacy["session_id"])
        assert {field: migrated[field] for field in fields} == {field: legacy[field] for field in fields}
        assert (migrated["actor_kind"], migrated["origin"]) == ("external_user", "interactive")
        assert reopened.get_provider_request(request["request_id"])["session_id"] == legacy["session_id"]
        other_worker = reopened.create_worker(
            project_id=project["project_id"], owner_id="owner", name="Scheduled conversation",
            role="conversation-agent", profile="codex-cli", backend="", runtime="codex-cli", model="stub",
        )
        scheduled = reopened.upsert_provider_session(
            tenant_id="local", owner_id="owner", conversation_id="same-conversation", agent_id="same-agent",
            actor_kind="system", origin="scheduler", model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"], worker_id=other_worker["worker_id"], workspace_dir=str(tmp_path), access_mode="workspace",
        )
        assert scheduled["session_id"] != legacy["session_id"]
        assert reopened.get_provider_session(tenant_id="local", owner_id="owner", conversation_id="same-conversation", agent_id="same-agent")["session_id"] == legacy["session_id"]
        with reopened._connect() as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("SELECT message_key FROM provider_session_visible_admissions").fetchone()[0] == "msg:retained"
            assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('scoped_legacy_index', 'scoped_legacy_trigger')")} == {"scoped_legacy_index", "scoped_legacy_trigger"}
        reopened.update_provider_session_history(legacy["session_id"], history_count=8)
        with reopened._connect() as conn:
            assert conn.execute("SELECT session_id FROM scope_migration_audit").fetchone()[0] == legacy["session_id"]
    finally:
        reopened.close()


def test_provider_session_advancement_is_exactly_once_and_cursor_guarded(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, "provider advancement")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={
            "messages": 0,
            "main_context_protocol": "main_context_v1",
            "owner_main_context_version": 7,
            "context_generation": 7,
        },
    )
    first, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="first-attempt",
        message_id="message-a",
        stream_id="stream-a",
        requested_history_count=1,
        replay_decision={
            "base_cursor": 0,
            "advancement_key": "logical-turn-a:2",
            "main_context_protocol": "main_context_v1",
            "logical_turn_id": "logical-turn-a",
            "logical_turn_revision": 2,
            "admitted_visible_message_keys": ["msg:user-a"],
        },
    )
    second, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="second-attempt",
        message_id="message-b",
        stream_id="stream-b",
        requested_history_count=2,
        replay_decision={
            "base_cursor": 0,
            "advancement_key": "logical-turn-b:1",
            "main_context_protocol": "main_context_v1",
            "logical_turn_id": "logical-turn-b",
            "logical_turn_revision": 1,
            "admitted_visible_message_keys": ["msg:user-b"],
        },
    )
    store.update_provider_request(first["request_id"], state="completed")
    store.update_provider_request(second["request_id"], state="completed")

    advanced = store.advance_provider_session_history(
        session["session_id"],
        request_id=second["request_id"],
        advancement_key="logical-turn-b:1",
        expected_history_count=0,
        history_count=3,
        context_manifest={
            "messages": 3,
            "accepted_visible_message_keys": ["msg:user-b", "msg:assistant-b"],
            "owner_main_context_version": 3,
            "context_generation": 3,
            "compactions": [{"kind": "newer"}],
            "excluded": {"native_prefix_bytes": 120},
            "provider_context_epoch": "newer-epoch",
            "observed_chars_per_token": 5.0,
            "last_request_id": second["request_id"],
            "last_accepted_replay_decision_v1": json.loads(
                second["replay_decision_json"]
            ),
        },
    )
    duplicate = store.advance_provider_session_history(
        session["session_id"],
        request_id=second["request_id"],
        advancement_key="logical-turn-b:1",
        expected_history_count=0,
        history_count=3,
        context_manifest={
            "messages": 3,
            "accepted_visible_message_keys": ["msg:user-b", "msg:assistant-b"],
        },
    )
    stale = store.advance_provider_session_history(
        session["session_id"],
        request_id=first["request_id"],
        advancement_key="logical-turn-a:2",
        expected_history_count=0,
        history_count=2,
        context_manifest={
            "messages": 2,
            "accepted_visible_message_keys": ["msg:user-a", "msg:assistant-a"],
            "owner_main_context_version": 9,
            "context_generation": 9,
            "compactions": [{"kind": "stale"}],
            "excluded": {"native_prefix_bytes": 20},
            "provider_context_epoch": "stale-epoch",
            "observed_chars_per_token": 2.0,
            "last_request_id": first["request_id"],
            "last_accepted_replay_decision_v1": json.loads(
                first["replay_decision_json"]
            ),
        },
    )

    assert advanced["advance_status"] == "advanced"
    assert duplicate["advance_status"] == "already_advanced"
    assert stale["advance_status"] == "reconciled_stale"
    current = store.get_provider_session_by_id(session["session_id"])
    assert current["history_count"] == 3
    manifest = json.loads(current["context_manifest_json"])
    assert manifest["accepted_advancement_keys"] == ["logical-turn-b:1", "logical-turn-a:2"]
    assert manifest["accepted_visible_message_keys"] == [
        "msg:user-b",
        "msg:assistant-b",
        "msg:user-a",
        "msg:assistant-a",
    ]
    assert manifest["history_cursor_authority"] == "stable_message_keys_v1"
    assert manifest["owner_main_context_version"] == 9
    assert manifest["context_generation"] == 9
    assert manifest["admission_sequence"] == 2
    assert manifest["last_accepted_admission_sequence"] == 2
    assert manifest["last_request_id"] == second["request_id"]
    assert manifest["last_accepted_request_id"] == second["request_id"]
    assert manifest["last_accepted_advancement_key"] == "logical-turn-b:1"
    assert manifest["last_accepted_replay_decision_v1"]["logical_turn_id"] == (
        "logical-turn-b"
    )
    assert manifest["compactions"] == [{"kind": "newer"}]
    assert manifest["excluded"] == {"native_prefix_bytes": 120}
    assert manifest["provider_context_epoch"] == "newer-epoch"
    assert manifest["observed_chars_per_token"] == 5.0
    assert json.loads(
        store.get_provider_request(first["request_id"])["replay_decision_json"]
    )["admission_state"] == "accepted"
    assert json.loads(
        store.get_provider_request(second["request_id"])["replay_decision_json"]
    )["admission_state"] == "accepted"

    calibrated = store.record_provider_session_usage_calibration(
        session["session_id"],
        request_id=second["request_id"],
        prompt_tokens=100,
        total_tokens=120,
        observed_chars_per_token=4.0,
    )
    first_calibration = json.loads(calibrated["context_manifest_json"])
    duplicate_calibration = store.record_provider_session_usage_calibration(
        session["session_id"],
        request_id=second["request_id"],
        prompt_tokens=1,
        total_tokens=1,
        observed_chars_per_token=8.0,
    )
    duplicate_manifest = json.loads(duplicate_calibration["context_manifest_json"])
    assert duplicate_manifest["accepted_visible_message_keys"] == (
        manifest["accepted_visible_message_keys"]
    )
    assert duplicate_manifest["last_accepted_admission_sequence"] == 2
    assert duplicate_manifest["observed_chars_per_token"] == (
        first_calibration["observed_chars_per_token"]
    )

    first_response = store.set_provider_request_response_if_empty(
        second["request_id"], '{"winner":"first"}'
    )
    second_response = store.set_provider_request_response_if_empty(
        second["request_id"], '{"winner":"second"}'
    )
    assert json.loads(first_response["response_json"])["winner"] == "first"
    assert json.loads(second_response["response_json"])["winner"] == "first"

    truncated_manifest = dict(duplicate_manifest)
    truncated_manifest["accepted_visible_message_keys"] = []
    store.update_provider_session_history(
        session["session_id"],
        history_count=3,
        context_manifest=truncated_manifest,
    )
    assert "msg:user-b" in store.get_provider_session_admission_state(
        session["session_id"]
    )["accepted_visible_message_keys"]
    with pytest.raises(ProviderAdmissionConflictError):
        store.create_provider_request(
            tenant_id="local",
            owner_id="owner",
            session_id=session["session_id"],
            idempotency_key="late-replay-attempt",
            message_id="late-replay-message",
            stream_id="late-replay-stream",
            requested_history_count=4,
            replay_decision={
                "main_context_protocol": "main_context_v1",
                "advancement_key": "late-replay-turn:1",
                "logical_turn_id": "late-replay-turn",
                "response_message_key": "msg:late-replay-assistant",
                "admitted_visible_message_keys": ["msg:user-b"],
            },
        )
    with pytest.raises(ProviderAdmissionConflictError):
        store.create_provider_request(
            tenant_id="local",
            owner_id="owner",
            session_id=session["session_id"],
            idempotency_key="late-advancement-replay",
            message_id="late-advancement-message",
            stream_id="late-advancement-stream",
            requested_history_count=4,
            replay_decision={
                "main_context_protocol": "main_context_v1",
                "advancement_key": "logical-turn-b:1",
                "logical_turn_id": "logical-turn-b",
                "response_message_key": "msg:new-assistant",
                "admitted_visible_message_keys": ["msg:new-user"],
            },
        )

    migration_manifest = dict(truncated_manifest)
    migration_manifest["accepted_visible_message_keys"] = ["msg:migrated-visible"]
    store.update_provider_session_history(
        session["session_id"], history_count=3, context_manifest=migration_manifest
    )
    with store._connect() as conn:
        conn.execute("DROP TABLE provider_session_visible_admissions")
        conn.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            ("provider_visible_admissions_v1",),
        )
    reopened = Store(str(tmp_path / "runtime.db"))
    assert "msg:migrated-visible" in reopened.get_provider_session_admission_state(
        session["session_id"]
    )["accepted_visible_message_keys"]


def test_failed_provider_request_releases_its_visible_input_reservation(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, "provider reservation retry")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )
    first, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="first-attempt",
        message_id="message-a",
        stream_id="stream-a",
        requested_history_count=1,
        replay_decision={
            "advancement_key": "logical-turn-a:1",
            "main_context_protocol": "main_context_v1",
            "logical_turn_id": "logical-turn-a",
            "admitted_visible_message_keys": ["msg:user-a"],
        },
    )
    assert store.get_provider_session_admission_state(session["session_id"])[
        "reserved_visible_message_keys"
    ] == ["msg:user-a"]

    with pytest.raises(ProviderAdmissionConflictError):
        store.create_provider_request(
            tenant_id="local",
            owner_id="owner",
            session_id=session["session_id"],
            idempotency_key="overlapping-attempt",
            message_id="message-overlap",
            stream_id="stream-overlap",
            requested_history_count=1,
            replay_decision={
                "advancement_key": "logical-turn-overlap:1",
                "main_context_protocol": "main_context_v1",
                "logical_turn_id": "logical-turn-overlap",
                "admitted_visible_message_keys": ["msg:user-a"],
            },
        )

    store.update_provider_request(first["request_id"], state="failed")
    assert store.get_provider_session_admission_state(session["session_id"])[
        "reserved_visible_message_keys"
    ] == []
    retry, created = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="retry-attempt",
        message_id="message-a-retry",
        stream_id="stream-a-retry",
        requested_history_count=1,
        replay_decision={
            "advancement_key": "logical-turn-a:1",
            "main_context_protocol": "main_context_v1",
            "logical_turn_id": "logical-turn-a",
            "admitted_visible_message_keys": ["msg:user-a"],
        },
    )
    assert created is True
    assert json.loads(retry["replay_decision_json"])["admission_state"] == "reserved"


def test_stale_unassigned_provider_request_releases_reservation_after_crash(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, "provider admission crash")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )
    request, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="crashed-attempt",
        message_id="message-a",
        stream_id="stream-a",
        requested_history_count=1,
        replay_decision={
            "advancement_key": "logical-turn-a:1",
            "main_context_protocol": "main_context_v1",
            "logical_turn_id": "logical-turn-a",
            "admitted_visible_message_keys": ["msg:user-a"],
        },
    )
    assert store.get_provider_session_admission_state(session["session_id"])[
        "reserved_visible_message_keys"
    ] == ["msg:user-a"]

    released = store.fail_stale_unassigned_provider_requests(
        request_id=request["request_id"],
        updated_before=request["updated_at"],
    )

    assert len(released) == 1
    assert released[0]["state"] == "failed"
    assert released[0]["fallback_state"] == "admission_interrupted"
    assert store.get_provider_session_admission_state(session["session_id"])[
        "reserved_visible_message_keys"
    ] == []


def test_provider_run_creation_and_request_attachment_share_one_transaction(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, "provider atomic run attach")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )
    request, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="atomic-attempt",
        message_id="atomic-message",
        stream_id="atomic-stream",
        requested_history_count=1,
        replay_decision={
            "main_context_protocol": "main_context_v1",
            "advancement_key": "atomic-turn:1",
            "logical_turn_id": "atomic-turn",
            "response_message_key": "msg:atomic-assistant",
            "admitted_visible_message_keys": ["msg:atomic-user"],
        },
    )
    original_insert = store._insert_run_row

    def fail_after_insert(conn, data):
        original_insert(conn, data)
        raise RuntimeError("synthetic crash after run insert")

    monkeypatch.setattr(store, "_insert_run_row", fail_after_insert)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        store.create_and_attach_provider_run(
            request_id=request["request_id"],
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            instruction="Run this admitted turn once.",
        )

    assert store.get_provider_request(request["request_id"])["run_id"] is None
    assert store.list_runs_for_worker(worker["worker_id"]) == []

    monkeypatch.setattr(store, "_insert_run_row", original_insert)
    run, created = store.create_and_attach_provider_run(
        request_id=request["request_id"],
        worker_id=worker["worker_id"],
        project_id=project["project_id"],
        instruction="Run this admitted turn once.",
    )

    assert created is True
    assert store.get_provider_request(request["request_id"])["run_id"] == run["run_id"]
    assert [item["run_id"] for item in store.list_runs_for_worker(worker["worker_id"])] == [
        run["run_id"]
    ]


def test_two_store_connections_cannot_reserve_the_same_visible_input(tmp_path):
    database_path = tmp_path / "runtime.db"
    store = Store(str(database_path))
    project = _project(store, "provider admission concurrency")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )
    barrier = Barrier(2)

    def reserve(suffix: str) -> str:
        connection_store = Store(str(database_path))
        barrier.wait(timeout=2)
        try:
            connection_store.create_provider_request(
                tenant_id="local",
                owner_id="owner",
                session_id=session["session_id"],
                idempotency_key=f"attempt-{suffix}",
                message_id=f"message-{suffix}",
                stream_id=f"stream-{suffix}",
                requested_history_count=1,
                replay_decision={
                    "advancement_key": f"logical-turn-{suffix}:1",
                    "main_context_protocol": "main_context_v1",
                    "logical_turn_id": f"logical-turn-{suffix}",
                    "admitted_visible_message_keys": ["msg:shared-user-input"],
                },
            )
            return "reserved"
        except ProviderAdmissionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(reserve, ("a", "b")))

    assert outcomes == ["conflict", "reserved"]


@pytest.mark.parametrize(
    ("shared_field", "shared_value"),
    [
        ("advancement_key", "logical-turn-shared:1"),
        ("response_message_key", "msg:assistant-shared"),
    ],
)
def test_provider_admission_reserves_logical_turn_and_response_identity(
    tmp_path, shared_field, shared_value
):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, f"provider {shared_field} identity")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )

    def decision(suffix: str) -> dict:
        value = {
            "main_context_protocol": "main_context_v1",
            "advancement_key": f"logical-turn-{suffix}:1",
            "logical_turn_id": f"logical-turn-{suffix}",
            "response_message_key": f"msg:assistant-{suffix}",
            "admitted_visible_message_keys": [f"msg:user-{suffix}"],
        }
        value[shared_field] = shared_value
        return value

    store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="attempt-a",
        message_id="message-a",
        stream_id="stream-a",
        requested_history_count=1,
        replay_decision=decision("a"),
    )

    with pytest.raises(ProviderAdmissionConflictError):
        store.create_provider_request(
            tenant_id="local",
            owner_id="owner",
            session_id=session["session_id"],
            idempotency_key="attempt-b",
            message_id="message-b",
            stream_id="stream-b",
            requested_history_count=1,
            replay_decision=decision("b"),
        )


def test_family_continuation_cannot_clear_a_live_same_turn_reservation(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, "family continuation reservation")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )
    decision = {
        "main_context_protocol": "main_context_v1",
        "advancement_key": "logical-turn-a:1",
        "logical_turn_id": "logical-turn-a",
        "logical_turn_revision": 1,
        "response_message_key": "msg:assistant-a",
        "admitted_visible_message_keys": ["msg:user-a"],
        "main_context_snapshot_sha256": "a" * 64,
        "context_epoch": "b" * 64,
    }
    first, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="turn-a",
        message_id="message-a",
        stream_id="stream-a",
        requested_history_count=1,
        replay_decision=decision,
    )
    store.update_provider_request(first["request_id"], state="completed")
    store.advance_provider_session_history(
        session["session_id"],
        request_id=first["request_id"],
        advancement_key="logical-turn-a:1",
        expected_history_count=0,
        history_count=1,
        context_manifest={
            "messages": 1,
            "accepted_visible_message_keys": ["msg:user-a", "msg:assistant-a"],
        },
    )

    store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="turn-a:graph:2",
        base_idempotency_key="turn-a",
        message_id="message-a",
        stream_id="stream-a-2",
        requested_history_count=1,
        replay_decision={**decision, "admitted_visible_message_keys": []},
    )

    with pytest.raises(ProviderAdmissionConflictError):
        store.create_provider_request(
            tenant_id="local",
            owner_id="owner",
            session_id=session["session_id"],
            idempotency_key="turn-a:graph:3",
            base_idempotency_key="turn-a",
            message_id="message-a",
            stream_id="stream-a-3",
            requested_history_count=1,
            replay_decision={**decision, "admitted_visible_message_keys": []},
        )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("main_context_snapshot_sha256", "c" * 64),
        ("context_epoch", "d" * 64),
    ],
)
def test_family_continuation_requires_the_same_admitted_main_snapshot(
    tmp_path, field, changed
):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, f"family continuation {field}")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )
    decision = {
        "main_context_protocol": "main_context_v1",
        "advancement_key": "logical-turn-a:1",
        "logical_turn_id": "logical-turn-a",
        "logical_turn_revision": 1,
        "response_message_key": "msg:assistant-a",
        "admitted_visible_message_keys": ["msg:user-a"],
        "main_context_snapshot_sha256": "a" * 64,
        "context_epoch": "b" * 64,
    }
    first, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="turn-a",
        message_id="message-a",
        stream_id="stream-a",
        requested_history_count=1,
        replay_decision=decision,
    )
    store.update_provider_request(first["request_id"], state="completed")
    store.advance_provider_session_history(
        session["session_id"],
        request_id=first["request_id"],
        advancement_key="logical-turn-a:1",
        expected_history_count=0,
        history_count=1,
        context_manifest={
            "messages": 1,
            "accepted_visible_message_keys": ["msg:user-a", "msg:assistant-a"],
        },
    )

    with pytest.raises(ProviderAdmissionConflictError):
        store.create_provider_request(
            tenant_id="local",
            owner_id="owner",
            session_id=session["session_id"],
            idempotency_key="turn-a:graph:2",
            base_idempotency_key="turn-a",
            message_id="message-a",
            stream_id="stream-a-2",
            requested_history_count=1,
            replay_decision={
                **decision,
                field: changed,
                "admitted_visible_message_keys": [],
            },
        )


def test_legacy_logical_turn_does_not_create_a_stable_admission_reservation(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = _project(store, "legacy logical turn")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner",
        conversation_id="conversation-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
        history_count=0,
        context_manifest={"messages": 0, "main_context_protocol": "main_context_v1"},
    )
    request, created = store.create_provider_request(
        tenant_id="local",
        owner_id="owner",
        session_id=session["session_id"],
        idempotency_key="legacy-attempt",
        message_id="legacy-message",
        stream_id="legacy-stream",
        requested_history_count=1,
        replay_decision={
            "advancement_key": "legacy-turn:1",
            "logical_turn_id": "legacy-turn",
            "response_message_key": "msg:legacy-assistant",
            "admitted_visible_message_keys": ["msg:legacy-user"],
        },
    )

    assert created is True
    assert "admission_state" not in json.loads(request["replay_decision_json"])
    assert store.get_provider_session_admission_state(session["session_id"])[
        "reserved_visible_message_keys"
    ] == []


def test_owner_main_context_rejects_stale_or_duplicate_logical_turn_revisions(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    common = {
        "tenant_id": "local",
        "owner_id": "owner",
        "agent_id": "agent-a",
        "continuity_domain_id": "domain-a",
    }

    current = store.append_provider_main_context_turn(
        **common,
        request_id="request-new",
        turn={
            "logical_turn_id": "logical-turn-a",
            "logical_turn_revision": 2,
            "conversation_id": "conversation-a",
            "user_text": "Use the corrected request.",
            "assistant_text": "Corrected answer.",
        },
    )
    duplicate = store.append_provider_main_context_turn(
        **common,
        request_id="request-retry",
        turn={
            "logical_turn_id": "logical-turn-a",
            "logical_turn_revision": 2,
            "conversation_id": "conversation-a",
            "user_text": "Use the corrected request.",
            "assistant_text": "Retry answer must not commit twice.",
        },
    )
    stale = store.append_provider_main_context_turn(
        **common,
        request_id="request-old",
        turn={
            "logical_turn_id": "logical-turn-a",
            "logical_turn_revision": 1,
            "conversation_id": "conversation-a",
            "user_text": "Old request.",
            "assistant_text": "Old delayed answer.",
        },
    )

    assert current["version"] == 1
    assert duplicate["version"] == 1
    assert stale["version"] == 1
    body = json.loads(stale["context_json"])
    assert [turn["logical_turn_revision"] for turn in body["turns"]] == [2]
    assert body["turns"][0]["assistant_text"] == "Corrected answer."


def test_store_enforces_foreign_keys_on_every_connection_and_reopen(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))

    first_project = _project(store, "first connection")
    _assert_connection_enforces_foreign_keys(
        store,
        project_id=first_project["project_id"],
    )

    second_project = _project(store, "second connection")
    _assert_connection_enforces_foreign_keys(
        store,
        project_id=second_project["project_id"],
    )

    reopened = Store(str(db_path))
    reopened_project = _project(reopened, "reopened store")
    _assert_connection_enforces_foreign_keys(
        reopened,
        project_id=reopened_project["project_id"],
    )


def test_store_enforces_foreign_keys_after_additive_migration(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    with store._connect() as conn:
        try:
            conn.execute("ALTER TABLE workers DROP COLUMN compute_released_at")
        except sqlite3.OperationalError as exc:
            pytest.skip(f"SQLite runtime does not support DROP COLUMN: {exc}")

    migrated = Store(str(db_path))
    with migrated._connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(workers)")
        }
        assert "compute_released_at" in columns
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    migrated_project = _project(migrated, "post migration")
    _assert_connection_enforces_foreign_keys(
        migrated,
        project_id=migrated_project["project_id"],
    )


def test_runtime_invocation_marker_additive_migration_preserves_legacy_runs(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    project = _project(store, "runtime invocation migration")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Invocation migration worker",
        role="worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    legacy_run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Preserve this queued legacy run.",
        state="queued",
    )
    with store._connect() as conn:
        try:
            conn.execute("ALTER TABLE runs DROP COLUMN runtime_invoked_at")
        except sqlite3.OperationalError as exc:
            pytest.skip(f"SQLite runtime does not support DROP COLUMN: {exc}")

    migrated = Store(str(db_path))
    with migrated._connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(runs)")
        }
        assert "runtime_invoked_at" in columns
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (migrated.get_run(legacy_run["run_id"]) or {})[
        "runtime_invoked_at"
    ] is None

    reopened = Store(str(db_path))
    assert (reopened.get_run(legacy_run["run_id"]) or {})[
        "runtime_invoked_at"
    ] is None


def test_store_reopen_fails_closed_on_preexisting_foreign_key_violation(tmp_path):
    db_path = tmp_path / "runtime.db"
    Store(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE foreign_key_reopen_probe (
                probe_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO foreign_key_reopen_probe (probe_id, project_id) "
            "VALUES ('orphan', 'missing-project')"
        )

    with pytest.raises(
        RuntimeError,
        match="SQLite foreign-key integrity check failed",
    ):
        Store(str(db_path))


def test_store_closes_and_fails_when_foreign_keys_cannot_be_enabled(
    tmp_path,
    monkeypatch,
):
    class DisabledForeignKeyConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, statement: str):
            _ = statement
            return self

        def fetchone(self):
            return (0,)

        def close(self) -> None:
            self.closed = True

    connection = DisabledForeignKeyConnection()
    monkeypatch.setattr(
        store_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(
        RuntimeError,
        match="SQLite foreign-key enforcement could not be enabled",
    ):
        Store(str(tmp_path / "runtime.db"))

    assert connection.closed is True


def test_store_connection_context_closes_file_descriptors_without_gc(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    garbage_collection_was_enabled = gc.isenabled()
    if garbage_collection_was_enabled:
        gc.disable()

    try:
        before = _open_file_descriptor_count()
        for _ in range(256):
            with store._connect() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
        after = _open_file_descriptor_count()

        assert after - before <= 4
    finally:
        if garbage_collection_was_enabled:
            gc.enable()
        gc.collect()
