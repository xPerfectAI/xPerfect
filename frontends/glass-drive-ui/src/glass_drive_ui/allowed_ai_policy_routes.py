"""Owner-session facade for the versioned Allowed AI policy service."""

from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["all", "selected"]
    ids: list[str] = Field(default_factory=list, max_length=256)


class HarnessPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile: str = Field(min_length=1, max_length=120)
    models: SelectionRequest
    connections: SelectionRequest


class PolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    mode: Literal["inherit", "all_authorized", "selected"]
    harnesses: list[HarnessPolicyRequest] = Field(max_length=64)


class AllowedAiUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    policy: PolicyRequest


def _validate_policy(policy: PolicyRequest, scope: str) -> None:
    if scope == "project" and policy.mode == "inherit":
        raise HTTPException(status_code=422, detail={"code": "invalid_policy"})
    if policy.mode != "selected" and policy.harnesses:
        raise HTTPException(status_code=422, detail={"code": "invalid_policy"})
    profiles = set()
    for harness in policy.harnesses:
        if harness.profile in profiles:
            raise HTTPException(status_code=422, detail={"code": "invalid_policy"})
        profiles.add(harness.profile)
        for selection in (harness.models, harness.connections):
            if len(set(selection.ids)) != len(selection.ids):
                raise HTTPException(status_code=422, detail={"code": "invalid_policy"})
            if selection.mode == "all" and selection.ids:
                raise HTTPException(status_code=422, detail={"code": "invalid_policy"})
            if any(not isinstance(item, str) or not item.strip() for item in selection.ids):
                raise HTTPException(status_code=422, detail={"code": "invalid_policy"})


def install_allowed_ai_policy_ui_routes(app, client_for_request):
    def forward(request, scope, scope_id, method="GET", payload=None, kind="execution-policy"):
        path_scope = "projects" if scope == "project" else "workspaces"
        path = f"/v1/{path_scope}/{quote(scope_id, safe='')}/{kind}"
        try:
            return client_for_request(
                request, human_confirmation=method != "GET"
            )._request(method, path, json_body=payload)
        except httpx.HTTPStatusError as error:
            code = None
            try:
                detail = error.response.json().get("detail", {})
                if isinstance(detail, dict):
                    candidate = detail.get("code")
                    if candidate in {
                        "policy_changed",
                        "policy_unavailable",
                        "selection_unavailable",
                        "options_unavailable",
                        "scope_forbidden",
                        "scope_missing",
                    }:
                        code = candidate
            except (ValueError, AttributeError, TypeError):
                pass
            raise HTTPException(
                status_code=error.response.status_code,
                detail={"code": code or "allowed_ai_request_failed"},
            ) from None
        except httpx.HTTPError:
            raise HTTPException(
                status_code=503, detail={"code": "allowed_ai_runtime_unavailable"}
            ) from None

    def get_policy(scope, scope_id, request: Request):
        return forward(request, scope, scope_id)

    def get_options(scope, scope_id, request: Request):
        return forward(request, scope, scope_id, kind="execution-options")

    async def update_policy(scope, scope_id, payload, request: Request):
        _validate_policy(payload.policy, scope)
        return await run_in_threadpool(
            forward,
            request,
            scope,
            scope_id,
            "PUT",
            payload.model_dump(),
        )

    @app.get("/api/projects/{project_id}/execution-policy")
    def project_policy(project_id: str, request: Request):
        return get_policy("project", project_id, request)

    @app.get("/api/projects/{project_id}/execution-options")
    def project_options(project_id: str, request: Request):
        return get_options("project", project_id, request)

    @app.put("/api/projects/{project_id}/execution-policy")
    async def update_project_policy(
        project_id: str, payload: AllowedAiUpdateRequest, request: Request
    ):
        return await update_policy("project", project_id, payload, request)

    @app.get("/api/workspaces/{workspace_id}/execution-policy")
    def workspace_policy(workspace_id: str, request: Request):
        return get_policy("workspace", workspace_id, request)

    @app.get("/api/workspaces/{workspace_id}/execution-options")
    def workspace_options(workspace_id: str, request: Request):
        return get_options("workspace", workspace_id, request)

    @app.put("/api/workspaces/{workspace_id}/execution-policy")
    async def update_workspace_policy(
        workspace_id: str, payload: AllowedAiUpdateRequest, request: Request
    ):
        return await update_policy("workspace", workspace_id, payload, request)
