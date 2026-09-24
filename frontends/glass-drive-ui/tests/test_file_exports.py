from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx

from glass_drive_ui.file_routes import install_file_routes


def test_export_proxy_keeps_cookie_scope_streaming_and_allows_read_only():
    observed = {"closed": 0, "chunks": 0, "requests": []}

    class Response:
        status_code = 200
        headers = {
            "content-disposition": 'attachment; filename="workspace-files.zip"',
            "cache-control": "private, no-store",
            "authorization": "must-not-forward",
        }

        async def aiter_bytes(self):
            for chunk in (b"zip", b"bytes"):
                observed["chunks"] += 1
                yield chunk

    class Runtime:
        @asynccontextmanager
        async def stream_file_download(self, path, headers, **kwargs):
            observed["requests"].append((path, headers, kwargs))
            try:
                yield Response()
            finally:
                observed["closed"] += 1

    def client_for_request(request, worker_id):
        assert request.cookies.get("session") == "test-session"
        assert worker_id == "worker"
        return Runtime()

    app = FastAPI()
    install_file_routes(
        app,
        client_for_request,
        lambda *_: {"role": "viewer"},
        lambda _: True,
        lambda exc, _: HTTPException(status_code=exc.response.status_code),
    )
    client = TestClient(app, cookies={"session": "test-session"})
    response = client.get(
        "/api/workspace/worker/files/export",
        params=[("file_ids", "first"), ("file_ids", "second")],
    )
    assert response.status_code == 200
    assert response.content == b"zipbytes"
    assert response.headers["content-type"] == "application/zip"
    assert "authorization" not in response.headers
    path, headers, kwargs = observed["requests"][0]
    assert parse_qs(urlsplit(path).query) == {"file_ids": ["first", "second"]}
    assert headers == kwargs == {}
    assert observed["closed"] == 1
    response = client.post(
        "/api/workspace/worker/files/export", json={"file_ids": ["first", "second"]}
    )
    assert response.status_code == 200
    assert observed["requests"][1][2] == {
        "method": "POST",
        "payload": {"file_ids": ["first", "second"]},
    }
    assert observed["closed"] == 2
    assert observed["chunks"] == 4
    assert (
        client.post(
            "/api/workspace/worker/files/export", json={"file_ids": []}
        ).status_code
        == 422
    )


def test_export_proxy_preserves_preflight_conflict():
    class Runtime:
        @asynccontextmanager
        async def stream_file_download(self, path, headers):
            response = httpx.Response(
                409, request=httpx.Request("GET", "https://runtime.invalid" + path)
            )
            raise httpx.HTTPStatusError(
                "changed", request=response.request, response=response
            )
            yield  # pragma: no cover

    app = FastAPI()
    install_file_routes(
        app,
        lambda *_: Runtime(),
        lambda *_: {},
        lambda _: False,
        lambda exc, _: HTTPException(
            status_code=exc.response.status_code, detail="Selection changed"
        ),
    )
    response = TestClient(app).get(
        "/api/workspace/worker/files/export", params={"file_ids": "first"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Selection changed"
