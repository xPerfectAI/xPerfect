from __future__ import annotations


# Public/durable terminal truth has exactly three outcomes. Provider-level
# interruption remains an internal execution detail and must be projected as a
# cancellation (explicit stop) or failure (unexpected ownership loss).
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})


def is_terminal_run_state(state: object) -> bool:
    return str(state or "").strip() in TERMINAL_RUN_STATES
