"""Owner-authenticated transport over the existing Library/Files owners."""
from __future__ import annotations

import os
from fastapi import Body, HTTPException, Request


def install_personal_library_routes(app, service, auth_context, require_worker):
    def flag(payload, name, default):
        value = payload.get(name, default)
        if type(value) is not bool:
            raise HTTPException(422, detail={"code": "library_boolean_invalid", "message": f"{name} must be true or false"})
        return value

    def owner(request, worker_id=None):
        ctx = auth_context(request)
        if ctx.auth_mode == "signed_link" or str(ctx.role or "").lower() == "viewer":
            raise HTTPException(403, detail={"code": "library_owner_required", "message": "Sign in with workspace management access to use My Library"})
        if ctx.auth_mode == "signed_internal_assertion" and "workspaces:write" not in ctx.scopes:
            raise HTTPException(403, detail={"code": "library_owner_required", "message": "Workspace management scope is required"})
        if worker_id:
            require_worker(worker_id, request, heal=False)
        tenant = ctx.tenant_id or "local"
        user = ctx.owner_id or str(os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID") or "demo-owner")
        if ctx.enterprise and not ctx.owner_id:
            raise HTTPException(403, detail="Library owner is unavailable")
        return tenant, user

    @app.get("/v1/personal-library")
    def listing(request: Request, include_archived: bool = False):
        return service.personal_library.list_items(*owner(request), include_archived=include_archived)

    @app.post("/v1/personal-library", status_code=201)
    def create(request: Request, payload: dict = Body(...)):
        return service.personal_library.create_item(*owner(request), payload, str(payload.get("idempotency_key") or ""))

    # Literal routes precede item IDs.
    @app.post("/v1/personal-library/resolve")
    def resolve(request: Request, payload: dict = Body(...)):
        return service.personal_library.resolve_refs(*owner(request), payload.get("refs", []))

    @app.get("/v1/personal-library/{item_id}")
    def read(item_id: str, request: Request, version_id: str | None = None):
        return service.personal_library.get_item(*owner(request), item_id, version_id=version_id)

    @app.patch("/v1/personal-library/{item_id}")
    def revise(item_id: str, request: Request, payload: dict = Body(...)):
        return service.personal_library.revise_item(*owner(request), item_id, payload, str(payload.get("expected_version_id") or ""))

    @app.post("/v1/personal-library/{item_id}/archive")
    def archive(item_id: str, request: Request, payload: dict = Body(...)):
        return service.personal_library.archive_item(*owner(request), item_id, flag(payload, "archived", True), str(payload.get("expected_version_id") or ""))

    @app.delete("/v1/personal-library/{item_id}")
    def delete(item_id: str, request: Request, confirm: bool = False):
        return service.personal_library.delete_item(*owner(request), item_id, confirm=confirm)

    @app.get("/v1/workspaces/{worker_id}/personal-library")
    def worker_listing(worker_id: str, request: Request):
        # The Library owner performs its scoped read-only lookup, including closed work.
        return service.personal_library.list_worker_items(*owner(request), worker_id)

    @app.post("/v1/workspaces/{worker_id}/personal-library/capture", status_code=201)
    def capture(worker_id: str, request: Request, payload: dict = Body(...)):
        return service.personal_library.capture_file(*owner(request), worker_id,
            str(payload.get("file_id") or ""), str(payload.get("revision") or ""),
            str(payload.get("name") or "").strip() or None, str(payload.get("idempotency_key") or ""))

    @app.post("/v1/workspaces/{worker_id}/personal-library")
    def apply(worker_id: str, request: Request, payload: dict = Body(...)):
        return service.personal_library.apply_to_worker(*owner(request, worker_id), worker_id,
            payload.get("refs", []), str(payload.get("request_key") or ""),
            confirm_native=flag(payload, "confirm_native", False))

    @app.delete("/v1/workspaces/{worker_id}/personal-library/{application_id}")
    def remove(worker_id: str, application_id: str, request: Request):
        return service.personal_library.remove_from_worker(*owner(request, worker_id), worker_id, application_id)

    @app.post("/v1/workspaces/{worker_id}/personal-library/{application_id}/undo")
    def undo(worker_id: str, application_id: str, request: Request):
        return service.personal_library.restore_to_worker(*owner(request, worker_id), worker_id, application_id)
