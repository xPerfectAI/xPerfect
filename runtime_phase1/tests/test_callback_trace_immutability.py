from __future__ import annotations

import json
import sqlite3

import pytest

import workers_projects_runtime.store as store_module
from workers_projects_runtime.store import Store


def _callback_fixture(tmp_path, *, suffix: str = "ledger") -> tuple[Store, dict, dict]:
    store = Store(str(tmp_path / f"callback-{suffix}.sqlite3"))
    record = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key=f"idempotency-{suffix}",
        request_digest=f"digest-{suffix}",
        origin_ref=f"origin_{suffix}_0001",
        title="Callback trace fixture",
        goal="Prove callback chronology",
        instruction="Record one synthetic callback.",
        origin_surface="telegram",
        worker_name="Trace worker",
        worker_role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test-model",
        execution_mode="docker",
        bootstrap_bundle={},
    )
    worker = store.get_worker(str(record["worker_id"]))
    assert worker is not None
    callback_id = f"cb_{suffix}"
    callback = store.insert_callback_outbox_once(
        callback_id=callback_id,
        project_id=str(record["project_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(record["current_run_id"]),
        attempt_number=None,
        event_type="run.queued",
        url="https://callback.example.invalid/events",
        payload_json=json.dumps(
            {
                "callback_id": callback_id,
                "callback_ts": 1_700_000_001,
                "event": "run.queued",
                "origin_ref": record["origin_ref"],
                "work_ref": record["work_ref"],
                "worker_id": worker["worker_id"],
                "run_id": record["current_run_id"],
            }
        ),
    )
    return store, record, callback


def _ledger(store: Store, callback_id: str) -> list[sqlite3.Row]:
    with store._connect() as conn:
        return conn.execute(
            """
            SELECT * FROM callback_trace_events
            WHERE callback_id = ? ORDER BY callback_sequence ASC
            """,
            (callback_id,),
        ).fetchall()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("status", "delivering"),
        ("attempts", 2),
        ("updated_at", "2026-08-23T01:02:03+00:00"),
        ("delivered_at", "2026-08-23T01:02:04+00:00"),
        ("http_accepted_at", "2026-08-23T01:02:05+00:00"),
        ("payload_json", '{"callback_ts":1700000001,"revision":2}'),
        ("last_error", "synthetic retry"),
        ("delivery_lease_token", "cbdel_synthetic"),
        ("delivery_generation", 2),
        ("delivery_lease_expires_at", "2026-08-23T01:07:03+00:00"),
    ],
)
def test_each_mutable_callback_field_appends_one_linked_event(
    tmp_path, column: str, value: object
):
    store, _record, callback = _callback_fixture(tmp_path, suffix=column)
    callback_id = str(callback["callback_id"])
    before = _ledger(store, callback_id)

    with store._connect() as conn:
        conn.execute(
            f"UPDATE callback_outbox SET {column} = ? WHERE callback_id = ?",
            (value, callback_id),
        )

    after = _ledger(store, callback_id)
    assert len(before) == 1
    assert len(after) == 2
    assert after[1]["previous_event_sha256"] == after[0]["event_sha256"]
    assert after[1]["event_sha256"] != after[0]["event_sha256"]
    assert set(json.loads(after[1]["snapshot_json"])) == {
        "attemptNumber",
        "attempts",
        "callbackId",
        "createdAt",
        "deliveredAt",
        "deliveryGeneration",
        "deliveryLeaseExpiresAt",
        "deliveryLeaseToken",
        "event",
        "httpAcceptedAt",
        "lastError",
        "payloadJson",
        "projectId",
        "resultDigest",
        "resultRevision",
        "runId",
        "status",
        "tenantId",
        "updatedAt",
        "url",
        "workerId",
    }


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("callback_id", "cb_rewritten"),
        ("project_id", "project_rewritten"),
        ("worker_id", "worker_rewritten"),
        ("tenant_id", "tenant-rewritten"),
        ("run_id", "run_rewritten"),
        ("attempt_number", 4),
        ("event_type", "run.failed"),
        ("url", "https://other.example.invalid/events"),
        ("result_revision", 2),
        ("result_digest", "sha256:" + "a" * 64),
        ("created_at", "2026-08-23T00:00:00+00:00"),
    ],
)
def test_each_callback_authority_field_is_immutable(
    tmp_path, column: str, value: object
):
    store, _record, callback = _callback_fixture(tmp_path, suffix=f"authority_{column}")
    callback_id = str(callback["callback_id"])

    with pytest.raises(sqlite3.IntegrityError, match="callback authority is immutable"):
        with store._connect() as conn:
            conn.execute(
                f"UPDATE callback_outbox SET {column} = ? WHERE callback_id = ?",
                (value, callback_id),
            )

    assert len(_ledger(store, callback_id)) == 1


