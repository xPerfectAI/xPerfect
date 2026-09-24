"""Owner UI projections for execution workspace members; runtime owns admission."""

from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool


class MemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=120)
    profile: str = ""
    effort: str | None = Field(default=None, max_length=64)
    provider_account_policy: str | None = Field(default=None, max_length=32)
    provider_account_id: str | None = Field(default=None, max_length=128)


def _safe_runtime_detail(error: httpx.HTTPStatusError):
    """Keep typed admission reasons useful while dropping provider internals."""
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
    return {"code": "member_runtime_unavailable"}


def install_member_ui_routes(app, client_for_request):
    def forward(request, path, method="GET", payload=None):
        try:
            return client_for_request(
                request, human_confirmation=method != "GET"
            )._request(method, path, json_body=payload)
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status_code=error.response.status_code,
                detail=_safe_runtime_detail(error),
            ) from None
        except httpx.HTTPError:
            raise HTTPException(
                status_code=503, detail={"code": "member_runtime_unavailable"}
            ) from None

    @app.get("/api/execution-workspaces/{workspace_id}/members")
    def members(workspace_id: str, request: Request):
        return forward(
            request, "/v1/workspaces/" + quote(workspace_id, safe="") + "/members"
        )

    @app.get("/api/member-profiles")
    def profiles(request: Request):
        return forward(request, "/v1/worker-profiles")

    @app.post("/api/execution-workspaces/{workspace_id}/members", status_code=201)
    async def add_member(workspace_id: str, payload: MemberRequest, request: Request):
        return await run_in_threadpool(
            forward,
            request,
            "/v1/workspaces/" + quote(workspace_id, safe="") + "/members",
            "POST",
            payload.model_dump(exclude_none=True),
        )
