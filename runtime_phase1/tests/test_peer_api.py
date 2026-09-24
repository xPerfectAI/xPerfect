from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.peer_collaboration import (
    PeerGrantRequest,
    PeerMessageRequest,
    PeerPolicyUpdate,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_OWNER_ID", "owner-a")
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    application = create_app(
        str(tmp_path / "runtime.db"), runtime=StubRuntime(), reconcile_on_startup=False
    )
    store = application.state.store
    project = store.create_project(
        owner_id="owner-a",
        tenant_id="local",
        title="Synthetic",
        goal="Synthetic",
        default_worker_profile="codex-cli",
    )
    workers = [
        store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            tenant_id="local",
            name=name,
            role="worker",
            profile="codex-cli",
            backend="stub",
            runtime="stub",
            model="test",
        )
        for name in ("A", "B")
    ]
    with TestClient(application) as client:
        yield application, client, workers


def enable(application, workers):
    peers = application.state.service.peers
    for worker in workers:
        peers.set_policy(
            worker["workspace_id"],
            tenant_id="local",
            owner_id="owner-a",
            request=PeerPolicyUpdate(
                expected_revision=1, discovery="account", access_enabled=True
            ),
        )
    return peers


def test_hosted_plaintext_peer_bridge_does_not_block_an_ordinary_run(app, monkeypatch):
    application, _client, workers = app
    enable(application, workers)
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "hosted-xfs")
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://runtime:8766")
    runtime = application.state.service.runtime
    original = runtime.run_task
    dispatched = []

    def capture(worker, instruction, **kwargs):
        dispatched.append(dict(worker))
        return original(worker, instruction, **kwargs)

    monkeypatch.setattr(runtime, "run_task", capture)
    worker = workers[0]
    run = application.state.service.assign_run(worker["worker_id"], "Ordinary hosted task")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        saved = application.state.store.get_run(run["run_id"])
        if saved and saved["state"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert saved["state"] == "completed", saved
    assert saved["output_text"] == "STUB_OK: Ordinary hosted task"
    assert len(dispatched) == 1
    assert "_peer_native_projection" not in dispatched[0]
    assert application.state.service.peers.policy(
        worker["workspace_id"], tenant_id="local", owner_id="owner-a"
    )["native_peer_status"] == {
        "available": False, "code": "peer_native_endpoint_requires_tls"
    }


def test_owner_api_policy_and_grant_are_scoped(app):
    _application, client, workers = app
    a, b = workers
    policy_url = f"/v1/workspaces/{a['workspace_id']}/peer-policy"
    assert client.get(policy_url).json()["discovery"] == "off"
    assert (
        client.put(
            policy_url,
            json={
                "expected_revision": 1,
                "discovery": "account",
                "access_enabled": True,
            },
        ).status_code
        == 200
    )
    assert (
        client.put(
            policy_url, json={"expected_revision": 1, "discovery": "off"}
        ).status_code
        == 409
    )
    assert client.get("/v1/workers/wrk_unknown/peers").status_code == 404
    assert (
        client.post(
            f"/v1/workers/{a['worker_id']}/peer-messages",
            json={
                "target_worker_id": b["worker_id"],
                "grant_id": "guess",
                "message": "Hi",
                "idempotency_key": "message",
            },
        ).status_code
        == 403
    )


def test_native_mcp_requires_run_token_and_has_no_owner_mutations(app):
    application, client, workers = app
    a, b = workers
    peers = enable(application, workers)
    run = application.state.store.create_run(
        a["worker_id"], a["project_id"], "Synthetic native turn", state="running"
    )
    with application.state.store._connect() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (run["run_id"],))
    token = peers.mint_native_session(a["worker_id"], run["run_id"])
    url = "/v1/native/peers/"
    packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert client.post(url, json=packet).status_code == 401
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
    }
    response = client.post(url, json=packet, headers=headers)
    assert response.status_code == 200, response.text
    names = {x["name"] for x in response.json()["result"]["tools"]}
    assert {
        "peers_list",
        "peer_message",
        "peer_messages",
        "peer_context_read",
        "peer_wait",
    } <= names
    assert not names & {
        "peer_policy_set",
        "peer_access_grant",
        "peer_access_revoke",
        "peer_access_grant_batch",
        "peer_access_options",
    }
    response = client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "peers_list", "arguments": {}},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = json.loads(response.json()["result"]["content"][0]["text"])
    assert [x["worker_id"] for x in data["items"]] == [b["worker_id"]]
    with application.state.store._connect() as conn:
        conn.execute(
            "UPDATE runs SET state='completed' WHERE run_id=?", (run["run_id"],)
        )
    assert client.post(url, json=packet, headers=headers).status_code == 401


