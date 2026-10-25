"""Optional lifetime spend admission and explicit owner/project operator holds.

This limits new control-plane invocations. It cannot cap a provider invoice or
stop calls made inside an already running native harness.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from .usage_accounting import decimal_amount, add_money


class AdmissionPolicyHold(RuntimeError):
    code = "admission_policy_hold"

    def __init__(self, hold: dict):
        super().__init__("This work is saved. Review the spending or operator pause, then resume.")
        self.hold = hold


class SpendPolicyRevisionConflict(ValueError):
    def __init__(self, current_revision: int):
        super().__init__("Spending policy changed; reload before saving")
        self.current_revision = current_revision


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict) or set(policy) != {"version", "enabled", "currency", "limit_amount", "unknown_policy", "reserve_amount"}:
        raise ValueError("Spending policy fields are invalid")
    if type(policy["version"]) is not int or policy["version"] != 1 or type(policy["enabled"]) is not bool:
        raise ValueError("Spending policy version or enabled value is invalid")
    currency = policy["currency"]
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency != currency.upper():
        raise ValueError("Spending currency is invalid")
    if not isinstance(policy["unknown_policy"], str) or policy["unknown_policy"] not in {"pause", "reserve"}:
        raise ValueError("Select an explicit unknown-usage policy")
    return {**policy, "limit_amount": decimal_amount(policy["limit_amount"]),
            "reserve_amount": decimal_amount(policy["reserve_amount"])}


def ensure_schema(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "admission_hold_started_at" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN admission_hold_started_at TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS spend_policies (
        tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,scope_type TEXT NOT NULL,
        scope_id TEXT NOT NULL,revision INTEGER NOT NULL,policy_json TEXT NOT NULL,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        PRIMARY KEY(tenant_id,owner_id,scope_type,scope_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS admission_holds (
        hold_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,
        scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,kind TEXT NOT NULL,
        generation INTEGER NOT NULL,reason_code TEXT NOT NULL,active INTEGER NOT NULL,
        created_at TEXT NOT NULL,resumed_at TEXT,
        UNIQUE(tenant_id,owner_id,scope_type,scope_id,kind))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS spend_reservations (
        attempt_id TEXT NOT NULL REFERENCES run_attempts(attempt_id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,scope_type TEXT NOT NULL,
        scope_id TEXT NOT NULL,currency TEXT NOT NULL,amount TEXT NOT NULL,state TEXT NOT NULL,
        PRIMARY KEY(attempt_id,scope_type,scope_id))""")


def require_scope(conn, tenant_id, owner_id, scope_type, scope_id):
    if scope_type not in {"owner", "project"} or not tenant_id or not owner_id:
        raise ValueError("Admission scope is invalid")
    if scope_type == "owner":
        if scope_id:
            raise ValueError("Owner scope must not specify a project")
    elif not conn.execute("SELECT 1 FROM projects WHERE project_id=? AND tenant_id=? AND owner_id=?",
                          (scope_id, tenant_id, owner_id)).fetchone():
        raise ValueError("Project does not belong to this owner")


def hold_row(row):
    result = dict(row)
    result["active"] = bool(result["active"])
    return result


def place_hold(conn, *, tenant_id, owner_id, scope_type, scope_id, kind, reason_code, now):
    if reason_code not in {"spend": {"spend_limit", "usage_unknown"}, "operator": {"operator_pause", "transport_integrity"}}.get(kind, set()):
        raise ValueError("Admission hold reason is invalid")
    require_scope(conn, tenant_id, owner_id, scope_type, scope_id)
    conn.execute("""INSERT INTO admission_holds
        (hold_id,tenant_id,owner_id,scope_type,scope_id,kind,generation,reason_code,active,created_at)
        VALUES (?,?,?,?,?,?,1,?,1,?) ON CONFLICT(tenant_id,owner_id,scope_type,scope_id,kind)
        DO UPDATE SET generation=admission_holds.generation+CASE WHEN admission_holds.active=0 OR admission_holds.reason_code!=excluded.reason_code THEN 1 ELSE 0 END,
        active=1,reason_code=excluded.reason_code,resumed_at=NULL""",
        ("hold_"+uuid.uuid4().hex, tenant_id, owner_id, scope_type, scope_id, kind, reason_code, now))
    conn.execute("""UPDATE runs SET admission_hold_started_at=COALESCE(admission_hold_started_at,?),
        queue_blocker_class='admission_policy_hold' WHERE tenant_id=? AND queue_wait_open=1
        AND runtime_invoked_at IS NULL AND worker_id IN (SELECT worker_id FROM workers WHERE tenant_id=? AND owner_id=?)
        AND (?='owner' OR project_id=?)""", (now, tenant_id, tenant_id, owner_id, scope_type, scope_id))
    return hold_row(conn.execute("SELECT * FROM admission_holds WHERE tenant_id=? AND owner_id=? AND scope_type=? AND scope_id=? AND kind=?",
                                (tenant_id, owner_id, scope_type, scope_id, kind)).fetchone())


def run_scope(conn, run):
    worker = conn.execute("SELECT owner_id FROM workers WHERE worker_id=? AND tenant_id=?", (run["worker_id"], run["tenant_id"])).fetchone()
    if worker is None:
        raise ValueError("Run owner is unavailable")
    return str(run["tenant_id"]), str(worker["owner_id"]), str(run["project_id"])