def test_changed_callback_replay_fails_instead_of_reusing_one_trace_event(tmp_path):
    store, record, callback = _callback_fixture(tmp_path, suffix="changed_replay")
    callback_id = str(callback["callback_id"])

    with pytest.raises(RuntimeError, match="Callback identity collision"):
        store.insert_callback_outbox_once(
            callback_id=callback_id,
            project_id=str(record["project_id"]),
            worker_id=str(record["worker_id"]),
            run_id=str(record["current_run_id"]),
            attempt_number=None,
            event_type="run.queued",
            url="https://callback.example.invalid/events",
            payload_json=json.dumps(
                {
                    "callback_id": callback_id,
                    "callback_ts": 1_700_000_001,
                    "event": "run.queued",
                    "origin_ref": record["origin_ref"],
                    "work_ref": record["work_ref"],
                    "worker_id": record["worker_id"],
                    "run_id": record["current_run_id"],
                    "changed": True,
                }
            ),
        )

    assert len(_ledger(store, callback_id)) == 1


def test_stale_and_current_callback_leases_preserve_one_hash_chain(tmp_path):
    first, _record, callback = _callback_fixture(tmp_path, suffix="lease_race")
    second = Store(str(first.db_path))
    callback_id = str(callback["callback_id"])

    stale = first.claim_pending_callback(callback_id)
    assert stale is not None
    with first._connect() as conn:
        conn.execute(
            """
            UPDATE callback_outbox
            SET delivery_lease_expires_at = '2000-01-01T00:00:00+00:00'
            WHERE callback_id = ?
            """,
            (callback_id,),
        )
    assert second.reclaim_stale_delivering_callbacks(
        stale_before="2100-01-01T00:00:00+00:00"
    ) == 1
    current = second.claim_pending_callback(callback_id)
    assert current is not None

    before_stale_replays = len(_ledger(first, callback_id))
    assert (
        first.mark_callback_pending(
            callback_id,
            lease_token=str(stale["delivery_lease_token"]),
            delivery_generation=int(stale["delivery_generation"]),
            attempts=1,
            payload_json=str(stale["payload_json"]),
            last_error="stale",
        )
        is None
    )
    assert len(_ledger(first, callback_id)) == before_stale_replays

    settled = second.mark_callback_dead_lettered(
        callback_id,
        lease_token=str(current["delivery_lease_token"]),
        delivery_generation=int(current["delivery_generation"]),
        attempts=1,
        payload_json=str(current["payload_json"]),
        last_error="synthetic terminal result",
    )
    assert settled is not None
    ledger = _ledger(second, callback_id)
    assert [row["callback_sequence"] for row in ledger] == list(
        range(1, len(ledger) + 1)
    )
    assert all(
        ledger[index]["previous_event_sha256"] == ledger[index - 1]["event_sha256"]
        for index in range(1, len(ledger))
    )


