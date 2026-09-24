"""Cookie-authenticated UI proxy; file bytes stay streamed in both directions."""

from urllib.parse import parse_qs, quote, urlencode

import httpx
from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


def install_file_routes(
    app, client_for_request, request_identity, restricted_identity, runtime_error
):
    def client(request, worker_id=None, write=False):
        # These routes accept the normal session or a scoped worker cookie only.
        # Legacy signed-link URLs remain valid on their existing non-file routes.
        request.state.file_cookie_only = True
        identity = request_identity(request, worker_id)
        if worker_id is None and identity.get("auth_source") == "signed_link":
            raise HTTPException(
                status_code=403,
                detail="This workspace view cannot access owner uploads or storage",
            )
        if write and restricted_identity(identity):
            raise HTTPException(
                status_code=403, detail="File changes require workspace write access"
            )
        if write and request.method in {"GET", "HEAD"}:
            return client_for_request(request, worker_id, file_write_scope=True)
        return client_for_request(request, worker_id)

    def call(request, method, path, payload=None, worker_id=None, *, write=False):
        active = client(request, worker_id, write or method not in {"GET", "HEAD"})
        try:
            return active.file_request(method, path, payload)
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "File operation failed") from exc

    @app.get("/api/storage")
    def storage(request: Request):
        return call(request, "GET", "/v1/storage")

    @app.get("/api/file-uploads")
    def uploads(request: Request, draft_id: str):
        return call(
            request, "GET", "/v1/file-uploads?" + urlencode({"draft_id": draft_id})
        )

    @app.post("/api/file-uploads", status_code=201)
    def create_upload(request: Request, payload: dict = Body(...)):
        return call(request, "POST", "/v1/file-uploads", payload)

    @app.put("/api/file-uploads/{upload_id}/content")
    async def content(upload_id: str, request: Request):
        active = client(request, write=True)
        try:
            return await active.upload_file_content(upload_id, request.stream())
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "File transfer failed") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail="File transfer was interrupted; retry the same file",
            ) from exc

    @app.delete("/api/file-uploads/{upload_id}")
    def cancel(upload_id: str, request: Request):
        return call(request, "DELETE", f"/v1/file-uploads/{quote(upload_id, safe='')}")

    @app.post("/api/file-uploads/export/prepare")
    def draft_export_prepare(request: Request, payload: dict = Body(...)):
        active = client(request)
        try:
            return active.file_request("POST", "/v1/file-uploads/export/prepare", payload)
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "Draft export could not be prepared") from exc

    async def draft_export_stream(request, upload_ids, revisions, *, method="GET"):
        if not upload_ids or len(upload_ids) != len(revisions):
            raise HTTPException(status_code=422, detail="Each selected file needs a revision")
        active = client(request)
        path = "/v1/file-uploads/export"
        if method == "GET":
            path += "?" + urlencode(
                {"upload_ids": upload_ids, "revisions": revisions}, doseq=True
            )
        context = active.stream_file_download(
            path, {}, method=method,
            payload={"upload_ids": upload_ids, "revisions": revisions} if method == "POST" else None,
        )
        try:
            response = await context.__aenter__()
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "Draft export failed") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail="Draft export is temporarily unavailable"
            ) from exc
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {"content-disposition", "cache-control", "x-content-type-options"}
        }
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            media_type="application/zip",
            headers=headers,
            background=BackgroundTask(context.__aexit__, None, None, None),
        )

    @app.get("/api/file-uploads/export")
    async def draft_export(
        request: Request,
        upload_ids: list[str] = Query(...),
        revisions: list[str] = Query(...),
    ):
        return await draft_export_stream(request, upload_ids, revisions)

    @app.post("/api/file-uploads/export")
    async def draft_export_form(request: Request):
        if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
            raise HTTPException(status_code=415, detail="Form encoding is required")
        try:
            form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="Invalid form encoding") from exc
        return await draft_export_stream(
            request, form.get("upload_ids", []), form.get("revisions", []), method="POST"
        )

    @app.get("/api/file-uploads/{upload_id}")
    def upload_receipt(upload_id: str, request: Request):
        return call(request, "GET", f"/v1/file-uploads/{quote(upload_id, safe='')}")

    @app.patch("/api/file-uploads/{upload_id}")
    def update_upload(upload_id: str, request: Request, payload: dict = Body(...)):
        return call(
            request, "PATCH", f"/v1/file-uploads/{quote(upload_id, safe='')}", payload
        )

    @app.get("/api/file-uploads/{upload_id}/content")
    async def download_upload(upload_id: str, request: Request, revision: str):
        active = client(request)
        path = (
            f"/v1/file-uploads/{quote(upload_id, safe='')}/content?"
            + urlencode({"revision": revision})
        )
        headers = {"Range": request.headers["range"]} if "range" in request.headers else {}
        context = active.stream_file_download(path, headers)
        try:
            response = await context.__aenter__()
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "Draft download failed") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail="Draft download is temporarily unavailable"
            ) from exc
        forwarded = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "content-length", "content-disposition", "content-range",
                "accept-ranges", "cache-control", "x-content-type-options",
            }
        }
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            media_type="application/octet-stream",
            headers=forwarded,
            background=BackgroundTask(context.__aexit__, None, None, None),
        )

    @app.get("/api/workspace/{worker_id}/files")
    def listing(worker_id: str, request: Request, directory: str = "", cursor: int = 0):
        result = call(
            request,
            "GET",
            f"/v1/workers/{quote(worker_id, safe='')}/files?"
            + urlencode({"directory": directory, "cursor": cursor}),
            worker_id=worker_id,
        )
        for item in result.get("items", []):
            if not item.get("is_dir"):
                item["download_url"] = (
                    f"/api/workspace/{quote(worker_id, safe='')}/files/{quote(item['file_id'], safe='')}/content?"
                    + urlencode({"revision": item["revision"]})
                )
        return result

    @app.post("/api/workspace/{worker_id}/files", status_code=201)
    def bind(worker_id: str, request: Request, payload: dict = Body(...)):
        return call(
            request,
            "POST",
            f"/v1/workers/{quote(worker_id, safe='')}/files",
            payload,
            worker_id,
        )

    @app.post("/api/workspace/{worker_id}/directories", status_code=201)
    def mkdir(worker_id: str, request: Request, payload: dict = Body(...)):
        return call(
            request,
            "POST",
            f"/v1/workers/{quote(worker_id, safe='')}/directories",
            payload,
            worker_id,
        )

    @app.api_route(
        "/api/workspace/{worker_id}/files/{file_id}", methods=["PATCH", "DELETE"]
    )
    def mutate(
        worker_id: str, file_id: str, request: Request, payload: dict = Body(...)
    ):
        return call(
            request,
            request.method,
            f"/v1/workers/{quote(worker_id, safe='')}/files/{quote(file_id, safe='')}",
            payload,
            worker_id,
        )

    async def export_response(worker_id, request, file_ids, *, method="GET"):
        active = client(request, worker_id)
        path = f"/v1/workers/{quote(worker_id, safe='')}/files/export"
        if method == "GET":
            path += "?" + urlencode({"file_ids": file_ids}, doseq=True)
            context = active.stream_file_download(path, {})
        else:
            context = active.stream_file_download(
                path, {}, method="POST", payload={"file_ids": file_ids}
            )
        try:
            response = await context.__aenter__()
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "File export failed") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail="File export is temporarily unavailable"
            ) from exc
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {"content-disposition", "cache-control", "x-content-type-options"}
        }
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            media_type="application/zip",
            headers=headers,
            background=BackgroundTask(context.__aexit__, None, None, None),
        )

    @app.get("/api/workspace/{worker_id}/files/export")
    async def export_get(
        worker_id: str, request: Request, file_ids: list[str] = Query(...)
    ):
        return await export_response(worker_id, request, file_ids)

    @app.post("/api/workspace/{worker_id}/files/export")
    async def export_post(worker_id: str, request: Request):
        content_type = request.headers.get("content-type", "").lower().split(";", 1)[0]
        if content_type == "application/x-www-form-urlencoded":
            try:
                payload = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=422, detail="Invalid form encoding") from exc
        elif content_type == "application/json":
            try:
                payload = await request.json()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Invalid export request") from exc
        else:
            raise HTTPException(status_code=415, detail="JSON or form encoding is required")
        file_ids = payload.get("file_ids") if isinstance(payload, dict) else None
        if (
            not isinstance(file_ids, list)
            or not file_ids
            or not all(isinstance(value, str) for value in file_ids)
        ):
            raise HTTPException(
                status_code=422, detail="Select at least one file or folder"
            )
        return await export_response(worker_id, request, file_ids, method="POST")

    @app.get("/api/workspace/{worker_id}/files/trash")
    def trash(worker_id: str, request: Request):
        return call(
            request,
            "GET",
            f"/v1/workers/{quote(worker_id, safe='')}/files/trash",
            worker_id=worker_id,
            write=True,
        )

    @app.post("/api/workspace/{worker_id}/files/{file_id}/restore")
    def restore(
        worker_id: str, file_id: str, request: Request, payload: dict = Body(...)
    ):
        return call(
            request,
            "POST",
            f"/v1/workers/{quote(worker_id, safe='')}/files/{quote(file_id, safe='')}/restore",
            payload,
            worker_id,
        )

    @app.get("/api/workspace/{worker_id}/files/{file_id}/content")
    async def download(
        worker_id: str, file_id: str, request: Request, revision: str | None = None
    ):
        active = client(request, worker_id)
        headers = (
            {"Range": request.headers["range"]} if "range" in request.headers else {}
        )
        path = f"/v1/workers/{quote(worker_id, safe='')}/files/{quote(file_id, safe='')}/content"
        if revision is not None:
            path += "?" + urlencode({"revision": revision})
        context = active.stream_file_download(path, headers)
        try:
            response = await context.__aenter__()
        except httpx.HTTPStatusError as exc:
            raise runtime_error(exc, "File download failed") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail="File download is temporarily unavailable"
            ) from exc
        forwarded = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "content-length",
                "content-disposition",
                "content-range",
                "accept-ranges",
                "cache-control",
                "x-content-type-options",
            }
        }
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            media_type="application/octet-stream",
            headers=forwarded,
            background=BackgroundTask(context.__aexit__, None, None, None),
        )
