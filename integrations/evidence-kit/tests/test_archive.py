from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from xperfect_evidence.archive import ReadOnlyAttempts, export_terminal, reconcile_pending
from xperfect_evidence.manifest import EvidenceError, PathPolicy, digest


def manifest(sample):
    return {"schema_version": 1, "attempts": [{"run_id": "run-1", "attempt_id": "attempt-1", "files": [sample["item"]]}]}


def lookup(run_id, attempt_id):
    return {"run_id": run_id, "attempt_id": attempt_id, "state": "completed", "ended_at": "synthetic-terminal-time"}


def test_exports_exact_terminal_evidence_while_unrelated_work_stays_active(sample):
    active = {"run_id": "another-run", "state": "running"}
    receipt = export_terminal(manifest(sample), source=sample["source"], destination=sample["destination"], lookup=lookup, policy=PathPolicy("NFC", False))
    assert receipt["files"] == 1 and not receipt["authoritative_state_backup"]
    assert active["state"] == "running"
    assert export_terminal(manifest(sample), source=sample["source"], destination=sample["destination"], lookup=lookup, policy=PathPolicy("NFC", False)) == receipt


def test_active_attempt_and_bad_hash_refuse_before_export(sample):
    with pytest.raises(EvidenceError):
        export_terminal(manifest(sample), source=sample["source"], destination=sample["destination"], lookup=lambda *_: {"state": "running"}, policy=PathPolicy("NFC", False))
    assert list(sample["destination"].path.iterdir()) == []
    (sample["source"].path / "original.txt").write_text("changed")
    with pytest.raises(EvidenceError):
        export_terminal(manifest(sample), source=sample["source"], destination=sample["destination"], lookup=lookup, policy=PathPolicy("NFC", False))


def test_archive_interruption_recovers_and_tampered_destination_refuses(sample):
    def crash(stage):
        if stage == "before_receipt":
            raise RuntimeError("interrupted copy")
    with pytest.raises(RuntimeError):
        export_terminal(manifest(sample), source=sample["source"], destination=sample["destination"], lookup=lookup, policy=PathPolicy("NFC", False), checkpoint=crash)
    base = "segments/" + digest({"manifest": manifest(sample), "path_policy": {"normalization": "NFC", "case_sensitive": False}})
    assert not sample["destination"].exists(base + "/RECEIPT.json")
    export_terminal(manifest(sample), source=sample["source"], destination=sample["destination"], lookup=lookup, policy=PathPolicy("NFC", False))
    (sample["destination"].path / base / "files" / "original.txt").write_text("tampered")
    with pytest.raises(EvidenceError):
        export_terminal(manifest(sample), source=sample["source"], destination=sample["destination"], lookup=lookup, policy=PathPolicy("NFC", False))


def test_readonly_collector_scopes_rows_and_never_migrates_database(tmp_path):
    database = tmp_path / "runtime.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("CREATE TABLE glasshive_schema_versions(component TEXT, version INTEGER); INSERT INTO glasshive_schema_versions VALUES('runtime_store',14);"
                                 "CREATE TABLE workers(worker_id TEXT,tenant_id TEXT,owner_id TEXT); INSERT INTO workers VALUES('worker','tenant','owner');"
                                 "CREATE TABLE runs(run_id TEXT,worker_id TEXT); INSERT INTO runs VALUES('run','worker');"
                                 "CREATE TABLE run_attempts(attempt_id TEXT,run_id TEXT,attempt_number INTEGER,state TEXT,ended_at TEXT);"
                                 "INSERT INTO run_attempts VALUES('attempt','run',1,'completed','terminal');")
    before = database.read_bytes()
    assert ReadOnlyAttempts(database, tenant_id="tenant", owner_id="owner")("run", "attempt")["state"] == "completed"
    assert len(ReadOnlyAttempts(database, tenant_id="tenant", owner_id="owner").attempts("run")) == 1
    assert ReadOnlyAttempts(database, tenant_id="tenant", owner_id="other")("run", "attempt") is None
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE glasshive_schema_versions SET version=999")
    with pytest.raises(EvidenceError):
        ReadOnlyAttempts(database, tenant_id="tenant", owner_id="owner")("run", "attempt")


def test_exact_g8_hold_reconciliation_is_inert_and_detects_missing_ids():
    expected = ["request:first", "coordinator:second"]
    hold = hashlib.sha256(json.dumps(sorted(expected), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    rows = [{"identity": i, "state": "queued", "restore_hold": 1, "restore_hold_set_hash": hold} for i in expected]
    result = reconcile_pending(expected, rows, hold_set_sha256=hold)
    assert result["state"] == "reconciled" and result["resume_authorized"] is False
    assert reconcile_pending(expected, rows[:1], hold_set_sha256=hold)["state"] == "blocked"
    with pytest.raises(EvidenceError):
        reconcile_pending(expected[:1], rows, hold_set_sha256=hold)


@pytest.mark.parametrize("state", ["accepted", "blocked", "restore_held"])
def test_reconciles_real_coordinator_pending_states_without_authorizing_resume(state):
    expected = ["goal:review-alpha", "turn:source-turn"]
    hold = hashlib.sha256(json.dumps(expected, separators=(",", ":")).encode()).hexdigest()
    rows = [{"identity": i, "state": state, "restore_hold": 1, "restore_hold_set_hash": hold}
            for i in expected]
    result = reconcile_pending(expected, rows, hold_set_sha256=hold)
    assert result["state"] == "reconciled"
    assert result["resume_authorized"] is False
    assert {item["status"] for item in result["items"]} == {"pending_held"}
    rows[0]["restore_hold"] = 0
    assert reconcile_pending(expected, rows, hold_set_sha256=hold)["state"] == "blocked"
