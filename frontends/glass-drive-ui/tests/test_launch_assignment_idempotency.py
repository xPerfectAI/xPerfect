"""Operator assignment recovery consumes the existing exact native run receipt."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import glass_drive_ui.runtime_client as runtime_client_module
from glass_drive_ui.runtime_client import RuntimeClient
from glass_drive_ui.server import create_app
from test_launch_worker_selection import ExistingWorkspaceClient
from test_server import FakeRuntimeClient, clear_glasshive_ui_env  # noqa: F401


def test_assignment_key_and_receipt_keep_scope_and_do_not_leak_to_another_request(monkeypatch):
    requests = []
    assertion_number = 0

    def headers():
        nonlocal assertion_number
        assertion_number += 1
        return {"X-GlassHive-User-Assertion": f"synthetic-assertion-{assertion_number}"}

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"run_id": "run_one"})

    original_client = httpx.Client
    monkeypatch.setattr(
        runtime_client_module.httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    runtime = RuntimeClient(
        "https://runtime.invalid", headers={"X-WPR-Token": "synthetic-service-token"},
        headers_factory=headers,
    )
    runtime.assign_run("wrk_one", "Keep the exact job", idempotency_key="launch/key")
    runtime.assignment_by_idempotency("wrk_one", "launch/key")
    runtime.assign_run("wrk_two", "A separate job")

    assert [request.method for request in requests] == ["POST", "GET", "POST"]
    assert requests[0].headers["x-glasshive-idempotency-key"] == "launch/key"
    assert requests[1].url.raw_path == b"/v1/workers/wrk_one/assignments/by-idempotency/launch%2Fkey"
    assert "x-glasshive-idempotency-key" not in requests[1].headers
    assert "x-glasshive-idempotency-key" not in requests[2].headers
    assert [request.headers["x-glasshive-user-assertion"] for request in requests] == [
        "synthetic-assertion-1", "synthetic-assertion-2", "synthetic-assertion-3",
    ]
    assert all(request.headers["x-wpr-token"] == "synthetic-service-token" for request in requests)


@pytest.mark.parametrize("selection,placement,worker_id", [
    ("new:codex-cli", {}, "wrk_new"),
    ("open:wrk_1", {}, "wrk_1"),
    ("existing:wrk_1", {}, "wrk_1"),
    ("duplicate:wrk_1", {}, "wrk_dup"),
    ("new:codex-cli", {"execution_workspace_id": "wsp_existing"}, "wrk_shared"),
])
def test_launch_forwards_the_same_exact_key_to_native_assignment(selection, placement, worker_id):
    runtime = ExistingWorkspaceClient()
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Inspect the supplied files", "context": "Keep the original bytes",
        "workspace_option": selection, "idempotency_key": "exact-launch-key", **placement,
    })
    assert response.status_code == 200, response.text
    assert runtime.assign_requests == [{
        "worker_id": worker_id,
        "instruction": "Inspect the supplied files\n\nBackground:\nKeep the original bytes",
        "idempotency_key": "exact-launch-key",
    }]


class LostAssignmentReply(FakeRuntimeClient):
    def __init__(self, *, failure="transport", receipt="ready"):
        super().__init__()
        self.failure = failure
        self.receipt = receipt
        self.receipt_reads = []
        self.accepted_run = None

    def assign_run(self, worker_id, instruction, *, idempotency_key=None):
        super().assign_run(worker_id, instruction, idempotency_key=idempotency_key)
        self.accepted_run = {
            "run_id": "run_durable", "worker_id": worker_id, "instruction": instruction,
            "state": "queued",
        }
        request = httpx.Request("POST", f"https://runtime.invalid/v1/workers/{worker_id}/assign")
        if self.failure == "transport":
            raise httpx.ReadTimeout("Synthetic lost reply", request=request)
        if self.failure == "invalid_json":
            raise json.JSONDecodeError("Synthetic malformed reply", "", 0)
        response = httpx.Response(int(self.failure), request=request)
        raise httpx.HTTPStatusError("Synthetic reply failure", request=request, response=response)

    def assignment_by_idempotency(self, worker_id, idempotency_key):
        self.receipt_reads.append((worker_id, idempotency_key))
        if self.receipt in {"404", "503"}:
            request = httpx.Request("GET", "https://runtime.invalid/assignment")
            response = httpx.Response(int(self.receipt), request=request)
            raise httpx.HTTPStatusError("Synthetic readback failure", request=request, response=response)
        if self.receipt == "foreign_worker":
            return {**self.accepted_run, "worker_id": "wrk_other"}
        if self.receipt == "other_instruction":
            return {**self.accepted_run, "instruction": "Another request"}
        return dict(self.accepted_run)


@pytest.mark.parametrize("failure", ["transport", "invalid_json", "500", "503"])
def test_lost_assignment_reply_reads_exact_saved_run_without_reassigning_or_failing(failure):
    runtime = LostAssignmentReply(failure=failure)
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Run once", "workspace_option": "new:codex-cli",
        "idempotency_key": "lost-reply-key",
    })
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "run_durable"
    assert response.json()["worker_id"] == "wrk_new"
    assert runtime.receipt_reads == [("wrk_new", "lost-reply-key")]
    assert len(runtime.assign_requests) == 1
    assert runtime.launch_failures == []
    assert runtime.accepted_run["state"] == "queued"


@pytest.mark.parametrize("receipt", ["404", "503", "foreign_worker", "other_instruction"])
def test_missing_or_mismatched_readback_preserves_unknown_without_failure_mutation(receipt):
    runtime = LostAssignmentReply(receipt=receipt)
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Run once", "workspace_option": "new:codex-cli",
        "idempotency_key": "unknown-reply-key",
    })
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "launch_outcome_unknown"
    assert response.json()["detail"]["worker_id"] == "wrk_new"
    assert response.json()["detail"]["watch_url"].startswith("/watch/wrk_new?")
    assert len(runtime.assign_requests) == 1
    assert runtime.launch_failures == []
    assert runtime.accepted_run["state"] == "queued"


def test_unkeyed_lost_assignment_reply_cannot_be_called_a_failure_or_replayed():
    runtime = LostAssignmentReply()
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Run once", "workspace_option": "new:codex-cli",
    })
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "launch_outcome_unknown"
    assert runtime.receipt_reads == []
    assert len(runtime.assign_requests) == 1
    assert runtime.launch_failures == []


def test_confirmed_assignment_rejection_retains_the_native_status():
    runtime = LostAssignmentReply(failure="409")
    response = TestClient(create_app(runtime_client=runtime)).post("/api/launch", json={
        "description": "Use an existing worker", "workspace_option": "open:wrk_1",
        "idempotency_key": "confirmed-rejection-key",
    })
    assert response.status_code == 409
    assert runtime.receipt_reads == []
    assert runtime.launch_failures == []
