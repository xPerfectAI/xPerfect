"""Explicit deployment capability; unknown values never fall back to host mode."""
import os


def packaged_linux() -> bool:
    profile = os.environ.get("XPERFECT_EXECUTION_PROFILE", "")
    if profile not in {"", "local-linux", "hosted-xfs"}:
        raise RuntimeError("Unsupported xPerfect execution profile")
    return bool(profile)
