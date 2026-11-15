"""Read-only processing readiness from retained runtime outcomes.

Routing cooldowns govern routing. This projection never expires failures or
changes routing, workers, retries or admission policy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


FAILURE_CLASSES = frozenset({
    "provider_auth_missing", "provider_auth_projection_unavailable",
    "provider_auth_preflight_unexpected", "provider_unauthorized",
    "provider_connected_account_reconnect_required", "provider_quota_exhausted",
    "provider_rate_limited", "provider_response_failed", "provider_temporarily_unavailable",
    "provider_request_rejected", "provider_progress_stalled",
    "runtime_sandbox_unavailable", "runtime_dependency_missing", "runtime_io_failed",
    "runtime_error", "unsupported_runtime_configuration", "worker_network_policy_unavailable",
    "service_processor_unexpected", "service_startup_fenced", "glasshive_evidence_check_failed",
    "native_route_contract_mismatch", "native_turn_incomplete", "runtime_terminated", "unknown",
})


def _time(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def summarize(outcomes: Iterable[dict]) -> dict:
    """Each scope clears only after a later-started successful invocation.

    No prompt, account, path, run ID or scope identifier is returned. A successful
    run already in flight when the failure finished cannot prove recovery.
    """
    scopes: dict[tuple, list[dict]] = {}
    for outcome in outcomes:
        scopes.setdefault(tuple(outcome["scope"]), []).append(outcome)
    unresolved: list[dict] = []
    proven = 0
    seen_failures = set()
    for records in scopes.values():
        failures = [row for row in records if row.get("failure_class") in FAILURE_CLASSES
                    and _time(row.get("ended_at")) is not None]
        successes = [row for row in records if row.get("state") == "completed"
                     and _time(row.get("started_at")) is not None
                     and _time(row.get("ended_at")) is not None]
        successes = [row for row in successes if _time(row["ended_at"]) >= _time(row["started_at"])]
        if successes:
            proven += 1
        for failure in failures:
            ended = _time(failure["ended_at"])
            recovered = any(_time(row["started_at"]) > ended
                            and _time(row["ended_at"]) >= _time(row["started_at"])
                            for row in successes)
            key = (tuple(failure["scope"]), failure.get("started_at"), failure["ended_at"], failure["failure_class"])
            if not recovered and key not in seen_failures:
                seen_failures.add(key)
                unresolved.append(failure)
    latest = max(unresolved, key=lambda row: _time(row["ended_at"]), default=None)
    return {
        "status": "down" if unresolved else "up" if proven else "unknown",
        "unresolved_failures": len(unresolved),
        "failure_class": latest["failure_class"] if latest else None,
        "last_failed_at": latest["ended_at"] if latest else None,
        "successful_scopes": proven,
    }
