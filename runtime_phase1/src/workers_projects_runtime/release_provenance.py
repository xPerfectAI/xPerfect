from __future__ import annotations

import os


def release_provenance() -> dict[str, str]:
    """Return public-safe immutable-release identity shared by all GlassHive services."""

    return {
        "release_id": str(os.environ.get("GLASSHIVE_RELEASE_ID") or "").strip(),
        "parent_revision": str(os.environ.get("GLASSHIVE_PARENT_REVISION") or "").strip(),
        "glasshive_revision": str(
            os.environ.get("GLASSHIVE_COMPONENT_REVISION") or ""
        ).strip(),
    }
