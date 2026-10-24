"""Session-scoped UI adapter; file bytes use the existing streaming Files routes."""
from urllib.parse import quote, urlencode
import httpx
from fastapi import Body, HTTPException, Request


def install_personal_library_routes(app, client_for_request, request_identity, restricted_identity, runtime_error):
    def call(request, method, path, payload=None, worker_id=None):
        request.state.file_cookie_only = True
        identity = request_identity(request, worker_id)
        if restricted_identity(identity):
            raise HTTPException(403, detail="Sign in with management access to use My Library")
        try:
            return client_for_request(request, worker_id, file_write_scope=True).file_request(method, path, payload)
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "Library operation failed") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(503, detail="Library is unavailable. Your selections are kept; try again.") from exc

    @app.get("/api/workspace-starters")
    def starters(request: Request):
        return call(request, "GET", "/v1/workspace-starters")

    @app.post("/api/workspace-starters/{starter_id}/import", status_code=201)
    def import_starter(starter_id: str, request: Request, payload: dict = Body(...)):
        return call(request, "POST", f"/v1/workspace-starters/{quote(starter_id, safe='')}/import", payload)

    @app.get("/api/personal-library")
    def listing(request: Request, include_archived: bool = False):
        return call(request, "GET", "/v1/personal-library?" + urlencode({"include_archived": str(include_archived).lower()}))

    @app.post("/api/personal-library", status_code=201)
    def create(request: Request, payload: dict = Body(...)):
        return call(request, "POST", "/v1/personal-library", payload)

    @app.post("/api/personal-library/resolve")
    def resolve(request: Request, payload: dict = Body(...)):
        return call(request, "POST", "/v1/personal-library/resolve", payload)

    @app.get("/api/personal-library/{item_id}")
    def read(item_id: str, request: Request, version_id: str | None = None):
        path = f"/v1/personal-library/{quote(item_id, safe='')}"
        if version_id is not None:
            path += "?" + urlencode({"version_id": version_id})
        return call(request, "GET", path)

    @app.patch("/api/personal-library/{item_id}")
    def revise(item_id: str, request: Request, payload: dict = Body(...)):
        return call(request, "PATCH", f"/v1/personal-library/{quote(item_id, safe='')}", payload)

    @app.delete("/api/personal-library/{item_id}")
    def delete(item_id: str, request: Request, confirm: bool = False):
        return call(request, "DELETE", f"/v1/personal-library/{quote(item_id, safe='')}?" + urlencode({"confirm": str(confirm).lower()}))

    @app.post("/api/personal-library/{item_id}/archive")
    def archive(item_id: str, request: Request, payload: dict = Body(...)):
        return call(request, "POST", f"/v1/personal-library/{quote(item_id, safe='')}/archive", payload)

    @app.get("/api/workspace/{worker_id}/library")
    def worker_listing(worker_id: str, request: Request):
        return call(request, "GET", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library", worker_id=worker_id)

    @app.post("/api/workspace/{worker_id}/library/capture", status_code=201)
    def capture(worker_id: str, request: Request, payload: dict = Body(...)):
        return call(request, "POST", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library/capture", payload, worker_id)

    @app.post("/api/workspace/{worker_id}/library")
    def apply(worker_id: str, request: Request, payload: dict = Body(...)):
        return call(request, "POST", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library", payload, worker_id)

    @app.delete("/api/workspace/{worker_id}/library/{application_id}")
    def remove(worker_id: str, application_id: str, request: Request):
        return call(request, "DELETE", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library/{quote(application_id, safe='')}", worker_id=worker_id)

    @app.post("/api/workspace/{worker_id}/library/{application_id}/undo")
    def undo(worker_id: str, application_id: str, request: Request):
        return call(request, "POST", f"/v1/workspaces/{quote(worker_id, safe='')}/personal-library/{quote(application_id, safe='')}/undo", {}, worker_id)