def blocker(conn, run, *, now: str, reserve: bool = False) -> dict | None:
    tenant, owner, project = run_scope(conn, run)
    held = conn.execute("""SELECT * FROM admission_holds WHERE tenant_id=? AND owner_id=? AND active=1
        AND (scope_type='owner' OR (scope_type='project' AND scope_id=?)) ORDER BY kind,hold_id LIMIT 1""", (tenant, owner, project)).fetchone()
    if held:
        conn.execute("UPDATE runs SET admission_hold_started_at=COALESCE(admission_hold_started_at,?),queue_blocker_class='admission_policy_hold' WHERE run_id=? AND queue_wait_open=1 AND runtime_invoked_at IS NULL", (now, run["run_id"]))
        return hold_row(held)
    policies = conn.execute("""SELECT * FROM spend_policies WHERE tenant_id=? AND owner_id=?
        AND (scope_type='owner' OR (scope_type='project' AND scope_id=?))""", (tenant, owner, project)).fetchall()
    for row in policies:
        policy = json.loads(row["policy_json"])
        if not policy["enabled"]:
            continue
        scope_type, scope_id = row["scope_type"], row["scope_id"]
        conditions = " AND r.project_id=?" if scope_type == "project" else ""
        params = [tenant, owner, row["created_at"]] + ([scope_id] if conditions else [])
        attempts = conn.execute("""SELECT a.attempt_id,a.state,a.runtime_invoked_at,a.ended_at FROM run_attempts a JOIN runs r ON r.run_id=a.run_id
            JOIN workers w ON w.worker_id=r.worker_id WHERE r.tenant_id=? AND w.owner_id=?
            AND a.runtime_invoked_at>=?""" + conditions, params).fetchall()
        spent = Decimal(0)
        unknown = False
        for attempt in attempts:
            receipts = conn.execute("SELECT usage_json FROM usage_receipts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchall()
            normalized = [json.loads(receipt[0]) for receipt in receipts]
            charges = [charge for receipt in normalized for charge in receipt["money"]
                       if charge["currency"] == policy["currency"]]
            if charges:
                spent = add_money((spent, add_money(charge["amount"] for charge in charges)))
            complete_money = bool(normalized) and all(any(charge["currency"] == policy["currency"] for charge in receipt["money"]) for receipt in normalized)
            if complete_money and attempt["ended_at"] is not None:
                conn.execute("UPDATE spend_reservations SET state='settled' WHERE attempt_id=? AND scope_type=? AND scope_id=?",
                             (attempt["attempt_id"], scope_type, scope_id))
            else:
                unknown = True
        reservations = conn.execute("SELECT * FROM spend_reservations WHERE tenant_id=? AND owner_id=? AND scope_type=? AND scope_id=? AND state IN ('open','unknown')",
                                    (tenant, owner, scope_type, scope_id)).fetchall()
        spent = add_money((spent, add_money(item["amount"] for item in reservations)))
        attempt_id = str(run["active_attempt_id"] or "")
        has_reservation = any(item["attempt_id"] == attempt_id for item in reservations)
        proposed = Decimal(0) if has_reservation else Decimal(policy["reserve_amount"])
        reason = "usage_unknown" if unknown and policy["unknown_policy"] == "pause" else "spend_limit" if add_money((spent, proposed)) > Decimal(policy["limit_amount"]) else ""
        if reason:
            return place_hold(conn, tenant_id=tenant, owner_id=owner, scope_type=scope_type,
                              scope_id=scope_id, kind="spend", reason_code=reason, now=now)
        if reserve and attempt_id:
            conn.execute("""INSERT INTO spend_reservations
                (attempt_id,tenant_id,owner_id,scope_type,scope_id,currency,amount,state) VALUES (?,?,?,?,?,?,?,'open')
                ON CONFLICT(attempt_id,scope_type,scope_id) DO NOTHING""",
                (attempt_id, tenant, owner, scope_type, scope_id, policy["currency"], policy["reserve_amount"]))
    return None


def settle_reservations(conn, attempt_id):
    # Unknown terminal money retains its configured reserve; never call it free.
    conn.execute("UPDATE spend_reservations SET state='unknown' WHERE attempt_id=? AND state='open'", (attempt_id,))


def resume_queue_clock(conn, tenant_id: str, owner_id: str, now: str) -> None:
    """Resume the same admission episode; absolute provider deadlines stay separate."""
    rows = conn.execute("""SELECT r.* FROM runs r JOIN workers w ON w.worker_id=r.worker_id
        WHERE r.tenant_id=? AND w.owner_id=? AND r.admission_hold_started_at IS NOT NULL
        AND r.runtime_invoked_at IS NULL AND r.queue_wait_open=1""", (tenant_id, owner_id)).fetchall()
    for run in rows:
        held = conn.execute("""SELECT 1 FROM admission_holds WHERE tenant_id=? AND owner_id=? AND active=1
            AND (scope_type='owner' OR (scope_type='project' AND scope_id=?)) LIMIT 1""",
            (tenant_id, owner_id, run["project_id"])).fetchone()
        if held:
            continue
        duration = max(timedelta(0), datetime.fromisoformat(now) - datetime.fromisoformat(run["admission_hold_started_at"]))
        deadline = (datetime.fromisoformat(run["queue_deadline_at"]) + duration).isoformat()
        status_at = (datetime.fromisoformat(run["queue_next_status_at"]) + duration).isoformat() if run["queue_next_status_at"] else None
        conn.execute("UPDATE runs SET admission_hold_started_at=NULL,queue_blocker_class='admission_pending',queue_deadline_at=?,queue_next_status_at=? WHERE run_id=?",
                     (deadline, status_at, run["run_id"]))
