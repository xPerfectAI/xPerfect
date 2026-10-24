"""Standalone target acceptance through the existing service, clock and outbox."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
import time

import pytest
from standardwebhooks.webhooks import Webhook

from workers_projects_runtime import schedule_targets
from workers_projects_runtime.control_plane import ControlPlaneConflict
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.schedule_webhooks import WebhookLimits, WebhookNetworkPolicy, WebhookTransport
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store

SECRET = "whsec_" + base64.b64encode(b"synthetic-owner-signature-key-123").decode()
TENANT, OWNER = "tenant-synthetic", "owner-synthetic"


def now():
    return datetime.now(timezone.utc)


def wait_for(check, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("Expected local target state did not settle")


@pytest.fixture
def service_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    services = []

    def build(name="runtime"):
        store = Store(str(tmp_path / f"{name}.db"))
        service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False, start_background_consumers=False)
        services.append(service)
        return service

    yield build
    for service in services:
        if not getattr(service, "_fixture_closed", False):
            service.shutdown()
            service.store.close()


def destination(service, *, owner=OWNER, url="https://example.invalid/private?token=synthetic-url-secret", secret=SECRET, payload_mode="envelope"):
    return service.create_webhook_destination(tenant_id=TENANT, owner_id=owner, name="Receiver", url=url, secret=secret,
                                               headers={"Authorization": "Bearer synthetic-private-auth"}, payload_mode=payload_mode)


def create(service, dest, *, owner=OWNER, key="synthetic-create-1", request_data=None, due=None, **extra):
    return service.create_target_schedule(tenant_id=TENANT, owner_id=owner,
        target={"kind": "webhook", "destination_id": dest["destination_id"], "request_data": request_data or {"request_ref": "opaque-1"}},
        idempotency_key=key, recurrence_type="once", starts_at=(due or now() + timedelta(hours=1)).isoformat(), **extra)


def rows(service, table):
    with service.store._connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def pending_callback(service, definition, key="synthetic-now-1"):
    service.run_recurring_schedule_now(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER, idempotency_token=key)
    return rows(service, "callback_outbox")[-1]


def worker(service):
    project = service.store.create_project(OWNER, "Synthetic", "Native work", "codex-cli", tenant_id=TENANT)
    return service.store.create_worker(project_id=project["project_id"], owner_id=OWNER, tenant_id=TENANT,
        name="Worker", role="operator", profile="codex-cli", backend="openclaw", runtime="codex-cli", model="stub/codex-cli")


def test_plain_webhook_has_no_dummy_worker_provider_or_run_and_reloads(service_factory):
    service = service_factory()
    dest = destination(service)
    definition = create(service, dest)
    assert definition["worker_id"] is None and definition["project_id"] is None
    assert definition["target"]["kind"] == "webhook"
    assert not rows(service, "workers") and not rows(service, "projects") and not rows(service, "runs")
    reloaded = service.get_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER)
    assert reloaded["target"] == definition["target"] and reloaded["version"] == 1
    assert service.get_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id="other-owner") is None
    public = json.dumps(service.list_webhook_destinations(tenant_id=TENANT, owner_id=OWNER)) + json.dumps(reloaded)
    for private in (SECRET, "synthetic-url-secret", "synthetic-private-auth"):
        assert private not in public


def test_concurrent_lost_reply_create_is_one_definition_and_changed_request_conflicts(service_factory):
    service = service_factory()
    dest = destination(service)
    due = now() + timedelta(hours=1)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: create(service, dest, due=due), range(8)))
    assert len({item["definition_id"] for item in results}) == 1
    assert len(rows(service, "recurring_schedule_definitions")) == 1
    assert create(service, dest, due=due)["definition_id"] == results[0]["definition_id"]
    with pytest.raises(ControlPlaneConflict, match="conflict"):
        create(service, dest, due=due, request_data={"request_ref": "different"})


def test_create_replay_survives_destination_revocation_without_new_admission(service_factory):
    service = service_factory()
    dest = destination(service)
    due = now() + timedelta(hours=1)
    accepted = create(service, dest, due=due)
    service.update_webhook_destination(dest["destination_id"], tenant_id=TENANT, owner_id=OWNER, enabled=False)
    replay = create(service, dest, due=due)
    assert replay["definition_id"] == accepted["definition_id"]
    assert len(rows(service, "recurring_schedule_definitions")) == 1


def test_destination_scope_permissions_rotation_and_protected_file(service_factory):
    service = service_factory()
    dest = destination(service)
    assert service.list_webhook_destinations(tenant_id=TENANT, owner_id="other-owner") == []
    with pytest.raises(ValueError):
        create(service, dest, owner="other-owner")
    path = service.webhook_destinations._path(TENANT, OWNER, dest["destination_id"])
    assert path.stat().st_mode & 0o077 == 0 and path.parent.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        service.update_webhook_destination(dest["destination_id"], tenant_id=TENANT, owner_id=OWNER, secret="YWJj")
    value = service.webhook_destinations.get(TENANT, OWNER, dest["destination_id"])
    assert value["version"] == 1 and value["secret"] == SECRET


def test_worker_target_retire_keeps_original_worker_project_and_context(service_factory):
    service = service_factory()
    selected = worker(service)
    definition = service.create_target_schedule(tenant_id=TENANT, owner_id=OWNER,
        target={"kind": "worker", "worker_id": selected["worker_id"]}, instruction="Original request",
        idempotency_key="synthetic-worker-1", recurrence_type="interval", interval_seconds=3600)
    retired = service.retire_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER)
    assert retired["worker_id"] == selected["worker_id"]
    assert retired["project_id"] == selected["project_id"]
    assert retired["instruction"] == "Original request"


def test_occurrence_snapshot_is_immutable_and_edit_supersedes_pending(service_factory):
    service = service_factory()
    dest = destination(service)
    definition = create(service, dest)
    original = pending_callback(service, definition)
    updated = service.update_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER,
        updates={"target": {"kind": "webhook", "destination_id": dest["destination_id"], "request_data": {"request_ref": "new"}}})
    assert updated["version"] == 2
    saved = rows(service, "callback_outbox")[0]
    assert saved["payload_json"] == original["payload_json"] and saved["status"] == "superseded"
    assert json.loads(saved["payload_json"])["request_data"]["request_ref"] == "opaque-1"
    with service.store._connect() as conn:
        with pytest.raises(Exception, match="immutable"):
            conn.execute("UPDATE callback_outbox SET payload_json='{}' WHERE callback_id=?", (saved["callback_id"],))
    assert not rows(service, "runs")


def test_pause_cancels_pending_and_cancel_after_claim_fences_send(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback["callback_id"])
    assert claimed
    service.deactivate_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER)
    assert schedule_targets.fence_callback(service, claimed) == "cancelled"
    assert rows(service, "callback_outbox")[0]["cancel_requested_at"]


def test_principal_revocation_blocks_materialization_and_existing_send(service_factory):
    service = service_factory()
    dest = destination(service)
    definition = create(service, dest)
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback["callback_id"])
    service.set_schedule_principal_authority(tenant_id=TENANT, owner_id=OWNER, enabled=False)
    assert schedule_targets.fence_callback(service, claimed) == "principal_disabled"
    with pytest.raises(ValueError):
        create(service, dest, key="new-after-disable")


def test_retry_persists_backoff_and_stale_completion_cannot_overwrite_accepted(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    first = service.store.claim_pending_callback(callback["callback_id"])
    assert schedule_targets.finish_callback(service.store, first, state="unacknowledged", error="connection_failed", retryable=True)
    persisted = rows(service, "callback_outbox")[0]
    assert persisted["status"] == "pending" and persisted["next_attempt_at"] > now().isoformat()
    assert not schedule_targets.due_callback_records(service.store, now().isoformat())
    from unittest.mock import patch
    with patch("workers_projects_runtime.store.utc_now", return_value=(now()+timedelta(seconds=3)).isoformat()):
        second = service.store.claim_pending_callback(callback["callback_id"])
    assert second and second["delivery_generation"] > first["delivery_generation"]
    assert schedule_targets.finish_callback(service.store, second, state="accepted")
    assert not schedule_targets.finish_callback(service.store, first, state="unacknowledged", retryable=True)
    assert rows(service, "callback_outbox")[0]["status"] == "http_accepted"
    assert rows(service, "recurring_schedule_occurrences")[0]["outcome"] == "accepted"


def test_crash_lease_not_reclaimed_early_but_expired_generation_recovers(service_factory):
    service = service_factory()
    callback = pending_callback(service, create(service, destination(service)))
    claimed = service.store.claim_pending_callback(callback["callback_id"])
    service.store.reclaim_stale_delivering_callbacks(stale_before=(now()-timedelta(seconds=300)).isoformat())
    assert rows(service, "callback_outbox")[0]["status"] == "delivering"
    with service.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET delivery_lease_expires_at=? WHERE callback_id=?", ((now()-timedelta(seconds=1)).isoformat(), callback["callback_id"]))
    service.store.reclaim_stale_delivering_callbacks(stale_before=(now()-timedelta(seconds=300)).isoformat())
    recovered = service.store.claim_pending_callback(callback["callback_id"])
    assert recovered["delivery_generation"] > claimed["delivery_generation"]
    assert not schedule_targets.finish_callback(service.store, claimed, state="accepted")


def test_expired_first_attempt_and_unacknowledged_recovery_remain_distinct(service_factory):
    service = service_factory()
    definition = create(service, destination(service), misfire_grace_seconds=1)
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback["callback_id"])
    # Keep immutable snapshot intact: simulate an occurrence expiring by advancing
    # the module clock, not editing authority or a persisted request.
    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz or timezone.utc) + timedelta(seconds=10)
    from unittest.mock import patch
    with patch.object(schedule_targets, "datetime", FutureDatetime):
        assert schedule_targets.finish_callback(service.store, claimed, state="unacknowledged", error="attempt_timeout", retryable=True)
    saved = rows(service, "callback_outbox")[0]
    assert saved["status"] == "unacknowledged" and saved["next_attempt_at"] is None
    assert rows(service, "recurring_schedule_occurrences")[0]["state"] == "unacknowledged"


@pytest.fixture
def live_receiver():
    received, mode = [], {"status": 204, "slow": False}
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            Webhook(SECRET).verify(raw, dict(self.headers))
            received.append((self.path, json.loads(raw), dict(self.headers)))
            if self.path == "/slow" and mode["slow"]:
                release.wait(2)
            self.send_response(mode["status"])
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, received, mode, release
    release.set();server.shutdown();server.server_close();thread.join(2)


def local_transport(service, port):
    service.schedule_webhook_transport.close()
    service.schedule_webhook_transport = WebhookTransport(policy=WebhookNetworkPolicy(allow_local_http=True, allowed_ports=(port,)))


def test_actual_due_clock_signed_http_without_any_ai_worker_and_wakes_early(service_factory, live_receiver):
    service = service_factory()
    port, received, _, _ = live_receiver
    local_transport(service, port)
    service._scheduler_thread = Thread(target=service._scheduler_loop, daemon=True)
    service._scheduler_thread.start()
    due = now() + timedelta(seconds=0.2)
    service._scheduler_wake_event.clear()
    definition = create(service, destination(service, url=f"http://127.0.0.1:{port}/due"), due=due)
    assert service._next_scheduler_wait_s(3600) < 1
    receipt = wait_for(lambda: received and received[0])
    assert receipt[1]["type"] == "schedule.due" and receipt[1]["schedule_id"] == definition["definition_id"]
    assert receipt[1]["principal"] == {"tenant_id": TENANT, "owner_id": OWNER}
    saved = wait_for(lambda: (values := rows(service, "callback_outbox")) and values[0]["status"] == "http_accepted" and values[0])
    assert saved["run_id"] is None and saved["worker_id"] is None
    assert not rows(service, "runs") and not rows(service, "workers")
    metrics = json.loads(saved["timings_json"])
    assert "sign_ms" in metrics and "connect_ms" in metrics and "total_ms" in metrics


def test_busy_ai_executor_and_slow_receiver_do_not_hold_healthy_receiver(service_factory, live_receiver):
    service = service_factory()
    port, received, mode, release = live_receiver
    local_transport(service, port)
    mode["slow"] = True
    ai_release = Event()
    blocked = [service.executor.submit(ai_release.wait, 2) for _ in range(service.executor._max_workers)]
    try:
        slow = create(service, destination(service, url=f"http://127.0.0.1:{port}/slow"), key="slow-create")
        fast = create(service, destination(service, url=f"http://127.0.0.1:{port}/fast"), key="fast-create")
        pending_callback(service, slow, "slow-manual")
        pending_callback(service, fast, "fast-manual")
        service.process_pending_schedule_webhooks_once()
        wait_for(lambda: any(path == "/fast" for path, _, _ in received))
        wait_for(lambda: any(row["definition_id"] == fast["definition_id"] and row["status"] == "http_accepted" for row in rows(service, "callback_outbox")))
        assert any(not future.done() for future in blocked)
        assert not release.is_set()
    finally:
        ai_release.set();release.set()


def test_due_fairness_first_round_contains_each_owner(service_factory):
    service = service_factory()
    for owner in (OWNER, "other-owner"):
        dest = destination(service, owner=owner)
        for index in range(3):
            definition = create(service, dest, owner=owner, key=f"create-{owner}-{index}")
            service.run_recurring_schedule_now(definition["definition_id"], tenant_id=TENANT, owner_id=owner, idempotency_token=f"manual-{owner}-{index}")
    first = schedule_targets.due_callback_records(service.store, now().isoformat(), limit=2)
    assert {row["owner_id"] for row in first} == {OWNER, "other-owner"}


def _old_worker_database(service_factory):
    """Restore only the old NOT NULL shape in a synthetic isolated database."""
    import re
    import sqlite3
    service = service_factory("migration")
    selected = worker(service)
    definition = service.create_recurring_schedule(selected["worker_id"], "Legacy instruction",
        recurrence_type="interval", interval_seconds=3600)
    service.store.materialize_recurring_schedule_occurrence(definition["definition_id"],
        expected_next_run_at=definition["next_run_at"], scheduled_for=definition["next_run_at"],
        next_run_at=(now()+timedelta(hours=2)).isoformat(), detected_at=now().isoformat())
    run = service.store.create_run(selected["worker_id"], selected["project_id"], "Legacy callback run")
    service.store.insert_callback_outbox_once(callback_id="cb_legacy_synthetic", project_id=selected["project_id"],
        worker_id=selected["worker_id"], run_id=run["run_id"], event_type="run.queued",
        url="https://example.invalid/callback", payload_json=json.dumps({"event": "run.queued", "callback_ts": 1_700_000_001, "callback_id": "cb_legacy_synthetic", "worker_id": selected["worker_id"], "run_id": run["run_id"]}))
    path = service.store.db_path
    service.shutdown()
    service.store.close()
    service._fixture_closed = True
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("BEGIN IMMEDIATE")
        for table, fields in (("recurring_schedule_definitions", ("project_id", "worker_id")),
                              ("recurring_schedule_occurrences", ("scheduled_run_id",)),
                              ("callback_outbox", ("project_id", "worker_id"))):
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            for field in fields:
                sql = re.sub(r"\b"+field+r" TEXT(?! NOT NULL)", field+" TEXT NOT NULL", sql)
            original = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,)).fetchall()
            temporary = table + "_old_shape"
            sql = re.sub(r"^CREATE TABLE [\"`\[]?"+table+r"[\"`\]]?", "CREATE TABLE "+temporary, sql, count=1)
            conn.execute(sql)
            columns = ','.join('"'+row[1]+'"' for row in conn.execute(f"PRAGMA table_info({table})"))
            conn.execute(f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
            for item in original:
                conn.execute(item[0])
        conn.execute("UPDATE glasshive_schema_versions SET version=10 WHERE component='runtime_store'")
        conn.execute("CREATE TABLE migration_test_audit(value TEXT)")
        conn.execute("CREATE TRIGGER target_migration_custom_trigger AFTER UPDATE ON recurring_schedule_definitions BEGIN INSERT INTO migration_test_audit VALUES(NEW.definition_id); END")
        conn.execute("CREATE INDEX target_migration_custom_index ON recurring_schedule_definitions(instruction)")
        conn.commit()
        tables = ("workers", "runs", "recurring_schedule_definitions", "recurring_schedule_occurrences", "scheduled_runs", "callback_outbox", "callback_trace_events")
        original_rows = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables}
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    return path, original_rows


def test_old_schema_upgrade_retains_rows_callback_ledger_indexes_triggers_and_fks(service_factory):
    path, before = _old_worker_database(service_factory)
    reopened = Store(str(path))
    try:
        with reopened._connect() as conn:
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
            from workers_projects_runtime.store import RUNTIME_STORE_SCHEMA_VERSION
            assert conn.execute("SELECT version FROM glasshive_schema_versions WHERE component='runtime_store'").fetchone()[0] == RUNTIME_STORE_SCHEMA_VERSION
            for table, expected in before.items():
                assert [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] == expected, table
            assert not next(row for row in conn.execute("PRAGMA table_info(recurring_schedule_definitions)") if row["name"] == "worker_id")["notnull"]
            objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'target_migration_custom_%'")}
            assert objects == {"target_migration_custom_trigger", "target_migration_custom_index"}
            definition_id = before["recurring_schedule_definitions"][0]["definition_id"]
            conn.execute("UPDATE recurring_schedule_definitions SET instruction=instruction WHERE definition_id=?", (definition_id,))
            assert conn.execute("SELECT value FROM migration_test_audit").fetchone()[0] == definition_id
    finally:
        reopened.close()


def test_old_schema_upgrade_failure_rolls_back_all_rebuilt_tables(service_factory, monkeypatch):
    import sqlite3
    path, before = _old_worker_database(service_factory)
    original_ensure = schedule_targets.ensure_target_columns

    def fail_after_rebuild(conn):
        assert not next(row for row in conn.execute("PRAGMA table_info(recurring_schedule_definitions)") if row["name"] == "worker_id")["notnull"]
        raise RuntimeError("synthetic interrupted migration")

    monkeypatch.setattr(schedule_targets, "ensure_target_columns", fail_after_rebuild)
    with pytest.raises(RuntimeError, match="synthetic interrupted migration"):
        Store(str(path))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert next(row for row in conn.execute("PRAGMA table_info(recurring_schedule_definitions)") if row["name"] == "worker_id")["notnull"]
        for table, expected in before.items():
            assert [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] == expected, table
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("SELECT version FROM glasshive_schema_versions WHERE component='runtime_store'").fetchone()[0] == 10
    monkeypatch.setattr(schedule_targets, "ensure_target_columns", original_ensure)
    recovered = Store(str(path))
    recovered.close()


def test_due_definition_batch_is_fair_before_outbox_admission(service_factory):
    service = service_factory()
    first_dest = destination(service)
    other_dest = destination(service, owner="other-owner")
    instant = now()
    for index in range(3):
        create(service, first_dest, key=f"first-owner-{index}", due=instant-timedelta(seconds=10-index))
    create(service, other_dest, owner="other-owner", key="other-owner-due", due=instant-timedelta(seconds=1))
    selected = service.store.list_due_recurring_schedule_definitions(instant.isoformat(), scheduler_owner="native", limit=2)
    assert {item["owner_id"] for item in selected} == {OWNER, "other-owner"}


def test_revoked_destination_does_not_block_pause_or_existing_run_receipt(service_factory):
    service = service_factory()
    dest = destination(service)
    definition = create(service, dest)
    first = service.run_recurring_schedule_now(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER, idempotency_token="stable-run-now")
    service.update_webhook_destination(dest["destination_id"], tenant_id=TENANT, owner_id=OWNER, enabled=False)
    replay = service.run_recurring_schedule_now(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER, idempotency_token="stable-run-now")
    assert replay["occurrence_id"] == first["occurrence_id"]
    assert first["occurrence_created"] is True and replay["occurrence_created"] is False
    with pytest.raises(ValueError, match="unavailable"):
        service.run_recurring_schedule_now(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER, idempotency_token="new-run-after-revocation")
    paused = service.deactivate_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER)
    assert paused["active"] is False


@pytest.mark.parametrize("kind", ["worker", "webhook"])
def test_manual_receipt_distinguishes_new_request_retry_and_atomic_race(service_factory, monkeypatch, kind):
    service = service_factory()
    if kind == "webhook":
        definition = create(service, destination(service))
    else:
        selected = worker(service)
        definition = service.create_target_schedule(
            tenant_id=TENANT, owner_id=OWNER,
            target={"kind": "worker", "worker_id": selected["worker_id"]},
            instruction="Synthetic saved work", idempotency_key="manual-receipt-worker",
            recurrence_type="interval", interval_seconds=3600,
        )
    def trigger(key):
        return service.run_recurring_schedule_now(
            definition["definition_id"], tenant_id=TENANT, owner_id=OWNER, idempotency_token=key,
        )
    first = trigger("same-request-key")
    replay = trigger("same-request-key")
    separate = trigger("different-request-key")
    assert first["occurrence_created"] is True
    assert replay["occurrence_created"] is False
    assert replay["occurrence_id"] == first["occurrence_id"]
    assert separate["occurrence_created"] is True
    assert separate["occurrence_id"] != first["occurrence_id"]
    assert all(item["status"] == "scheduled" for item in (first, replay, separate))
    # Exercise the transactional existing branch, not just the recovery fast path.
    monkeypatch.setattr(schedule_targets, "manual_receipt", lambda *_args: None)
    atomic_replay = trigger("same-request-key")
    assert atomic_replay["occurrence_created"] is False
    assert atomic_replay["occurrence_id"] == first["occurrence_id"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        concurrent = list(pool.map(lambda _: trigger("concurrent-request-key"), range(8)))
    assert sum(item["occurrence_created"] is True for item in concurrent) == 1
    assert len({item["occurrence_id"] for item in concurrent}) == 1
    assert len(rows(service, "recurring_schedule_occurrences")) == 3
    if kind == "webhook":
        assert len(rows(service, "callback_outbox")) == 3
    else:
        assert len(rows(service, "scheduled_runs")) == 3
    # Creation/replay is a command receipt, never persisted occurrence state.
    assert all("occurrence_created" not in row for row in rows(service, "recurring_schedule_occurrences"))


def test_manual_creation_marker_does_not_appear_on_ordinary_due_or_unknown_admission(service_factory, monkeypatch):
    service = service_factory()
    definition = create(service, destination(service))
    occurrence = schedule_targets.materialize_webhook(
        service.store, definition, scheduled_for=definition["next_run_at"],
        detected_at=now().isoformat(), next_run_at=definition["next_run_at"],
    )
    assert "occurrence_created" not in occurrence
    replay = schedule_targets.materialize_webhook(
        service.store, definition, scheduled_for=definition["next_run_at"],
        detected_at=now().isoformat(), next_run_at=definition["next_run_at"],
    )
    assert "occurrence_created" not in replay
    monkeypatch.setattr(schedule_targets, "materialize_webhook", lambda *_args, **_kwargs: None)
    assert service.run_recurring_schedule_now(
        definition["definition_id"], tenant_id=TENANT, owner_id=OWNER, idempotency_token="not-admitted-key",
    ) is None


@pytest.mark.parametrize("created", [None, False, True])
def test_delegated_manual_receipt_retains_its_owners_certainty(service_factory, monkeypatch, created):
    from workers_projects_runtime.service import DELEGATED_RECURRENCE_OWNER
    service = service_factory()
    receipt = {"status": "scheduled", "occurrence_id": "delegated-occurrence"}
    if created is not None:
        receipt["occurrence_created"] = created
    monkeypatch.setattr(service, "recurring_schedule_owner", lambda: DELEGATED_RECURRENCE_OWNER)
    monkeypatch.setattr(service, "_delegated_schedule_call",
        lambda operation, *_args, **_kwargs: receipt if operation == "run_now" else None)
    result = service.run_recurring_schedule_now(
        "delegated-schedule", tenant_id=TENANT, owner_id=OWNER, idempotency_token="delegated-request-key",
    )
    assert result is receipt
    assert ("occurrence_created" in result) is (created is not None)


def test_destination_create_receipt_recovers_generated_secret_without_reenabling(service_factory):
    service = service_factory()
    arguments = {"tenant_id": TENANT, "owner_id": OWNER, "name": "Receiver", "url": "https://example.invalid/private",
                 "idempotency_key": "destination-create-1"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda _: service.create_webhook_destination(**arguments), range(4)))
    assert len({item["destination_id"] for item in created}) == 1
    assert len({item["secret_once"] for item in created}) == 1
    selected = created[0]
    service.update_webhook_destination(selected["destination_id"], tenant_id=TENANT, owner_id=OWNER, enabled=False)
    recovered = service.create_webhook_destination(**arguments)
    assert recovered["enabled"] is False and recovered["secret_once"] == selected["secret_once"]
    with pytest.raises(ControlPlaneConflict):
        service.create_webhook_destination(**{**arguments, "url": "https://other.invalid/"})
    assert len(service.list_webhook_destinations(tenant_id=TENANT, owner_id=OWNER)) == 1


def test_cancelling_an_inflight_unacknowledged_send_keeps_uncertainty_and_stops_retry(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback["callback_id"])
    # The sender has passed its fence and sent; the owner then pauses while the
    # receiver's ACK is lost. Cancelling future delivery cannot retract that send.
    service.deactivate_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER)
    assert schedule_targets.finish_callback(service.store, claimed, state="unacknowledged", error="attempt_timeout", retryable=True)
    saved = rows(service, "callback_outbox")[0]
    occurrence = rows(service, "recurring_schedule_occurrences")[0]
    assert saved["status"] == "unacknowledged" and saved["next_attempt_at"] is None
    assert occurrence["state"] == "unacknowledged"


def test_cancelling_an_inflight_accepted_send_cannot_undo_known_ack(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback["callback_id"])
    service.deactivate_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER)
    assert schedule_targets.finish_callback(service.store, claimed, state="accepted", http_status=202)
    assert rows(service, "callback_outbox")[0]["status"] == "http_accepted"
    assert rows(service, "recurring_schedule_occurrences")[0]["outcome"] == "accepted"


def test_invalid_overdue_definition_does_not_spin_all_scheduler_phases(service_factory):
    service = service_factory()
    definition = create(service, destination(service), due=now()-timedelta(seconds=1))
    with service.store._connect() as conn:
        conn.execute("UPDATE recurring_schedule_definitions SET recurrence_type='invalid' WHERE definition_id=?", (definition['definition_id'],))
    service._scheduler_wake_event.clear()
    service._materialize_due_recurring_schedules(now())
    assert service._next_scheduler_wait_s(5) == 5
    assert not service._scheduler_wake_event.is_set()


def test_progressing_due_backlog_wakes_next_bounded_batch(service_factory):
    service = service_factory()
    dest = destination(service)
    instant = now()
    for index in range(51):
        create(service, dest, key=f'backlog-{index:08}', due=instant-timedelta(seconds=1))
    service._scheduler_wake_event.clear()
    assert len(service._materialize_due_recurring_schedules(instant)) == 50
    assert service._scheduler_wake_event.is_set()
    assert len(service._materialize_due_recurring_schedules(instant)) == 1
    assert len(rows(service, 'recurring_schedule_occurrences')) == 51


def test_explicit_targets_are_not_hidden_behind_legacy_external_owner_definitions(service_factory, monkeypatch):
    service = service_factory()
    selected = worker(service)
    instant = now()
    for index in range(51):
        service.create_recurring_schedule(selected["worker_id"], f"Legacy schedule {index}", recurrence_type="once",
            starts_at=(instant-timedelta(seconds=10)).isoformat())
    explicit = create(service, destination(service), due=instant-timedelta(seconds=1))
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    service._materialize_due_recurring_schedules(instant)
    assert any(item["definition_id"] == explicit["definition_id"] for item in rows(service, "recurring_schedule_occurrences"))
    assert service.get_recurring_schedule(explicit["definition_id"], tenant_id=TENANT, owner_id=OWNER)["target"]["kind"] == "webhook"


@pytest.mark.parametrize("prior_send", [False, True])
def test_local_destination_fence_does_not_add_a_send_or_erase_uncertainty(service_factory, prior_send):
    service = service_factory()
    dest = destination(service)
    definition = create(service, dest)
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback['callback_id'])
    if prior_send:
        schedule_targets.finish_callback(service.store, claimed, state='unacknowledged', error='attempt_timeout', retryable=True)
        with service.store._connect() as conn:
            conn.execute("UPDATE callback_outbox SET next_attempt_at=? WHERE callback_id=?", ((now()-timedelta(seconds=1)).isoformat(),callback['callback_id']))
        claimed = service.store.claim_pending_callback(callback['callback_id'])
    service.update_webhook_destination(dest['destination_id'], tenant_id=TENANT, owner_id=OWNER, enabled=False)
    schedule_targets.deliver(service, claimed)
    saved = rows(service, 'callback_outbox')[0]
    occurrence = rows(service, 'recurring_schedule_occurrences')[0]
    assert saved['attempts'] == int(prior_send)
    assert occurrence['attempt_count'] == int(prior_send)
    assert saved['status'] == ('unacknowledged' if prior_send else 'rejected')


def test_later_edits_never_rewrite_terminal_delivery_history(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback['callback_id'])
    schedule_targets.finish_callback(service.store, claimed, state='unacknowledged', error='attempt_timeout')
    before = rows(service, 'recurring_schedule_occurrences')[0]
    service.update_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER, updates={'instruction': 'A new instruction'})
    service.deactivate_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)
    assert rows(service, 'recurring_schedule_occurrences')[0] == before


def test_manual_success_then_pause_does_not_finish_the_future_clock(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    claimed = service.store.claim_pending_callback(callback['callback_id'])
    assert schedule_targets.finish_callback(service.store, claimed, state='accepted')
    service.deactivate_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)
    paused = service.get_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)
    assert paused['last_outcome'] == 'accepted' and paused['last_occurrence_at']
    assert paused['clock_finished'] is False
    assert service.list_recurring_schedules(tenant_id=TENANT, owner_id=OWNER, include_inactive=True)[0]['clock_finished'] is False
    service.update_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER, updates={'enabled': True})
    resumed = service.get_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)
    assert resumed['active'] and resumed['clock_finished'] is False


def test_actual_due_one_off_finishes_the_clock_without_a_manual_result(service_factory):
    service = service_factory()
    instant = now()
    definition = create(service, destination(service), due=instant+timedelta(seconds=1))
    service._materialize_due_recurring_schedules(instant+timedelta(seconds=2))
    fired = service.get_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)
    assert fired['active'] is False and fired['clock_finished'] is True
    assert service.list_recurring_schedules(tenant_id=TENANT, owner_id=OWNER, include_inactive=True)[0]['clock_finished'] is True


def test_destination_body_limit_rejects_at_create_and_edit_before_clock(service_factory):
    service = service_factory()
    dest = service.create_webhook_destination(tenant_id=TENANT, owner_id=OWNER, name="Small receiver",
        url="https://receiver.example.test/hook", secret=SECRET, max_body_bytes=1024)
    assert dest["max_body_bytes"] == 1024
    with pytest.raises(ValueError, match="schedule_payload_too_large"):
        create(service, dest, instruction="large" * 300)
    assert not rows(service, "recurring_schedule_definitions")
    accepted = create(service, dest, instruction="Small task")
    with pytest.raises(ValueError, match="schedule_payload_too_large"):
        service.update_recurring_schedule(accepted["definition_id"], tenant_id=TENANT, owner_id=OWNER,
            updates={"instruction": "x" * 1024})
    assert service.get_recurring_schedule(accepted["definition_id"], tenant_id=TENANT, owner_id=OWNER)["instruction"] == "Small task"
    event = pending_callback(service, accepted)
    assert len(event["payload_json"].encode()) <= 1024
    assert json.loads(event["payload_json"])["instruction"] == "Small task"


def test_body_limit_counts_utf8_and_raw_payload_mode_exactly(service_factory):
    service = service_factory()
    dest = service.create_webhook_destination(tenant_id=TENANT, owner_id=OWNER, name="UTF8 receiver",
        url="https://receiver.example.test/hook", secret=SECRET, max_body_bytes=1024)
    with pytest.raises(ValueError, match="schedule_payload_too_large"):
        create(service, dest, instruction="😀" * 240)
    raw = service.create_webhook_destination(tenant_id=TENANT, owner_id=OWNER, name="Raw receiver",
        url="https://receiver.example.test/raw", secret=SECRET, max_body_bytes=1024, payload_mode="raw")
    assert create(service, raw, key="raw-large-instruction", instruction="x" * 2000)["instruction"] == "x" * 2000
    with pytest.raises(ValueError, match="schedule_payload_too_large"):
        create(service, raw, key="raw-large-data", request_data={"value": "x" * 1024})


def test_current_destination_conflict_is_safe_after_secret_rotation(service_factory):
    service = service_factory()
    data = dict(tenant_id=TENANT, owner_id=OWNER, name="Rotation receiver", url="https://receiver.example.test/hook", secret=SECRET,
        idempotency_key="rotation-destination")
    first = service.create_webhook_destination(**data)
    assert service.create_webhook_destination(**data)["destination_id"] == first["destination_id"]
    secret_b = "whsec_" + base64.b64encode(b"different-synthetic-secret-key-123").decode()
    service.update_webhook_destination(first["destination_id"], tenant_id=TENANT, owner_id=OWNER, secret=secret_b)
    with pytest.raises(schedule_targets.DestinationCreateConflict) as exc:
        service.create_webhook_destination(**data)
    assert exc.value.destination_id == first["destination_id"] and exc.value.changed_fields == ["secret"]
    assert SECRET not in str(exc.value) and secret_b not in str(exc.value)
    service.update_webhook_destination(first["destination_id"], tenant_id=TENANT, owner_id=OWNER, secret=SECRET)
    assert service.create_webhook_destination(**data)["destination_id"] == first["destination_id"]


def test_schedule_retarget_expected_version_is_compare_and_swap(service_factory):
    service = service_factory()
    first, second = destination(service), destination(service)
    definition = create(service, first)
    target = {**definition["target"], "destination_id": second["destination_id"]}
    updated = service.update_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER,
        updates={"target": target, "expected_version": definition["version"]})
    assert updated["version"] == definition["version"] + 1
    with pytest.raises(ControlPlaneConflict):
        service.update_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER,
            updates={"enabled": False, "expected_version": definition["version"]})
    assert service.get_recurring_schedule(definition["definition_id"], tenant_id=TENANT, owner_id=OWNER)["version"] == updated["version"]


def test_owner_destination_filter_drains_bounded_cas_retarget_batches(service_factory):
    service = service_factory()
    first, second = destination(service), destination(service)
    for index in range(3):
        create(service, first, key=f"destination-batch-{index}")
    create(service, second, key="destination-other")
    migrated = []
    while True:
        batch = service.list_recurring_schedules(tenant_id=TENANT, owner_id=OWNER, destination_id=first["destination_id"], include_inactive=True, limit=1)
        if not batch:
            break
        assert len(batch) == 1
        item = batch[0]
        service.update_recurring_schedule(item["definition_id"], tenant_id=TENANT, owner_id=OWNER,
            updates={"target": {**item["target"], "destination_id": second["destination_id"]}, "expected_version": item["version"]})
        migrated.append(item["definition_id"])
    assert len(set(migrated)) == 3
    assert len(service.list_recurring_schedules(tenant_id=TENANT, owner_id=OWNER, destination_id=second["destination_id"], include_inactive=True)) == 4
    assert service.list_recurring_schedules(tenant_id=TENANT, owner_id="other-owner", destination_id=second["destination_id"]) == []


def _claim_retry_now(service, callback_id):
    with service.store._connect() as conn:
        conn.execute('UPDATE callback_outbox SET next_attempt_at=? WHERE callback_id=?', ((now()-timedelta(seconds=1)).isoformat(), callback_id))
    claimed = service.store.claim_pending_callback(callback_id)
    assert claimed
    return claimed


def _reclaim_expired_sender(service, callback_id):
    with service.store._connect() as conn:
        conn.execute('UPDATE callback_outbox SET delivery_lease_expires_at=? WHERE callback_id=?', ((now()-timedelta(seconds=1)).isoformat(), callback_id))
    assert service.store.reclaim_stale_delivering_callbacks(stale_before=(now()-timedelta(seconds=300)).isoformat()) == 1
    return rows(service, 'recurring_schedule_occurrences')[0]


def test_timeout_then_retry_503_then_401_preserves_original_unknown_send(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    first = service.store.claim_pending_callback(callback['callback_id'])
    assert schedule_targets.finish_callback(service.store, first, state='unacknowledged', error='attempt_timeout', retryable=True)
    middle = rows(service, 'recurring_schedule_occurrences')[0]
    assert middle['state'] == 'pending' and middle['outcome'] == 'unacknowledged'
    for public in (
        service.get_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER),
        service.list_recurring_schedules(tenant_id=TENANT, owner_id=OWNER, include_inactive=True)[0],
    ):
        assert public['last_outcome'] == 'pending'
        assert public['last_occurrence_id'] == middle['occurrence_id']
        assert public['last_occurrence_expires_at'] == callback['expires_at']
    second = _claim_retry_now(service, callback['callback_id'])
    assert schedule_targets.finish_callback(service.store, second, state='rejected', error='http_rejected', http_status=503, retryable=True)
    middle = rows(service, 'recurring_schedule_occurrences')[0]
    assert middle['state'] == 'pending' and middle['outcome'] == 'unacknowledged'
    third = _claim_retry_now(service, callback['callback_id'])
    assert schedule_targets.finish_callback(service.store, third, state='rejected', error='http_rejected', http_status=401)
    saved = rows(service, 'callback_outbox')[0]
    occurrence = rows(service, 'recurring_schedule_occurrences')[0]
    assert saved['status'] == occurrence['state'] == 'unacknowledged'
    assert saved['attempts'] == occurrence['attempt_count'] == 3
    assert occurrence['http_status'] == 401 and saved['last_error'] == 'http_rejected'
    assert service.get_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)['last_outcome'] == 'unacknowledged'


def test_crashed_sender_then_401_preserves_unknown_without_fabricated_attempt(service_factory):
    service = service_factory()
    callback = pending_callback(service, create(service, destination(service)))
    first = service.store.claim_pending_callback(callback['callback_id'])
    recovered = _reclaim_expired_sender(service, callback['callback_id'])
    assert recovered['outcome'] == 'unacknowledged' and recovered['attempt_count'] == 0
    second = _claim_retry_now(service, callback['callback_id'])
    assert second['delivery_generation'] > first['delivery_generation']
    assert schedule_targets.finish_callback(service.store, second, state='rejected', error='http_rejected', http_status=401)
    saved = rows(service, 'callback_outbox')[0]
    occurrence = rows(service, 'recurring_schedule_occurrences')[0]
    assert saved['status'] == occurrence['state'] == 'unacknowledged'
    assert saved['attempts'] == occurrence['attempt_count'] == 1
    assert not schedule_targets.finish_callback(service.store, first, state='accepted', http_status=202)


@pytest.mark.parametrize('unknown_source', ['timeout', 'crash'])
def test_positive_retry_ack_settles_prior_uncertainty(unknown_source, service_factory):
    service = service_factory()
    callback = pending_callback(service, create(service, destination(service)))
    first = service.store.claim_pending_callback(callback['callback_id'])
    if unknown_source == 'timeout':
        assert schedule_targets.finish_callback(service.store, first, state='unacknowledged', error='attempt_timeout', retryable=True)
    else:
        _reclaim_expired_sender(service, callback['callback_id'])
    second = _claim_retry_now(service, callback['callback_id'])
    assert schedule_targets.finish_callback(service.store, second, state='accepted', http_status=202)
    saved = rows(service, 'callback_outbox')[0]
    occurrence = rows(service, 'recurring_schedule_occurrences')[0]
    assert saved['status'] == 'http_accepted'
    assert occurrence['state'] == occurrence['outcome'] == 'accepted'
    assert occurrence['http_status'] == 202 and occurrence['terminal_at']
    assert saved['next_attempt_at'] is None


def test_cancel_after_crash_keeps_unknown_receipt_even_with_zero_finished_attempts(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    first = service.store.claim_pending_callback(callback['callback_id'])
    recovered = _reclaim_expired_sender(service, callback['callback_id'])
    assert recovered['attempt_count'] == 0 and recovered['outcome'] == 'unacknowledged'
    service.deactivate_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)
    saved = rows(service, 'callback_outbox')[0]
    occurrence = rows(service, 'recurring_schedule_occurrences')[0]
    assert saved['status'] == occurrence['state'] == 'unacknowledged'
    assert saved['attempts'] == occurrence['attempt_count'] == 0
    assert occurrence['terminal_at'] and saved['next_attempt_at'] is None
    assert service.get_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)['last_outcome'] == 'unacknowledged'
    assert not schedule_targets.finish_callback(service.store, first, state='accepted', http_status=202)


def test_first_http_401_is_rejected_in_owner_get_list_and_history(service_factory):
    service = service_factory()
    definition = create(service, destination(service))
    callback = pending_callback(service, definition)
    attempt = service.store.claim_pending_callback(callback['callback_id'])
    assert schedule_targets.finish_callback(service.store, attempt, state='rejected', error='http_rejected', http_status=401)
    for public in (
        service.get_recurring_schedule(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER),
        service.list_recurring_schedules(tenant_id=TENANT, owner_id=OWNER, include_inactive=True)[0],
    ):
        assert public['last_outcome'] == 'rejected'
        assert public['last_occurrence_expires_at'] == callback['expires_at']
    occurrence = service.list_recurring_schedule_occurrences(definition['definition_id'], tenant_id=TENANT, owner_id=OWNER)[0]
    assert occurrence['state'] == 'rejected' and occurrence['outcome'] == 'http_rejected'
    assert occurrence['http_status'] == 401 and occurrence['delivery_expires_at'] == callback['expires_at']
