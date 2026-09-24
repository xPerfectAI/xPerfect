"""Owner-session facade for the runtime's versioned worker configuration."""

from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["inherit", "selected"]
    source_ids: list[str]
    inline_chars: int = Field(ge=0, le=262144)


class ToolsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["inherit", "selected"]
    mcp_server_ids: list[str]


class BackgroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    max_parallel_runs: int = Field(ge=1, le=32)


class ConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    context: ContextRequest
    tools: ToolsRequest
    background: BackgroundRequest


def install_worker_configuration_ui_routes(app, client_for_request):
    def forward(request, worker_id, method="GET", payload=None, suffix=""):
        try:
            return client_for_request(
                request, human_confirmation=method != "GET"
            )._request(
                method,
                "/v1/workers/" + quote(worker_id, safe="") + "/configuration" + suffix,
                json_body=payload,
            )
        except httpx.HTTPStatusError as error:
            code = "configuration_unavailable"
            try:
                candidate = error.response.json().get("detail", {}).get("code")
                if candidate in {
                    "configuration_changed",
                    "selection_unavailable",
                    "worker_unavailable",
                    "context_unavailable",
                    "context_page_invalid",
                    "configuration_needs_attention",
                }:
                    code = candidate
            except (ValueError, AttributeError, TypeError):
                pass
            raise HTTPException(
                status_code=error.response.status_code,
                detail={"code": code},
            ) from None
        except httpx.HTTPError:
            raise HTTPException(
                status_code=503, detail={"code": "configuration_unavailable"}
            ) from None

    @app.get("/api/workers/{worker_id}/configuration")
    def configuration(worker_id: str, request: Request):
        return forward(request, worker_id)

    @app.put("/api/workers/{worker_id}/configuration")
    async def configure(
        worker_id: str, payload: ConfigurationRequest, request: Request
    ):
        return await run_in_threadpool(
            forward, request, worker_id, "PUT", payload.model_dump()
        )

    @app.get("/api/workers/{worker_id}/configuration/context/{source_id}")
    def context_source(
        worker_id: str,
        source_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        max_chars: int = Query(default=12000, ge=1, le=65536),
    ):
        return forward(
            request,
            worker_id,
            suffix=(
                "/context/"
                + quote(source_id, safe="")
                + f"?offset={offset}&max_chars={max_chars}"
            ),
        )
