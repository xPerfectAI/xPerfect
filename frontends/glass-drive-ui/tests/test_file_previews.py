"""The Files reader keeps revision, cookie authority and streaming headers."""
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx

from glass_drive_ui.file_routes import install_file_routes


def test_preview_proxy_streams_security_headers_and_projects_only_its_origin():
    observed = {"closed": 0, "requests": []}

    class Runtime:
        @asynccontextmanager
        async def stream_file_download(self, path, headers):
            observed["requests"].append((path, headers))
            try:
                yield httpx.Response(206, content=b"file bytes", headers={
                    "content-type": "application/pdf", "content-disposition": "inline",
                    "content-range": "bytes 0-9/20", "accept-ranges": "bytes",
                    "content-security-policy": "default-src 'none'; frame-ancestors 'self'",
                    "referrer-policy": "no-referrer", "x-frame-options": "SAMEORIGIN",
                    "cache-control": "private, no-store", "x-content-type-options": "nosniff",
                    "authorization": "must-not-forward",
                })
            finally:
                observed["closed"] += 1

    def client_for(request, worker_id):
        assert request.state.file_cookie_only
        assert request.cookies.get("session") == "synthetic-session"
        assert worker_id == "worker"
        return Runtime()

    app = FastAPI()
    install_file_routes(app, client_for, lambda *_: {"role": "viewer"},
                        lambda _: True, lambda exc, _: HTTPException(status_code=exc.response.status_code))
    client = TestClient(app, base_url="http://localhost:39980", cookies={
        "session": "synthetic-session", "xperfect_appearance_39880": "light",
        "xperfect_appearance_39980": "dark",
    })
    url = "/api/workspace/worker/files/file/preview"
    assert client.get(url).status_code == 422
    response = client.get(url, params={"revision": "exact", "inline": "1", "path": "not-authorized"},
                          headers={"Range": "bytes=0-9", "x-xperfect-appearance": "light"})
    assert response.status_code == 206 and response.content == b"file bytes"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "authorization" not in response.headers
    path, headers = observed["requests"][-1]
    assert urlsplit(path).path == "/v1/workers/worker/files/file/preview"
    assert parse_qs(urlsplit(path).query) == {"revision": ["exact"], "inline": ["1"]}
    assert headers == {"Range": "bytes=0-9", "x-xperfect-appearance": "dark", "x-xperfect-file-ui": "1"}
    assert observed["closed"] == 1
    client.cookies.set("xperfect_appearance_39980", "invalid")
    client.get(url, params={"revision": "exact"})
    assert observed["requests"][-1][1]["x-xperfect-appearance"] == "system"
    client.get("/api/workspace/worker/files/file/content", params={"revision": "exact", "inline": "1"})
    assert observed["requests"][-1] == ("/v1/workers/worker/files/file/content?revision=exact", {})
