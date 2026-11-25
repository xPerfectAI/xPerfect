"""How many Worker runs one connected account may serve at once.

Every account is leased to one run at a time, except the explicit "Existing Claude sign-in" of a single-user
local host. That account copies no credential: each run uses the owner's own OS sign-in with a private config
folder, the way several Claude Code sessions share one sign-in on one computer. So its Worker runs (lease lanes
ending in ":mission") may share it, up to the host's existing per-account limit (WPR_HOST_ACCOUNT_ACTIVE_LIMIT).
Every other use of it (setup, verify, disconnect, recovery) still needs it alone, and accounts whose credentials
are projected or copied stay one run at a time.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

CURRENT_NATIVE_PREFIX = "native-home://current-claude/"
DEFAULT_ACCOUNT_RUN_LIMIT = 4


def current_native_run_limit() -> int:
    """The existing per-account active-run limit, read the same way as the host capacity."""
    raw = str(os.environ.get("WPR_HOST_ACCOUNT_ACTIVE_LIMIT") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_ACCOUNT_RUN_LIMIT
    except ValueError:
        value = DEFAULT_ACCOUNT_RUN_LIMIT
    return max(1, min(value, 256))


def is_run_lane(lane: str) -> bool:
    return str(lane or "").endswith(":mission")


def is_current_native_locator(locator: Any) -> bool:
    return str(locator or "").startswith(CURRENT_NATIVE_PREFIX)


def current_native_account(conn: Any, account_id: str) -> bool:
    """Whether this account is the explicit existing Claude sign-in (False when the table does not exist)."""
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_accounts'").fetchone()
    if table is None:
        return False
    row = conn.execute("SELECT secret_locator FROM provider_accounts WHERE account_id = ?", (account_id,)).fetchone()
    return row is not None and is_current_native_locator(row[0])


def room_for_another_run(lanes: Iterable[str]) -> bool:
    """A new Worker run may join these unreleased leases of an existing Claude sign-in."""
    lanes = [str(lane or "") for lane in lanes]
    return all(is_run_lane(lane) for lane in lanes) and len(lanes) < current_native_run_limit()
