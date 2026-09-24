"""Owner-session UI facade; native peer credentials never enter the browser."""

from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool


def install_peer_ui_routes(app, client_for_request):
    def segment(value):
        return quote(value, safe="")

    def forward(request, method, path, payload=None, *, human=False):
        client = client_for_request(request, human_confirmation=human)
        try:
            return client._request(method, path, json_body=payload)
        except httpx.HTTPStatusError as error:
            # Pass bounded typed errors; do not forward a provider response body.
            try:
                detail = error.response.json().get("detail", {})
                code = detail.get("code") if isinstance(detail, dict) else None
            except ValueError:
                code = None
            raise HTTPException(
                status_code=error.response.status_code,
                detail={"code": code or "peer_request_failed"},
            ) from None
        except httpx.HTTPError:
            raise HTTPException(
                status_code=503, detail={"code": "peer_runtime_unavailable"}
            ) from None

    @app.get("/api/peer-workspaces")
    def workspace_choices(request: Request):
        return forward(request, "GET", "/v1/peer-workspaces")

    @app.get("/api/workspaces/{workspace_id}/peer-policy")
    def policy(workspace_id: str, request: Request):
        return forward(
            request, "GET", f"/v1/workspaces/{segment(workspace_id)}/peer-policy"
        )

    @app.put("/api/workspaces/{workspace_id}/peer-policy")
    async def policy_update(workspace_id: str, request: Request):
        return await run_in_threadpool(
            forward,
            request,
            "PUT",
            f"/v1/workspaces/{segment(workspace_id)}/peer-policy",
            await request.json(),
            human=True,
        )

    @app.post("/api/peer-grants")
    async def grant(request: Request):
        return await run_in_threadpool(
            forward,
            request,
            "POST",
            "/v1/peer-grants",
            await request.json(),
            human=True,
        )

    @app.get("/api/workers/{worker_id}/peer-access-options")
    def permission_options(worker_id: str, request: Request):
        return forward(
            request, "GET", f"/v1/workers/{segment(worker_id)}/peer-access-options"
        )

    @app.post("/api/peer-grants/batch")
    async def permission_batch(request: Request):
        return await run_in_threadpool(
            forward,
            request,
            "POST",
            "/v1/peer-grants/batch",
            await request.json(),
            human=True,
        )

    @app.delete("/api/peer-grants/{grant_id}")
    def revoke(grant_id: str, request: Request):
        return forward(
            request, "DELETE", f"/v1/peer-grants/{segment(grant_id)}", human=True
        )

    @app.get("/api/workers/{worker_id}/peers")
    def peers(worker_id: str, request: Request):
        return forward(request, "GET", f"/v1/workers/{segment(worker_id)}/peers")

    @app.get("/api/workers/{worker_id}/peer-grants")
    def grants(worker_id: str, request: Request):
        return forward(request, "GET", f"/v1/workers/{segment(worker_id)}/peer-grants")

    @app.get("/api/workers/{worker_id}/peer-messages")
    def messages(worker_id: str, request: Request, after: int = 0, limit: int = 50):
        return forward(
            request,
            "GET",
            f"/v1/workers/{segment(worker_id)}/peer-messages?after={after}&limit={limit}",
        )

    @app.post("/api/workers/{worker_id}/peer-messages")
    async def send(worker_id: str, request: Request):
        return await run_in_threadpool(
            forward,
            request,
            "POST",
            f"/v1/workers/{segment(worker_id)}/peer-messages",
            await request.json(),
        )
