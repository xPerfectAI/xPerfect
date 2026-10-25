"""Terminal evidence export. G8 remains the authoritative state recovery owner."""

from __future__ import annotations

import sqlite3
import urllib.parse
from contextlib import closing
from pathlib import Path

from .evaluation import TERMINAL
from .files import Root, disjoint
from .manifest import EvidenceError, PathPolicy, digest, fields, file_plan, hash_value, text


class ReadOnlyAttempts:
    """Explicit local operator access; never initializes or migrates a Store."""

    def __init__(self, database, *, tenant_id, owner_id):
        path = Path(database).absolute()
        if path != path.resolve(strict=True) or not path.is_file():
            raise EvidenceError("Database path is not a real existing file")
        self.uri = "file:" + urllib.parse.quote(str(path), safe="/") + "?mode=ro"
        self.tenant, self.owner = text(tenant_id), text(owner_id)

    def _read(self, run_id, attempt_id=None):
        try:
            with closing(sqlite3.connect(self.uri, uri=True, timeout=5)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                row = connection.execute("SELECT version FROM glasshive_schema_versions WHERE component='runtime_store'").fetchone()
                if row is None or row[0] not in {13, 14}:
                    raise EvidenceError("Unsupported evidence-collector schema; use an explicit operator bundle")
                rows = connection.execute(
                    "SELECT a.attempt_id,a.run_id,a.attempt_number,a.state,a.ended_at,r.worker_id "
                    "FROM run_attempts a JOIN runs r ON r.run_id=a.run_id JOIN workers w ON w.worker_id=r.worker_id "
                    "WHERE a.run_id=? AND w.tenant_id=? AND w.owner_id=? "
                    + ("AND a.attempt_id=? " if attempt_id is not None else "")
                    + "ORDER BY a.attempt_number LIMIT 1001",
                    (run_id, self.tenant, self.owner, attempt_id) if attempt_id is not None else (run_id, self.tenant, self.owner),
                ).fetchall()
                if len(rows) > 1000:
                    raise EvidenceError("Attempt history exceeds collector bounds")
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise EvidenceError("Read-only attempt evidence is unavailable or incompatible") from exc

    def __call__(self, run_id, attempt_id):
        rows = self._read(run_id, attempt_id)
        return rows[0] if rows else None

    def attempts(self, run_id):
        return self._read(run_id)


def _terminal(row, run_id, attempt_id):
    if (not row or row.get("run_id") != run_id or row.get("attempt_id") != attempt_id
            or row.get("state") not in TERMINAL or not row.get("ended_at")):
        raise EvidenceError("Exact attempt is not terminal and settled")


def export_terminal(manifest, *, source: Root, destination: Root, lookup, policy: PathPolicy, checkpoint=lambda _stage: None):
    fields(manifest, {"schema_version", "attempts"})
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or not isinstance(manifest["attempts"], list) or not 1 <= len(manifest["attempts"]) <= source.limits.files):
        raise EvidenceError("Invalid terminal evidence manifest")
    disjoint(source.path, destination.path)
    all_files, identities = [], set()
    for attempt in manifest["attempts"]:
        fields(attempt, {"run_id", "attempt_id", "files"})
        key = text(attempt["run_id"]), text(attempt["attempt_id"])
        if key in identities:
            raise EvidenceError("Repeated attempt membership")
        identities.add(key)
        _terminal(lookup(*key), *key)
        all_files.extend(attempt["files"])
    file_plan(all_files, policy, destination.limits)
    # Prove every source before creating a destination segment.
    for item in all_files:
        source.snapshot(item["relative_path"], expected=item)
    selected = {"manifest": manifest, "path_policy": {"normalization": policy.normalization, "case_sensitive": policy.case_sensitive}}
    segment = "segments/" + digest(selected)
    receipt = {"schema_version": 1, "manifest_sha256": digest(manifest), "attempts": sorted([list(i) for i in identities]),
               "path_policy": selected["path_policy"], "files": len(all_files), "bytes": sum(i["size_bytes"] for i in all_files), "state": "verified", "authoritative_state_backup": False}
    with destination.lock("segments/lock"):
        destination.put_json(segment + "/MANIFEST.json", manifest)
        for attempt in manifest["attempts"]:
            key = attempt["run_id"], attempt["attempt_id"]
            _terminal(lookup(*key), *key)
            for item in attempt["files"]:
                destination.copy(source, item["relative_path"], segment + "/files/" + item["package_path"], item)
                checkpoint("file_copied")
            _terminal(lookup(*key), *key)
        for item in all_files:
            destination.snapshot(segment + "/files/" + item["package_path"], expected=item)
        if destination.inventory(segment + "/files") != {i["package_path"] for i in all_files}:
            raise EvidenceError("Archive segment membership differs")
        checkpoint("before_receipt")
        destination.put_json(segment + "/RECEIPT.json", receipt)
    return receipt


def reconcile_pending(expected_ids, restored_rows, *, hold_set_sha256):
    """Return an inert reconciliation report. This function cannot resume work."""
    hash_value(hold_set_sha256)
    if not isinstance(expected_ids, list) or len(set(expected_ids)) != len(expected_ids):
        raise EvidenceError("Expected pending identities must be exact and unique")
    for identity in expected_ids:
        text(identity)
    # G8's exact-set hash uses sorted compact JSON without the receipt newline.
    import hashlib
    import json
    expected_hash = hashlib.sha256(json.dumps(sorted(expected_ids), sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode()).hexdigest()
    if expected_hash != hold_set_sha256:
        raise EvidenceError("Pending identities differ from the existing G8 hold set")
    rows = {}
    for row in restored_rows:
        fields(row, {"identity", "state", "restore_hold", "restore_hold_set_hash"})
        if row["identity"] in rows:
            raise EvidenceError("Duplicate restored identity")
        rows[row["identity"]] = row
    result = []
    for identity in expected_ids:
        row = rows.get(identity)
        pending = {"queued", "running", "paused", "needs_input", "claimed", "admitted", "settling"}
        status = "missing" if row is None else "conflict" if row["restore_hold"] != 1 or row["restore_hold_set_hash"] != hold_set_sha256 or row["state"] not in TERMINAL | pending else "terminal_held" if row["state"] in TERMINAL else "pending_held"
        result.append({"identity": identity, "status": status})
    unexpected = sorted(set(rows) - set(expected_ids))
    return {"hold_set_sha256": hold_set_sha256, "items": result, "unexpected": unexpected,
            "resume_authorized": False, "state": "reconciled" if not unexpected and all(r["status"] in {"pending_held", "terminal_held"} for r in result) else "blocked"}
