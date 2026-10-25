from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xperfect_evidence.client import Client, HTTPFailure
from xperfect_evidence.manifest import EvidenceError, Limits, canonical, sha


@contextmanager
def server():
    saved = {"assignments": {}, "calls": 0, "uploads": {}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def reply(self, status, body):
            data = canonical(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path == "/v1/redirect":
                self.send_response(302)
                self.send_header("Location", "/v1/large")
                self.end_headers()
            elif self.path == "/v1/large":
                self.reply(200, {"value": "x" * 3000})
            elif "/assignments/by-idempotency/" in self.path:
                key = self.path.rsplit("/", 1)[-1]
                self.reply(200, saved["assignments"][key]) if key in saved["assignments"] else self.reply(404, {})
            elif self.path == "/v1/file-uploads/file-1":
                self.reply(200, saved["uploads"].get("file-1", {}))
            else:
                self.reply(404, {})
        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/assign"):
                key = self.headers["Idempotency-Key"]
                if key in saved["assignments"] and saved["assignments"][key]["instruction"] != value["instruction"]:
                    self.reply(409, {})
                    return
                if key not in saved["assignments"]:
                    saved["calls"] += 1
                    saved["assignments"][key] = {"run_id": "run-" + str(saved["calls"]), "state": "queued", **value}
                self.reply(saved.get("post_status", 202), saved["assignments"][key])
            elif self.path == "/v1/file-uploads":
                saved["uploads"]["file-1"] = {"upload_id": "file-1", "state": "pending", **value}
                self.reply(201, saved["uploads"]["file-1"])
            else:
                self.reply(404, {})
        def do_PUT(self):
            content = self.rfile.read(int(self.headers["Content-Length"]))
            saved["uploads"]["file-1"].update(state="ready", sha256=sha(content), revision="server-revision")
            self.reply(200, saved["uploads"]["file-1"])
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}", saved
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=3)


def test_assignments_use_runtime_idempotency_and_conflict():
    with server() as (url, saved):
        runtime = Client(url)
        first = runtime.assign("worker", "original task", "high", "key")
        assert runtime.assign("worker", "original task", "high", "key") == first
        assert saved["calls"] == 1
        with pytest.raises(HTTPFailure) as error:
            runtime.assign("worker", "changed task", "high", "key")
        assert error.value.status == 409


def test_streamed_upload_checks_exact_server_revision_and_digest(sample):
    with server() as (url, saved):
        result = Client(url).upload(root=sample["source"], path="original.txt", item=sample["item"], key="upload-key", draft_id="draft")
        assert result["revision"] == "server-revision"
        assert result["sha256"] == sample["item"]["sha256"]


def test_server_error_after_acceptance_recovers_same_identity():
    with server() as (url, saved):
        saved["post_status"] = 503
        result = Client(url).assign("worker", "original task", "high", "key")
        assert result["run_id"] == "run-1" and saved["calls"] == 1


def test_no_redirect_or_unbounded_response():
    with server() as (url, _saved):
        runtime = Client(url, limits=Limits(manifest_bytes=512))
        with pytest.raises(HTTPFailure) as error:
            runtime.request("GET", "/v1/redirect")
        assert error.value.status == 302
        with pytest.raises(EvidenceError):
            runtime.request("GET", "/v1/large")


@pytest.mark.parametrize("url", ["http://remote.example", "https://name:secret@example.test", "https://example.test/path", "https://example.test/?token=secret", "file:///tmp/example", "http://127.0.0.1:0", "https://example.test:invalid", "https://[invalid"])
def test_unsafe_service_origins_are_rejected(url):
    with pytest.raises(EvidenceError):
        Client(url)
