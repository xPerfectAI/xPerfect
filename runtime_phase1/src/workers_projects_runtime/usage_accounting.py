"""Content-free, exact-attempt usage observations. Token counts are not billing."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
NATIVE_TOTAL_FIELDS = ("input_tokens", "output_tokens")


def decimal_amount(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Money amount must be a finite nonnegative decimal")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Money amount must be a finite nonnegative decimal") from exc
    if not amount.is_finite() or amount < 0 or len(str(value)) > 128 or abs(amount.as_tuple().exponent) > 128 or amount.adjusted() > 128:
        raise ValueError("Money amount must be a finite nonnegative decimal")
    return format(amount, "f")


def add_money(values) -> Decimal:
    values = list(values)
    with localcontext() as context:
        context.prec = 512 + len(str(len(values)))
        return sum((Decimal(value) for value in values), Decimal(0))


class _DecimalSum:
    """SQLite aggregate over normalized decimal strings, without binary rounding."""
    def __init__(self):
        self.amount = Decimal(0)

    def step(self, value):
        if value is not None:
            self.amount = add_money((self.amount, value))

    def finalize(self):
        return format(self.amount, "f")


def register_decimal_sum(conn) -> None:
    conn.create_aggregate("usage_decimal_sum", 1, _DecimalSum)


def normalize_observation(usage: dict[str, Any] | None, *, total_cost_usd: object = None) -> dict[str, Any]:
    """Accept reported values only. Missing and invalid counts remain distinguishable."""
    raw = usage if isinstance(usage, dict) else {}
    if raw.get("source") == "estimated":
        raise ValueError("Estimated usage cannot become a usage receipt")
    tokens = {}
    statuses = raw.get("_token_statuses") if isinstance(raw.get("_token_statuses"), dict) else {}
    for key in TOKEN_FIELDS:
        value = raw.get(key)
        valid = type(value) is int and value >= 0
        tokens[key] = {"value": value if valid else None,
                       "status": "reported" if valid else "invalid" if statuses.get(key) == "invalid" else "not_reported" if value is None else "invalid"}
    money = []
    if total_cost_usd is not None:
        try:
            money = [{"currency": "USD", "amount": decimal_amount(total_cost_usd), "source": "provider_reported"}]
        except ValueError:
            pass
    native = raw.get("_native_totals") if isinstance(raw.get("_native_totals"), dict) else {}
    totals = {key: {"value": native[key] if type(native.get(key)) is int and native[key] >= 0 else None,
                    "status": "reported" if type(native.get(key)) is int and native[key] >= 0 else "not_reported"}
              for key in NATIVE_TOTAL_FIELDS}
    scope = raw.get("_measurement_scope")
    return {"version": 1, "tokens": tokens, "money": money, "native_totals": totals,
            "measurement_coverage": "partial" if raw.get("_measurement_coverage") == "partial" else "complete",
            "measurement_scope": scope if scope in ("native_latest_response", "sdk_aggregate_unproven") else "attempt_snapshot"}


def unknown_summary() -> dict[str, Any]:
    result = normalize_observation(None)
    return {**result, "coverage": "unknown", "receipt_ids": [], "attempts": 0, "unknown_attempts": 0}


def observation_input(observation: object) -> dict[str, Any]:
    """Read only the normalized private receipt shape; never return raw fields."""
    if not isinstance(observation, dict) or observation.get("version") != 1:
        return {}
    fields = observation.get("tokens")
    if not isinstance(fields, dict):
        return {}
    raw = {key: item["value"] for key, item in fields.items()
           if key in TOKEN_FIELDS and isinstance(item, dict) and item.get("status") == "reported"
           and type(item.get("value")) is int and item["value"] >= 0}
    raw["_token_statuses"] = {key: "invalid" for key, item in fields.items()
                              if key in TOKEN_FIELDS and isinstance(item, dict) and item.get("status") == "invalid"}
    revision = observation.get("revision", 0)
    raw["_receipt_revision"] = revision if type(revision) is int and revision >= 0 else 0
    totals = observation.get("native_totals", {})
    raw["_native_totals"] = {key: item["value"] for key, item in totals.items()
                            if key in NATIVE_TOTAL_FIELDS and isinstance(item, dict) and item.get("status") in {"reported", "partial"}
                            and type(item.get("value")) is int and item["value"] >= 0} if isinstance(totals, dict) else {}
    raw["_measurement_coverage"] = observation.get("measurement_coverage")
    raw["_measurement_scope"] = observation.get("measurement_scope")
    money = observation.get("money")
    for charge in money if isinstance(money, list) else []:
        if isinstance(charge, dict) and charge.get("source") == "provider_reported" and charge.get("currency") == "USD":
            try:
                raw["_reported_cost_usd"] = decimal_amount(charge.get("amount"))
            except ValueError:
                pass
    return raw


def merge_observation(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """An absent/invalid observation can never erase an already reported value."""
    merged = {**incoming, "tokens": dict(incoming["tokens"])}
    for key in TOKEN_FIELDS:
        if incoming["tokens"][key]["status"] != "reported" and previous.get("tokens", {}).get(key, {}).get("status") == "reported":
            merged["tokens"][key] = previous["tokens"][key]
        elif incoming["tokens"][key]["status"] == "not_reported" and previous.get("tokens", {}).get(key, {}).get("status") == "invalid":
            merged["tokens"][key] = previous["tokens"][key]
    if not incoming["money"]:
        merged["money"] = previous.get("money", [])
    merged["native_totals"] = dict(incoming["native_totals"])
    for key in NATIVE_TOTAL_FIELDS:
        old = previous.get("native_totals", {}).get(key, {})
        if old.get("status") in {"reported", "partial"} and merged["native_totals"][key]["status"] != "reported":
            merged["native_totals"][key] = old
    if previous.get("measurement_coverage") == "partial":
        merged["measurement_coverage"] = "partial"
        merged["measurement_scope"] = previous.get("measurement_scope", "attempt_snapshot")
    return merged


def summarize(observations: list[dict[str, Any]], *, attempts: int, unknown_attempts: int) -> dict[str, Any]:
    summary = unknown_summary()
    summary.update(attempts=attempts, unknown_attempts=unknown_attempts)
    summary["receipt_ids"] = [item["receipt_id"] for item in observations]
    for key in TOKEN_FIELDS:
        values = [item["usage"]["tokens"][key] for item in observations]
        known = [item["value"] for item in values if item["status"] in {"reported", "partial"}]
        statuses = {item["status"] for item in values}
        summary["tokens"][key] = {
            "value": sum(known) if known else None,
            "status": "reported" if known and len(known) == len(values) and statuses == {"reported"} and not unknown_attempts
                      else "partial" if known else "invalid" if "invalid" in statuses else "not_reported",
        }
    money: dict[str, Decimal] = {}
    for item in observations:
        for charge in item["usage"]["money"]:
            currency = charge["currency"]
            money[currency] = add_money((money.get(currency, Decimal(0)), charge["amount"]))
    summary["money"] = [{"currency": key, "amount": format(amount, "f"), "source": "provider_reported"}
                        for key, amount in sorted(money.items())]
    for key in NATIVE_TOTAL_FIELDS:
        fields = [item["usage"].get("native_totals", {}).get(key, {}) for item in observations]
        values = [item["value"] for item in fields if item.get("value") is not None and item.get("status") in {"reported", "partial"}]
        summary["native_totals"][key] = {"value": sum(values) if values else None,
            "status": "reported" if values and len(values) == len(fields) and not unknown_attempts else "partial" if values else "not_reported"}
    known_count = sum(item["status"] == "reported" for item in summary["tokens"].values())
    summary["measurement_coverage"] = "partial" if any(item["usage"].get("measurement_coverage") == "partial" for item in observations) else "complete"
    complete = observations and known_count == len(TOKEN_FIELDS) and not unknown_attempts and all(item["usage"].get("measurement_coverage", "complete") == "complete" for item in observations)
    summary["coverage"] = "complete" if complete else "partial" if any(item["value"] is not None for item in (*summary["tokens"].values(), *summary["native_totals"].values())) or money else "unknown"
    summary["money_coverage"] = "complete" if observations and not unknown_attempts and all(item["usage"]["money"] and item["usage"].get("money_coverage", "complete") == "complete" for item in observations) else "partial" if money else "unknown"
    return summary


def aggregate_metrics_conn(conn, run_join: str, run_clause: str, params) -> dict[str, Any]:
    """Aggregate the existing run summaries without materializing lifetime JSON."""
    register_decimal_sum(conn)
    scope = "WITH metric_usage AS (SELECT COALESCE(NULLIF(runs.usage_summary_json,''),'{}') AS body FROM runs" + run_join + run_clause + ") "
    fields = ["COUNT(*) AS runs", "COALESCE(SUM(json_extract(body,'$.attempts')),0) AS attempts",
        "COALESCE(SUM(CASE WHEN COALESCE(json_extract(body,'$.unknown_attempts'),0)>0 THEN json_extract(body,'$.unknown_attempts') WHEN COALESCE(json_extract(body,'$.coverage'),'unknown')='unknown' THEN 1 ELSE 0 END),0) AS unknown_attempts",
        "MAX(CASE WHEN json_extract(body,'$.measurement_coverage')='partial' THEN 1 ELSE 0 END) AS partial_measurement",
        "COALESCE(SUM(CASE WHEN json_array_length(body,'$.money')>0 AND COALESCE(json_extract(body,'$.money_coverage'),'complete')='complete' THEN 1 ELSE 0 END),0) AS complete_money"]
    for group, names in (("tokens",TOKEN_FIELDS),("native_totals",NATIVE_TOTAL_FIELDS)):
        for name in names:
            alias = group + "_" + name
            path = "$." + group + "." + name
            known = f"json_extract(body,'{path}.status') IN ('reported','partial') AND json_extract(body,'{path}.value') IS NOT NULL"
            fields.extend((f"SUM(CASE WHEN {known} THEN json_extract(body,'{path}.value') ELSE 0 END) AS {alias}_value",
                f"COALESCE(SUM(CASE WHEN {known} THEN 1 ELSE 0 END),0) AS {alias}_known",
                f"COALESCE(SUM(CASE WHEN json_extract(body,'{path}.status')='reported' THEN 1 ELSE 0 END),0) AS {alias}_reported",
                f"MAX(CASE WHEN json_extract(body,'{path}.status')='invalid' THEN 1 ELSE 0 END) AS {alias}_invalid"))
    totals = conn.execute(scope + "SELECT " + ",".join(fields) + " FROM metric_usage",params).fetchone()
    money = conn.execute(scope + """SELECT json_extract(m.value,'$.currency') AS currency,
        usage_decimal_sum(json_extract(m.value,'$.amount')) AS amount
        FROM metric_usage JOIN json_each(body,'$.money') m GROUP BY currency ORDER BY currency""",params).fetchall()
    result = unknown_summary()
    result.update(attempts=totals["attempts"],unknown_attempts=totals["unknown_attempts"],
                  measurement_coverage="partial" if totals["partial_measurement"] else "complete")
    for group,names in (("tokens",TOKEN_FIELDS),("native_totals",NATIVE_TOTAL_FIELDS)):
        for name in names:
            alias=group+"_"+name
            known=totals[alias+"_known"]
            complete=known and known==totals["runs"] and not totals["unknown_attempts"]
            if group=="tokens":
                complete=complete and totals[alias+"_reported"]==totals["runs"]
            result[group][name]={"value":totals[alias+"_value"] if known else None,
                "status":"reported" if complete else "partial" if known else "invalid" if totals[alias+"_invalid"] else "not_reported"}
    result["money"]=[{"currency":item["currency"],"amount":item["amount"],"source":"provider_reported"} for item in money]
    complete = totals["runs"] and not totals["unknown_attempts"] and not totals["partial_measurement"] and all(item["status"]=="reported" for item in result["tokens"].values())
    result["coverage"]="complete" if complete else "partial" if money or any(item["value"] is not None for item in (*result["tokens"].values(),*result["native_totals"].values())) else "unknown"
    result["money_coverage"]="complete" if totals["runs"] and not totals["unknown_attempts"] and totals["complete_money"]==totals["runs"] else "partial" if money else "unknown"
    return result


def ensure_schema(conn) -> None:
    """Called only by the runtime Store schema owner, including fresh databases."""
    ensure_account_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS usage_receipts (
        receipt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL REFERENCES run_attempts(attempt_id) ON DELETE CASCADE,
        receipt_key TEXT NOT NULL, revision INTEGER NOT NULL, measurement_kind TEXT NOT NULL,
        invocation_at TEXT NOT NULL, usage_json TEXT NOT NULL, observed_at TEXT NOT NULL,
        UNIQUE(attempt_id, receipt_key))""")
    for table in ("runs", "run_attempts"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        additions = {"usage_summary_json": "TEXT NOT NULL DEFAULT '{}'",
                     "upstream_request_id": "TEXT NOT NULL DEFAULT ''",
                     "failure_status_source": "TEXT NOT NULL DEFAULT 'unavailable'",
                     "failure_status_code": "INTEGER"}
        for column, definition in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if table == "run_attempts" and "usage_accounting_state" not in columns:
            conn.execute("ALTER TABLE run_attempts ADD COLUMN usage_accounting_state TEXT NOT NULL DEFAULT 'legacy_unknown'")
        if table == "run_attempts" and "usage_revision" not in columns:
            conn.execute("ALTER TABLE run_attempts ADD COLUMN usage_revision INTEGER NOT NULL DEFAULT 0")


def ensure_account_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS account_usage_receipts (
        receipt_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, revision INTEGER NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(account_usage_receipts)")}
    if "complete" not in columns:
        conn.execute("ALTER TABLE account_usage_receipts ADD COLUMN complete INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE account_usage_receipts SET complete=CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN 1 ELSE 0 END")
    if "duration_seconds" not in columns:
        conn.execute("ALTER TABLE account_usage_receipts ADD COLUMN duration_seconds REAL")


def account_duration_delta(conn, *, receipt_id: str, account_id: str,
                           tenant_id: str, owner_id: str, duration: float) -> float:
    """The wrapper's measured duration has its own once-only receipt slot."""
    changed = conn.execute("""UPDATE account_usage_receipts SET duration_seconds=?
        WHERE receipt_id=? AND account_id=? AND tenant_id=? AND owner_id=?
        AND duration_seconds IS NULL""", (duration,receipt_id,account_id,tenant_id,owner_id))
    return duration if changed.rowcount else 0.0


def account_usage_delta(conn, *, receipt_id: str, revision: int, account_id: str,
                        tenant_id: str, owner_id: str, input_tokens: int | None,
                        output_tokens: int | None, coverage_complete: bool | None = None) -> tuple[bool, int | None, int | None, int]:
    """Idempotent local counter projection in the caller's account transaction."""
    previous = conn.execute("SELECT * FROM account_usage_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
    if previous is not None and (previous["account_id"], previous["tenant_id"], previous["owner_id"]) != (account_id, tenant_id, owner_id):
        raise ValueError("Usage receipt owner mismatch")
    if previous is not None and revision < previous["revision"]:
        return False, None, None, 0
    before_input = previous["input_tokens"] if previous is not None else None
    before_output = previous["output_tokens"] if previous is not None else None
    if previous is not None and revision == previous["revision"]:
        if ((before_input is not None and input_tokens is not None and before_input != input_tokens)
                or (before_output is not None and output_tokens is not None and before_output != output_tokens)):
            raise ValueError("Usage receipt revision conflict")
    merged_input = input_tokens if input_tokens is not None else before_input
    merged_output = output_tokens if output_tokens is not None else before_output
    complete = (merged_input is not None and merged_output is not None) if coverage_complete is None else coverage_complete
    unknown = int(not complete)
    previous_unknown = int(not previous["complete"]) if previous is not None else 0
    conn.execute("""INSERT INTO account_usage_receipts
        (receipt_id,account_id,tenant_id,owner_id,revision,input_tokens,output_tokens,complete) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(receipt_id) DO UPDATE SET revision=excluded.revision,
        input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,complete=excluded.complete""",
        (receipt_id, account_id, tenant_id, owner_id, revision, merged_input, merged_output, int(complete)))
    return (previous is None,
            None if merged_input is None else merged_input - (before_input or 0),
            None if merged_output is None else merged_output - (before_output or 0),
            unknown - previous_unknown)


def safe_upstream_request_id(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else ""


def reconcile_conn(conn, *, run_id: str, attempt_id: str, usage: dict[str, Any] | None,
                   observed_at: str, invocation_at: str, total_cost_usd: object = None,
                   receipt_key: str = "attempt", revision: int = 0,
                   measurement_kind: str = "snapshot") -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", receipt_key) or type(revision) is not int or revision < 0 or measurement_kind not in {"snapshot", "delta"}:
        raise ValueError("Usage receipt identity is invalid")
    attempt = conn.execute("SELECT * FROM run_attempts WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)).fetchone()
    if attempt is None or not invocation_at or str(attempt["runtime_invoked_at"] or "") != invocation_at:
        raise ValueError("Usage receipt must match an invoked attempt")
    # One attempt snapshot supersedes its provisional fields; independent native
    # request deltas require distinct IDs. Never combine overlapping scopes.
    kinds = {row[0] for row in conn.execute("SELECT measurement_kind FROM usage_receipts WHERE attempt_id=?", (attempt_id,))}
    if kinds and measurement_kind not in kinds:
        raise ValueError("Overlapping usage measurement scopes")
    if measurement_kind == "snapshot" and receipt_key != "attempt":
        raise ValueError("An attempt snapshot must use its stable attempt key")
    incoming = normalize_observation(usage, total_cost_usd=total_cost_usd)
    previous = conn.execute("SELECT * FROM usage_receipts WHERE attempt_id=? AND receipt_key=?", (attempt_id, receipt_key)).fetchone()
    if previous is not None:
        if revision < previous["revision"]:
            return json.loads(attempt["usage_summary_json"] or "{}")
        old = json.loads(previous["usage_json"])
        merged = merge_observation(old, incoming)
        if revision == previous["revision"]:
            # Same-revision enrichment may fill unknown fields, but cannot change
            # reported values or charges. Replaying partial receipts is harmless.
            for key in TOKEN_FIELDS:
                if old["tokens"][key]["status"] == "reported" and incoming["tokens"][key]["status"] == "reported" and old["tokens"][key]["value"] != incoming["tokens"][key]["value"]:
                    raise ValueError("Usage receipt revision conflict")
            if old["money"] and incoming["money"] and old["money"] != incoming["money"]:
                raise ValueError("Usage receipt revision conflict")
        incoming = merged
        receipt_id = previous["receipt_id"]
    else:
        import hashlib
        receipt_id = "usage_" + hashlib.sha256(f"{attempt_id}\0{receipt_key}".encode()).hexdigest()
    changed = previous is None or incoming != json.loads(previous["usage_json"]) or revision != previous["revision"]
    conn.execute("""INSERT INTO usage_receipts
        (receipt_id,run_id,attempt_id,receipt_key,revision,measurement_kind,invocation_at,usage_json,observed_at)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(attempt_id,receipt_key) DO UPDATE SET
        revision=excluded.revision,usage_json=excluded.usage_json,observed_at=excluded.observed_at""",
        (receipt_id, run_id, attempt_id, receipt_key, revision, measurement_kind, invocation_at,
         json.dumps(incoming, sort_keys=True), observed_at))
    conn.execute("UPDATE run_attempts SET usage_accounting_state='reconciled' WHERE attempt_id=?", (attempt_id,))
    if changed:
        conn.execute("UPDATE run_attempts SET usage_revision=usage_revision+1 WHERE attempt_id=?", (attempt_id,))
    from .spend_admission import settle_reservations
    if attempt["ended_at"] is not None:
        settle_reservations(conn, attempt_id)
    receipts = [{"receipt_id": row["receipt_id"], "attempt_id": row["attempt_id"], "usage": json.loads(row["usage_json"])}
                for row in conn.execute("SELECT * FROM usage_receipts WHERE run_id=? ORDER BY receipt_id", (run_id,))]
    for target, selected, count, missing in (
        ("run_attempts", [item for item in receipts if item["attempt_id"] == attempt_id], 1, 0),
        ("runs", receipts, None, None),
    ):
        if target == "runs":
            invoked = {row[0] for row in conn.execute("SELECT attempt_id FROM run_attempts WHERE run_id=? AND runtime_invoked_at IS NOT NULL", (run_id,))}
            count, missing = len(invoked), len(invoked - {item["attempt_id"] for item in receipts})
        summary = summarize(selected, attempts=count, unknown_attempts=missing)
        key = "attempt_id" if target == "run_attempts" else "run_id"
        identity = attempt_id if target == "run_attempts" else run_id
        conn.execute(f"UPDATE {target} SET usage_summary_json=? WHERE {key}=?", (json.dumps(summary, sort_keys=True), identity))
        if target == "runs":
            # Compatibility integers contain known subtotals only. A missing
            # receipt must never replace a prior known observation with zero.
            for field in TOKEN_FIELDS:
                value = summary["tokens"][field]["value"]
                if value is not None:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE run_id=?", (value, run_id))
        else:
            project_account_conn(conn, run_id, attempt_id, summary)
    return summary


def project_account_conn(conn, run_id: str, attempt_id: str, summary: dict) -> None:
    """Apply late/recovered receipts to existing account counters in this transaction."""
    attempt = conn.execute("SELECT * FROM run_attempts WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)).fetchone()
    # A recovered usage file can arrive before terminal lifecycle authority.
    # Preserve the receipt immediately, but never infer a failure from running
    # work. The terminal attempt trace reapplies this projection in its txn.
    if attempt is None or not attempt["runtime_invoked_at"] or attempt["ended_at"] is None:
        return
    row = conn.execute("SELECT * FROM allowed_ai_start_fences WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)).fetchone()
    if row is None:
        return
    try:
        binding = json.loads(row["receipt_json"] or "{}")
    except (TypeError, ValueError):
        return
    account_id = binding.get("connection_id") if isinstance(binding, dict) else None
    if not account_id or binding.get("run_id") != run_id:
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_accounts'").fetchone():
        return
    account = conn.execute("SELECT 1 FROM provider_accounts WHERE account_id=? AND tenant_id=? AND owner_id=?", (account_id, row["tenant_id"], row["owner_id"])).fetchone()
    if account is None:
        return
    revision = attempt["usage_revision"]
    first, input_delta, output_delta, unknown_delta = account_usage_delta(
        conn, receipt_id=attempt_id, revision=revision, account_id=account_id,
        tenant_id=row["tenant_id"], owner_id=row["owner_id"],
        input_tokens=summary["tokens"]["input_tokens"]["value"] if summary["tokens"]["input_tokens"]["value"] is not None else summary["native_totals"]["input_tokens"]["value"],
        output_tokens=summary["tokens"]["output_tokens"]["value"], coverage_complete=summary["measurement_coverage"] == "complete" and summary["tokens"]["input_tokens"]["status"] == "reported" and summary["tokens"]["output_tokens"]["status"] == "reported")
    conn.execute("""UPDATE provider_accounts SET observed_runs=observed_runs+?,
        observed_failures=observed_failures+?,observed_unknown_runs=observed_unknown_runs+?,
        observed_input_tokens=CASE WHEN ? IS NULL THEN observed_input_tokens ELSE COALESCE(observed_input_tokens,0)+? END,
        observed_output_tokens=CASE WHEN ? IS NULL THEN observed_output_tokens ELSE COALESCE(observed_output_tokens,0)+? END
        WHERE account_id=? AND tenant_id=? AND owner_id=?""",
        (int(first), int(first and attempt["state"] != "completed"), unknown_delta,
         input_delta, input_delta, output_delta, output_delta, account_id, row["tenant_id"], row["owner_id"]))