def test_message_reaches_existing_worker_execution(app):
    application, _client, workers = app
    a, b = workers
    peers = enable(application, workers)
    permission = peers.grant(
        tenant_id="local",
        owner_id="owner-a",
        request=PeerGrantRequest(
            source_worker_id=a["worker_id"],
            target_worker_id=b["worker_id"],
            scopes=["message", "wake"],
            source_revision=2,
            target_revision=2,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            idempotency_key="grant",
        ),
    )
    delivered = peers.send(
        a["worker_id"],
        tenant_id="local",
        owner_id="owner-a",
        request=PeerMessageRequest(
            target_worker_id=b["worker_id"],
            grant_id=permission["grant_id"],
            message="Synthetic evidence request",
            idempotency_key="message",
        ),
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = application.state.store.get_run(delivered["delivery_run_id"])
        if run and run["state"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert run is not None, delivered
    assert run["state"] == "completed", run
    assert run["runtime_invoked_at"]
    assert json.loads(run["instruction"])["kind"] == "untrusted_peer_message"
    assert "Synthetic evidence request" in run["output_text"]
    receipt = peers.message(
        delivered["message_id"], a["worker_id"], tenant_id="local", owner_id="owner-a"
    )
    assert receipt["delivery_state"] == "invoked"
    assert receipt["model_read_confirmed"] is False


def test_native_mcp_message_reaches_recipient_turn_and_wait_times_out(app):
    application, client, workers = app
    a, b = workers
    peers = enable(application, workers)
    permission = peers.grant(
        tenant_id="local",
        owner_id="owner-a",
        request=PeerGrantRequest(
            source_worker_id=a["worker_id"],
            target_worker_id=b["worker_id"],
            scopes=["message", "wake"],
            source_revision=2,
            target_revision=2,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            idempotency_key="native-grant",
        ),
    )
    source = application.state.store.create_run(
        a["worker_id"], a["project_id"], "Synthetic native sender"
    )
    with application.state.store._connect() as conn:
        conn.execute(
            "UPDATE runs SET state='running',active_attempt_id='synthetic-attempt' WHERE run_id=?",
            (source["run_id"],),
        )
    token = peers.mint_native_session(a["worker_id"], source["run_id"])
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
    }
    packet = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "peer_message",
            "arguments": {
                "target_worker_id": b["worker_id"],
                "grant_id": permission["grant_id"],
                "message": "Native MCP synthetic evidence",
                "idempotency_key": "native-send",
            },
        },
    }
    response = client.post("/v1/native/peers/", json=packet, headers=headers)
    assert response.status_code == 200, response.text
    sent = json.loads(response.json()["result"]["content"][0]["text"])
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = application.state.store.get_run(sent["delivery_run_id"])
        if run and run["state"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert run["state"] == "completed", run
    assert "Native MCP synthetic evidence" in run["output_text"]
    result = client.get(
        f"/v1/workers/{a['worker_id']}/peer-wait?after={sent['sequence']}&timeout_seconds=0"
    )
    assert result.status_code == 200 and result.json()["timed_out"] is True
    assert result.json()["items"] == []


def test_owner_batch_api_requires_exact_snapshot_and_returns_persistent_grants(app):
    _application, client, workers = app
    a, b = workers
    options = client.get(f"/v1/workers/{a['worker_id']}/peer-access-options").json()
    body = {
        "source_worker_id": a["worker_id"],
        "workspaces": [
            {
                "workspace_id": w["workspace_id"],
                "revision": w["revision"],
                "member_ids": [m["worker_id"] for m in w["members"]],
            }
            for w in options["workspaces"]
        ],
        "target_worker_ids": [b["worker_id"]],
        "direction": "both",
        "enable_access": True,
        "expires_at": None,
        "idempotency_key": "api-batch",
    }
    first = client.post("/v1/peer-grants/batch", json=body)
    assert first.status_code == 201, first.text
    assert len(first.json()["items"]) == 2
    assert all(
        g["expires_at"] is None and g["status"] == "active"
        for g in first.json()["items"]
    )
    assert client.post("/v1/peer-grants/batch", json=body).json() == first.json()
    assert (
        client.post(
            "/v1/peer-grants/batch", json=body | {"owner_id": "override"}
        ).status_code
        == 422
    )
    assert client.get("/v1/workers/unknown/peer-access-options").status_code == 404