def test_external_sql_without_store_hash_functions_fails_closed(tmp_path):
    store, _record, callback = _callback_fixture(tmp_path, suffix="external_sql")
    callback_id = str(callback["callback_id"])

    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            conn.execute(
                "UPDATE callback_outbox SET status = 'delivering' WHERE callback_id = ?",
                (callback_id,),
            )

    assert len(_ledger(store, callback_id)) == 1


def test_callback_trace_ledger_is_append_only(tmp_path):
    store, _record, callback = _callback_fixture(tmp_path, suffix="append_only")
    callback_id = str(callback["callback_id"])

    with pytest.raises(sqlite3.IntegrityError, match="callback trace is append-only"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE callback_trace_events SET status = 'changed' WHERE callback_id = ?",
                (callback_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="callback trace is append-only"):
        with store._connect() as conn:
            conn.execute(
                "DELETE FROM callback_trace_events WHERE callback_id = ?",
                (callback_id,),
            )


def test_public_callback_history_is_the_redacted_immutable_ledger(tmp_path):
    store, record, callback = _callback_fixture(tmp_path, suffix="public_history")
    callback_id = str(callback["callback_id"])
    claimed = store.claim_pending_callback(callback_id)
    assert claimed is not None
    pending = store.mark_callback_pending(
        callback_id,
        lease_token=str(claimed["delivery_lease_token"]),
        delivery_generation=int(claimed["delivery_generation"]),
        attempts=1,
        payload_json=str(claimed["payload_json"]),
        last_error="synthetic retry",
    )
    assert pending is not None

    detail = store.work_trace_detail(
        run_id=str(record["current_run_id"]),
        tenant_id="tenant-a",
        owner_id="owner-a",
    )

    assert detail is not None
    history = detail["callbackDeliveries"]
    assert [event["status"] for event in history] == [
        "pending",
        "delivering",
        "pending",
    ]
    assert [event["ledgerSequence"] for event in history] == [1, 2, 3]
    assert [event["callbackRevision"] for event in history] == [1, 2, 3]
    assert history[0]["previousEventSha256"] is None
    assert all(
        history[index]["previousEventSha256"] == history[index - 1]["eventSha256"]
        for index in range(1, len(history))
    )
    assert all(
        set(event)
        == {
            "acceptedAt",
            "attemptNumber",
            "attempts",
            "authoritySha256",
            "callbackRef",
            "callbackRevision",
            "createdAt",
            "deliveryGeneration",
            "event",
            "eventSha256",
            "ledgerSequence",
            "payloadSha256",
            "previousEventSha256",
            "resultDigest",
            "resultRevision",
            "status",
            "updatedAt",
        }
        for event in history
    )
    encoded = json.dumps(detail)
    assert callback_id not in encoded
    assert "cbdel_" not in encoded
    assert "synthetic retry" not in encoded


def test_callback_trace_reader_rejects_a_forged_sequence_gap(tmp_path):
    store, record, callback = _callback_fixture(tmp_path, suffix="sequence_gap")
    callback_id = str(callback["callback_id"])
    previous = _ledger(store, callback_id)[-1]
    callback_sequence = 99
    run_sequence = 99
    event_sha256 = store_module._callback_trace_event_sha256_values(
        callback_id,
        callback_sequence,
        run_sequence,
        previous["snapshot_json"],
        previous["event_sha256"],
    )
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO callback_trace_events (
                callback_id, run_id, callback_sequence, run_sequence,
                mutation_kind, status, snapshot_json, payload_sha256,
                authority_sha256, previous_event_sha256, event_sha256,
                created_at
            ) VALUES (?, ?, ?, ?, 'forged', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                callback_id,
                record["current_run_id"],
                callback_sequence,
                run_sequence,
                previous["status"],
                previous["snapshot_json"],
                previous["payload_sha256"],
                previous["authority_sha256"],
                previous["event_sha256"],
                event_sha256,
                previous["created_at"],
            ),
        )

    with pytest.raises(RuntimeError, match="Callback trace integrity check failed"):
        store.work_trace_detail(
            run_id=str(record["current_run_id"]),
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
