from __future__ import annotations

import os
from urllib.parse import urlencode


OPERATOR_URL_SURFACES = {"", "web", "browser", "desktop", "playground", "librechat", "glasshive"}


def operator_base_url() -> str:
    return os.environ.get("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780").strip().rstrip("/")


def surface_can_open_operator_url(surface: str | None) -> bool:
    normalized = str(surface or "").strip().lower()
    return normalized in OPERATOR_URL_SURFACES


def build_watch_url(
    worker_id: str,
    project_id: str | None,
    *,
    surface: str | None = "desktop",
    base_url: str | None = None,
) -> str | None:
    normalized_worker_id = str(worker_id or "").strip()
    if not normalized_worker_id:
        return None
    query = {
        "surface": str(surface or "desktop").strip() or "desktop",
    }
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id:
        query["project_id"] = normalized_project_id
    return f"{(base_url or operator_base_url()).rstrip('/')}/watch/{normalized_worker_id}?{urlencode(query)}"


def surface_aware_watch_url(
    worker_id: str,
    project_id: str | None,
    *,
    request_surface: str | None,
    watch_surface: str | None = "desktop",
    base_url: str | None = None,
) -> str | None:
    if not surface_can_open_operator_url(request_surface):
        return None
    return build_watch_url(worker_id, project_id, surface=watch_surface, base_url=base_url)
