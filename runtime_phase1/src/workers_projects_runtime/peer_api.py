"""Thin owner-authenticated routes for the peer collaboration owner."""

import asyncio
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .peer_collaboration import (
    PeerError,
    PeerGrantRequest,
    PeerMessageRequest,
    PeerPolicyUpdate,
)
from .peer_permissions import PeerGrantBatchRequest, access_options, grant_batch


class PeerContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_worker_id: str = Field(min_length=1, max_length=128)
    grant_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)


def install_peer_routes(app, peers, principal, require_human):
    @app.exception_handler(PeerError)
    async def peer_error(_request: Request, error: PeerError):
        return JSONResponse(
            status_code=error.status_code, content={"detail": {"code": error.code}}
        )

    def owner(request, *, human=False):
        ctx, tenant, user = principal(request)
        if not user or getattr(ctx, "auth_mode", "") == "signed_link":
            raise PeerError("peer_owner_session_required", 403)
        if human:
            require_human(ctx)
        return {"tenant_id": tenant, "owner_id": user}

    @app.get("/v1/peer-workspaces")
    def workspace_choices(request: Request):
        return peers.workspace_choices(**owner(request))

    @app.get("/v1/workspaces/{workspace_id}/peer-policy")
    def policy(workspace_id: str, request: Request):
        return peers.policy(workspace_id, **owner(request))

    @app.put("/v1/workspaces/{workspace_id}/peer-policy")
    def update_policy(workspace_id: str, payload: PeerPolicyUpdate, request: Request):
        return peers.set_policy(
            workspace_id, request=payload, **owner(request, human=True)
        )

    @app.post("/v1/peer-grants", status_code=201)
    def grant(payload: PeerGrantRequest, request: Request):
        return peers.grant(request=payload, **owner(request, human=True))

    @app.get("/v1/workers/{worker_id}/peer-access-options")
    def permission_options(worker_id: str, request: Request):
        return access_options(peers, worker_id, **owner(request))

    @app.post("/v1/peer-grants/batch", status_code=201)
    def permission_batch(payload: PeerGrantBatchRequest, request: Request):
        return grant_batch(peers, payload, **owner(request, human=True))

    @app.delete("/v1/peer-grants/{grant_id}")
    def revoke(grant_id: str, request: Request):
        return peers.revoke(grant_id, **owner(request, human=True))

    @app.get("/v1/workers/{worker_id}/peers")
    def discover(worker_id: str, request: Request):
        return peers.discover(worker_id, **owner(request))

    @app.get("/v1/workers/{worker_id}/peer-grants")
    def grants(worker_id: str, request: Request):
        return peers.grants(worker_id, **owner(request))

    @app.post("/v1/workers/{worker_id}/peer-messages", status_code=202)
    def send(worker_id: str, payload: PeerMessageRequest, request: Request):
        return peers.send(worker_id, request=payload, **owner(request))

    @app.get("/v1/workers/{worker_id}/peer-messages")
    def messages(worker_id: str, request: Request, after: int = 0, limit: int = 50):
        return peers.messages(worker_id, after=after, limit=limit, **owner(request))

    @app.post("/v1/workers/{worker_id}/peer-context")
    def context(worker_id: str, payload: PeerContextRequest, request: Request):
        return peers.read_context(worker_id, **payload.model_dump(), **owner(request))

    @app.get("/v1/workers/{worker_id}/peer-wait")
    async def wait(
        worker_id: str, request: Request, after: int = 0, timeout_seconds: float = 10
    ):
        if not 0 <= timeout_seconds <= 20:
            raise PeerError("peer_wait_invalid", 422)
        end = time.monotonic() + timeout_seconds
        while True:
            result = peers.messages(worker_id, after=after, **owner(request))
            if result["items"] or time.monotonic() >= end:
                return result | {"timed_out": not bool(result["items"])}
            await asyncio.sleep(min(0.2, max(0, end - time.monotonic())))
