"""Owner-session facade for creating a shared execution workspace."""

from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool


class ExecutionWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    execution_mode: Literal["docker", "host"] = "docker"
    file_placement: Literal["common", "member_private"] = "common"


def _runtime_detail(error: httpx.HTTPStatusError):
    try:
        detail = error.response.json().get("detail")
    except (ValueError, AttributeError, TypeError):
        detail = None
    if isinstance(detail, dict):
        safe = {
            key: value.strip()[:1000]
            for key in ("code", "message", "recovery")
            if isinstance((value := detail.get(key)), str) and value.strip()
        }
        if safe.get("message"):
            return safe
    if isinstance(detail, str) and detail.strip() and error.response.status_code < 500:
        return detail.strip()[:1000]
    return {"code": "workspace_runtime_unavailable"}


def install_workspace_ui_routes(app, client_for_request):
    def forward(request: Request, project_id: str, payload: dict):
        try:
            return client_for_request(request, human_confirmation=True)._request(
                "POST",
                f"/v1/projects/{quote(project_id, safe='')}/execution-workspaces",
                json_body={"mode": "shared", **payload},
            )
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status_code=error.response.status_code,
                detail=_runtime_detail(error),
            ) from None
        except httpx.HTTPError:
            raise HTTPException(
                status_code=503,
                detail={"code": "workspace_runtime_unavailable"},
            ) from None

    @app.post("/api/projects/{project_id}/execution-workspaces", status_code=201)
    async def create_workspace(
        project_id: str,
        payload: ExecutionWorkspaceRequest,
        request: Request,
    ):
        body = payload.model_dump()
        return await run_in_threadpool(forward, request, project_id, body)
