from __future__ import annotations


PROFILE_BACKEND_LABELS = {
    "codex-cli": "codex-cli",
    "claude-code": "claude-code",
}


def derive_legacy_backend_label(
    *,
    profile: object = "",
    runtime: object = "",
    backend: object = "",
    execution_mode: object = "",
    default: object = "",
) -> str:
    """Return the compatibility backend label without letting stale backend win."""

    clean_profile = str(profile or "").strip()
    if clean_profile in PROFILE_BACKEND_LABELS:
        return PROFILE_BACKEND_LABELS[clean_profile]
    if clean_profile.startswith("openclaw"):
        return "openclaw"
    for candidate in (runtime, clean_profile, backend, execution_mode, default):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""
