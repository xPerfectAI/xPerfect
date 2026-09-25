"""Authenticated transport for the canonical workspace file service."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, quote, urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .workspace_files import FileAdmissionError, WorkspaceFiles
from .workspace_file_exports import WorkspaceFileExport


class UploadRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(default="", max_length=4096)
    size_bytes: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


class BindRequest(BaseModel):
    upload_ids: list[str]
    revisions: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=128)
    directory: str | None = None


class FileMutationRequest(BaseModel):
    path: str | None = None
    revision: str = Field(min_length=1, max_length=128)


class FileRestoreRequest(BaseModel):
    undo_id: str = Field(min_length=1, max_length=128)


class DirectoryRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class FileExportRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)


class DraftMutationRequest(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    revision: str = Field(min_length=1, max_length=128)


class DraftExportRequest(BaseModel):
    upload_ids: list[str] = Field(min_length=1)
    revisions: list[str] | None = None


class StoragePolicyRequest(BaseModel):
    storage_limit_bytes: int | None = Field(default=None, ge=0)
    max_file_bytes: int | None = Field(default=None, ge=0)
    max_batch_files: int | None = Field(default=None, ge=0)
    max_batch_bytes: int | None = Field(default=None, ge=0)
    inherit: bool = False


def install_workspace_file_routes(
    app, files: WorkspaceFiles, auth_context, require_worker
) -> None:
    @app.exception_handler(FileAdmissionError)
    async def file_error(_request: Request, exc: FileAdmissionError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": str(exc)}},
        )

    def owner(request: Request, *, write: bool = False) -> tuple[str, str]:
        ctx = auth_context(request)
        worker_id = request.path_params.get("worker_id")
        if worker_id:
            require_worker(worker_id, request)
        elif ctx.auth_mode == "signed_link":
            raise HTTPException(
                status_code=403, detail="This view cannot access owner file drafts"
            )
        if write and (
            ctx.auth_mode == "signed_link" or str(ctx.role or "").lower() == "viewer"
        ):
            raise HTTPException(
                status_code=403, detail="File changes require workspace write access"
            )
        if write and ctx.auth_mode == "signed_internal_assertion" and "workspaces:write" not in ctx.scopes:
            raise HTTPException(status_code=403, detail="Workspace write scope is required")
        if ctx.enterprise and not ctx.owner_id:
            raise HTTPException(status_code=403, detail="File owner is unavailable")
        return (
            ctx.tenant_id or "local",
            ctx.owner_id
            or str(os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID") or "demo-owner"),
        )

    @app.get("/v1/storage")
    def storage(request: Request):
        tenant, user = owner(request)
        if auth_context(request).auth_mode == "signed_link":
            raise HTTPException(
                status_code=403, detail="Owner storage is unavailable to this view"
            )
        return files.storage(tenant, user)

    @app.patch("/v1/storage/policy")
    def policy(request: Request, payload: StoragePolicyRequest):
        tenant, user = owner(request, write=True)
        ctx = auth_context(request)
        administrator = (
            not ctx.enterprise or str(ctx.role or "").lower() == "tenant_admin"
        )
        values = payload.model_dump(exclude_unset=True, exclude={"inherit"})
        return files.set_policy(
            tenant, user, values, administrator=administrator, inherit=payload.inherit
        )

    @app.patch("/v1/storage/owners/{owner_id}/policy")
    def owner_policy(owner_id: str, request: Request, payload: StoragePolicyRequest):
        tenant, _ = owner(request, write=True)
        ctx = auth_context(request)
        if ctx.enterprise and str(ctx.role or "").lower() != "tenant_admin":
            raise HTTPException(
                status_code=403, detail="Tenant administrator role required"
            )
        values = payload.model_dump(exclude_unset=True, exclude={"inherit"})
        return files.set_policy(
            tenant, owner_id, values, administrator=True, inherit=payload.inherit
        )

    @app.get("/v1/storage/owners/{owner_id}")
    def owner_storage(owner_id: str, request: Request):
        tenant, _ = owner(request)
        ctx = auth_context(request)
        if ctx.enterprise and str(ctx.role or "").lower() != "tenant_admin":
            raise HTTPException(status_code=403, detail="Tenant administrator role required")
        return files.storage(tenant, owner_id)

    @app.get("/v1/file-uploads")
    def uploads(request: Request, draft_id: str):
        return files.list_uploads(*owner(request), draft_id)

    @app.post("/v1/file-uploads", status_code=201)
    def create_upload(request: Request, payload: UploadRequest):
        return files.create_upload(*owner(request, write=True), **payload.model_dump())

    def prepared_drafts(request, upload_ids, revisions):
        from .workspace_file_drafts import DraftExport
        return DraftExport(files, *owner(request), upload_ids, revisions)

    @app.post("/v1/file-uploads/export/prepare")
    def draft_export_prepare(request: Request, payload: DraftExportRequest):
        return prepared_drafts(request, payload.upload_ids, payload.revisions).descriptor()

    @app.get("/v1/file-uploads/export/prepare")
    def draft_export_prepare_get(request: Request, upload_ids: list[str] = Query(...), revisions: list[str] | None = Query(None)):
        return prepared_drafts(request, upload_ids, revisions).descriptor()

    def draft_export_response(prepared):
        return StreamingResponse(prepared.stream(), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="stored-files.zip"',
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/file-uploads/export")
    def draft_export(request: Request, upload_ids: list[str] = Query(...), revisions: list[str] | None = Query(None)):
        return draft_export_response(prepared_drafts(request, upload_ids, revisions))

    @app.post("/v1/file-uploads/export")
    async def draft_export_post(request: Request):
        owner(request)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            if content_type == "application/x-www-form-urlencoded":
                values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
            elif content_type == "application/json":
                values = await request.json()
            else:
                raise HTTPException(status_code=415, detail="Use a JSON selection or URL-encoded form")
            payload = DraftExportRequest.model_validate(values)
        except ValueError:
            raise HTTPException(status_code=422, detail="Stored file selection is invalid") from None
        return draft_export_response(prepared_drafts(request, payload.upload_ids, payload.revisions))

    @app.get("/v1/file-uploads/{upload_id}")
    def draft_metadata(upload_id: str, request: Request, revision: str | None = None):
        return files.get_upload(*owner(request), upload_id, revision=revision)

    @app.patch("/v1/file-uploads/{upload_id}")
    def draft_move(upload_id: str, request: Request, payload: DraftMutationRequest):
        return files.move_upload(*owner(request, write=True), upload_id, **payload.model_dump())

    @app.get("/v1/file-uploads/{upload_id}/content")
    def draft_download(upload_id: str, request: Request, revision: str | None = None):
        from starlette.background import BackgroundTask
        from .workspace_file_drafts import DraftDownload
        prepared = DraftDownload(files, *owner(request), upload_id, revision=revision)
        size = prepared.size
        start, end, status = 0, size - 1, 200
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(prepared.name, safe='')}",
            "Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff"}
        try:
            range_header = request.headers.get("range")
            if range_header:
                try:
                    unit, value = range_header.split("=", 1)
                    if unit != "bytes" or "," in value:
                        raise ValueError()
                    first, last = value.split("-", 1)
                    if first:
                        start = int(first)
                        end = min(size - 1, int(last)) if last else size - 1
                    else:
                        length = int(last)
                        if length <= 0:
                            raise ValueError()
                        start = max(0, size - length)
                    if start < 0 or start >= size or end < start:
                        raise ValueError()
                except ValueError:
                    raise HTTPException(status_code=416, detail="Requested file range is unavailable", headers={"Content-Range": f"bytes */{size}"}) from None
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(max(0, end - start + 1))
            return StreamingResponse(prepared.stream(start, end), status_code=status,
                media_type="application/octet-stream", headers=headers, background=BackgroundTask(prepared.close))
        except BaseException:
            prepared.close()
            raise

    @app.put("/v1/file-uploads/{upload_id}/content")
    async def receive_upload(upload_id: str, request: Request):
        return await files.receive(
            *owner(request, write=True), upload_id, request.stream()
        )

    @app.delete("/v1/file-uploads/{upload_id}")
    def cancel_upload(upload_id: str, request: Request):
        return files.cancel(*owner(request, write=True), upload_id)

    @app.post("/v1/workers/{worker_id}/files", status_code=201)
    def bind(worker_id: str, request: Request, payload: BindRequest):
        return files.bind(
            worker_id,
            *owner(request, write=True),
            payload.upload_ids,
            payload.idempotency_key,
            directory=payload.directory,
            revisions=payload.revisions or None,
        )

    @app.get("/v1/workers/{worker_id}/files")
    def listing(worker_id: str, request: Request, directory: str = "", cursor: int = 0):
        result = files.list_files(
            worker_id, *owner(request), directory=directory, cursor=cursor
        )
        ctx = auth_context(request)
        result["can_write"] = (
            result["can_write"]
            and ctx.auth_mode != "signed_link"
            and str(ctx.role or "").lower() != "viewer"
        )
        for item in result["items"]:
            if not item["is_dir"]:
                item["download_url"] = (
                    f"/v1/workers/{quote(worker_id, safe='')}/files/{quote(item['file_id'], safe='')}/content?"
                    + urlencode({"revision": item["revision"]})
                )
        return result

    @app.post("/v1/workers/{worker_id}/directories", status_code=201)
    def mkdir(worker_id: str, request: Request, payload: DirectoryRequest):
        return files.mkdir(worker_id, *owner(request, write=True), payload.path)

    def export_response(worker_id: str, request: Request, file_ids: list[str]):
        prepared = WorkspaceFileExport(files, worker_id, *owner(request), file_ids)
        return StreamingResponse(
            prepared.stream(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="workspace-files.zip"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/workers/{worker_id}/files/export")
    def export_get(worker_id: str, request: Request, file_ids: list[str] = Query(...)):
        return export_response(worker_id, request, file_ids)

    @app.post("/v1/workers/{worker_id}/files/export")
    def export_post(worker_id: str, request: Request, payload: FileExportRequest):
        return export_response(worker_id, request, payload.file_ids)

    @app.post("/v1/workers/{worker_id}/files/export/prepare")
    def export_prepare(worker_id: str, request: Request, payload: FileExportRequest):
        WorkspaceFileExport(files, worker_id, *owner(request), payload.file_ids)
        return {
            "method": "POST",
            "path": f"/v1/workers/{quote(worker_id, safe='')}/files/export",
            "body": {"file_ids": payload.file_ids},
            "content_type": "application/json",
            "media_type": "application/zip",
            "filename": "workspace-files.zip",
        }

    @app.get("/v1/workers/{worker_id}/files/export/prepare")
    def export_prepare_get(worker_id: str, request: Request, file_ids: list[str] = Query(...)):
        return export_prepare(worker_id, request, FileExportRequest(file_ids=file_ids))

    @app.get("/v1/workers/{worker_id}/files/trash")
    def trash(worker_id: str, request: Request):
        return files.trash(worker_id, *owner(request, write=True))

    @app.post("/v1/workers/{worker_id}/files/{file_id}/restore")
    def restore(
        worker_id: str, file_id: str, request: Request, payload: FileRestoreRequest
    ):
        return files.undo(
            worker_id, *owner(request, write=True), file_id, payload.undo_id
        )

    @app.patch("/v1/workers/{worker_id}/files/{file_id}")
    def move(
        worker_id: str, file_id: str, request: Request, payload: FileMutationRequest
    ):
        return files.mutate(
            worker_id,
            *owner(request, write=True),
            file_id,
            revision=payload.revision,
            path=payload.path,
        )

    @app.delete("/v1/workers/{worker_id}/files/{file_id}")
    def remove(
        worker_id: str, file_id: str, request: Request, payload: FileMutationRequest
    ):
        return files.mutate(
            worker_id,
            *owner(request, write=True),
            file_id,
            revision=payload.revision,
            delete=True,
        )

    @app.get("/v1/workers/{worker_id}/files/{file_id}")
    def file_metadata(worker_id: str, file_id: str, request: Request, revision: str | None = None):
        result = files.get_file(worker_id, *owner(request), file_id, revision=revision)
        if not result["is_dir"]:
            result["download_url"] = f"/v1/workers/{quote(worker_id, safe='')}/files/{quote(file_id, safe='')}/content?" + urlencode({"revision": result["revision"]})
        return result

    @app.get("/v1/workers/{worker_id}/files/{file_id}/content")
    def download(
        worker_id: str, file_id: str, request: Request, revision: str | None = None
    ):
        from starlette.background import BackgroundTask
        from .workspace_file_exports import CHUNK_BYTES, _identity

        descriptor, name, size = files.open_download(
            worker_id, *owner(request), file_id, revision=revision
        )
        closed = False

        def close():
            nonlocal closed
            if not closed:
                closed = True
                os.close(descriptor)

        def changed():
            return FileAdmissionError("File changed during download; refresh Files and retry", 409)

        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size != size:
                raise changed()
            identity = _identity(metadata)
        except BaseException:
            close()
            raise

        def check_identity():
            try:
                if _identity(os.fstat(descriptor)) != identity:
                    raise changed()
            except OSError:
                raise changed() from None

        start, end = 0, size - 1
        status = 200
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        range_header = request.headers.get("range")
        if range_header:
            try:
                unit, value = range_header.split("=", 1)
                if unit != "bytes" or "," in value:
                    raise ValueError()
                first, last = value.split("-", 1)
                if first:
                    start = int(first)
                    end = min(size - 1, int(last)) if last else size - 1
                else:
                    length = int(last)
                    if length <= 0:
                        raise ValueError()
                    start = max(0, size - length)
                if start < 0 or start >= size or end < start:
                    raise ValueError()
            except ValueError:
                close()
                raise HTTPException(
                    status_code=416,
                    detail="Requested file range is unavailable",
                    headers={"Content-Range": f"bytes */{size}"},
                )
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(max(0, end - start + 1))

        def body():
            remaining = max(0, end - start + 1)
            try:
                os.lseek(descriptor, start, os.SEEK_SET)
                check_identity()
                while remaining:
                    check_identity()
                    chunk = os.read(descriptor, min(CHUNK_BYTES, remaining))
                    check_identity()
                    if not chunk:
                        raise changed()
                    remaining -= len(chunk)
                    yield chunk
                check_identity()
            finally:
                close()

        return StreamingResponse(
            body(),
            status_code=status,
            headers=headers,
            media_type="application/octet-stream",
            background=BackgroundTask(close),
        )
