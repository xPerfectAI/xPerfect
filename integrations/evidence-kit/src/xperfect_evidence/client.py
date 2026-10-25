"""Small bounded client for existing xPerfect HTTP operations."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .manifest import EvidenceError, Limits, canonical, loads


class HTTPFailure(EvidenceError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Runtime request rejected ({status})")


class UncertainRequest(EvidenceError):
    """The reply is unknown. Reconcile identity before repeating a write."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def segment(value):
    return urllib.parse.quote(str(value), safe="")


class Client:
    def __init__(self, base_url, *, token="", limits=Limits(), timeout=20):
        try:
            parsed = urllib.parse.urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise EvidenceError("Invalid runtime origin") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or port == 0):
            raise EvidenceError("Use a runtime origin without userinfo, path, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise EvidenceError("Remote runtime connections require HTTPS")
        if not 0 < timeout <= limits.seconds:
            raise EvidenceError("Request timeout is outside command bounds")
        self.base_url, self.token, self.limits, self.timeout = base_url.rstrip("/"), token, limits, timeout
        if not isinstance(token, str) or len(token) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise EvidenceError("Invalid access token")
        self.deadline = time.monotonic() + limits.seconds
        self.opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))

    def request(self, method, path, *, value=None, data=None, headers=None, json_result=True):
        if not path.startswith("/v1/") or path.startswith("//"):
            raise EvidenceError("Only existing runtime API paths are admitted")
        outgoing = {"Accept": "application/json"}
        if self.token:
            outgoing["Authorization"] = "Bearer " + self.token
        if value is not None:
            data = canonical(value)
            if len(data) > self.limits.manifest_bytes:
                raise EvidenceError("Request exceeds byte bound")
            outgoing["Content-Type"] = "application/json"
        outgoing.update(headers or {})
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=outgoing)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise UncertainRequest("Command deadline expired; reconcile accepted work")
        try:
            with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                limit = self.limits.manifest_bytes if json_result else self.limits.file_bytes
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= self.deadline:
                        raise UncertainRequest("Response exceeded command deadline")
                    chunk = response.read1(min(64 * 1024, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise EvidenceError("Runtime response exceeds byte bound")
                body = b"".join(chunks)
                if len(body) > limit:
                    raise EvidenceError("Runtime response exceeds byte bound")
                return loads(body, self.limits) if json_result else body
        except urllib.error.HTTPError as exc:
            raise HTTPFailure(exc.code) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            raise UncertainRequest("Runtime reply unavailable; reconcile the same identity") from None

    def worker(self, worker_id):
        return self.request("GET", f"/v1/workers/{segment(worker_id)}")

    def run(self, run_id):
        return self.request("GET", f"/v1/runs/{segment(run_id)}")

    def assignment(self, worker_id, key):
        try:
            return self.request("GET", f"/v1/workers/{segment(worker_id)}/assignments/by-idempotency/{segment(key)}")
        except HTTPFailure as exc:
            if exc.status == 404:
                return None
            if exc.status >= 500:
                raise UncertainRequest("Assignment lookup unavailable") from None
            raise

    def assign(self, worker_id, task, effort, key):
        existing = self.assignment(worker_id, key)
        # Re-submit an existing key so the runtime verifies its full request digest.
        try:
            result = self.request("POST", f"/v1/workers/{segment(worker_id)}/assign",
                                  value={"instruction": task, "effort": effort},
                                  headers={"Idempotency-Key": key})
        except (UncertainRequest, HTTPFailure) as exc:
            if isinstance(exc, HTTPFailure) and exc.status < 500:
                raise
            try:
                result = self.assignment(worker_id, key)
            except EvidenceError:
                raise UncertainRequest("Assignment acceptance cannot yet be read back") from None
            if result is None:
                raise UncertainRequest("Assignment acceptance is unknown; recover the same key") from None
            if result.get("instruction") != task or result.get("effort", "") != effort:
                raise EvidenceError("Recovered assignment differs from the submitted request")
        if existing is not None and existing["run_id"] != result["run_id"]:
            raise EvidenceError("Assignment identity changed")
        return result

    def upload(self, *, root, path, item, key, draft_id):
        metadata = self.request("POST", "/v1/file-uploads", value={
            "draft_id": draft_id, "name": item["package_path"].split("/")[-1],
            "relative_path": item["package_path"], "size_bytes": item["size_bytes"], "idempotency_key": key,
        })
        upload_id = metadata["upload_id"]
        if metadata["state"] != "ready":
            root.snapshot(path, expected=item)
            with root.reader(path) as (handle, _info):
                self.request("PUT", f"/v1/file-uploads/{segment(upload_id)}/content", data=handle,
                             headers={"Content-Length": str(item["size_bytes"]), "Content-Type": "application/octet-stream"})
        metadata = self.request("GET", f"/v1/file-uploads/{segment(upload_id)}")
        if (metadata.get("state") != "ready" or metadata.get("sha256") != item["sha256"] or metadata.get("size_bytes") != item["size_bytes"]
                or metadata.get("relative_path") != item["package_path"] or metadata.get("name") != item["package_path"].split("/")[-1]):
            raise EvidenceError("Admitted upload bytes differ")
        # The server owns the exact revision; never manufacture it from a content hash.
        if not metadata.get("revision"):
            raise EvidenceError("Runtime did not return the exact upload revision")
        return {k: metadata[k] for k in ("upload_id", "revision", "sha256", "size_bytes")}

    def bind(self, worker_id, uploads, key):
        return self.request("POST", f"/v1/workers/{segment(worker_id)}/files", value={
            "upload_ids": [i["upload_id"] for i in uploads], "revisions": [i["revision"] for i in uploads],
            "idempotency_key": key,
        })

    def verify_callback(self, association):
        result = self.request("POST", "/v1/callback-associations/verify", value=association)
        if (result.get("valid") is not True or result.get("runId") != association["run_id"]
                or result.get("currentRunId") != association["run_id"]):
            raise EvidenceError("Callback is not bound to the current run")
        return result
