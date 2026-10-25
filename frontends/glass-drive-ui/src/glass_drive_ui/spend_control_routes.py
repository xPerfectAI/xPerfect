"""Signed owner-session facade for admission settings."""
from urllib.parse import quote
import httpx
from fastapi import HTTPException, Request


def install_spend_control_ui_routes(app, client_for_request):
    def forward(request, path, method="GET", payload=None):
        try:
            return client_for_request(request, human_confirmation=method != "GET")._request(method, path, json_body=payload)
        except httpx.HTTPStatusError as exc:
            message = ("Check the currency, threshold, reserve and selected scope." if exc.response.status_code == 422
                       else "Sign in as the owner to change work pause settings." if exc.response.status_code in {401, 403}
                       else "Settings changed or could not be saved. Reload the current settings before trying again.")
            raise HTTPException(status_code=exc.response.status_code,
                detail=message) from None
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="Work pause settings are unavailable. Try reloading.") from None

    @app.get("/api/spend-policy")
    def owner_policy(request: Request):
        return forward(request, "/v1/spend-policy")

    @app.put("/api/spend-policy")
    def update_owner_policy(request: Request, payload: dict):
        return forward(request, "/v1/spend-policy", "PUT", payload)

    @app.get("/api/projects/{project_id}/spend-policy")
    def project_policy(project_id: str, request: Request):
        return forward(request, f"/v1/projects/{quote(project_id, safe='')}/spend-policy")

    @app.put("/api/projects/{project_id}/spend-policy")
    def update_project_policy(project_id: str, request: Request, payload: dict):
        return forward(request, f"/v1/projects/{quote(project_id, safe='')}/spend-policy", "PUT", payload)

    @app.get("/api/admission-holds")
    def holds(request: Request):
        return forward(request, "/v1/admission-holds")

    @app.post("/api/admission-holds")
    def hold(request: Request, payload: dict):
        return forward(request, "/v1/admission-holds", "POST", payload)

    @app.post("/api/admission-holds/{hold_id}/resume")
    def resume(hold_id: str, request: Request, payload: dict):
        return forward(request, f"/v1/admission-holds/{quote(hold_id, safe='')}/resume", "POST", payload)
