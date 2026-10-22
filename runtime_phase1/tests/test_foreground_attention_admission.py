"""Retained foreground attention is distinct from a still-running native reply."""

from datetime import datetime, timezone
import hashlib
import json

from fastapi import HTTPException
import pytest

from workers_projects_runtime.conversation_provider import ChatCompletionRequest
from test_conversation_provider import AUTH, _client, _payload, lifecycle_owned_test_clients


@pytest.fixture
def foreground_attention(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store, service, provider = (
        client.app.state.store, client.app.state.service,
        client.app.state.conversation_provider,
    )
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    payload = _payload(tmp_path)
    payload["metadata"]["provider_session_mode"] = "persistent"
    request = provider.start(ChatCompletionRequest.model_validate(payload))
    run = store.get_run(request["run_id"])
    store.update_run(run["run_id"], state="needs_input",
        runtime_invoked_at="2026-01-01T00:00:00+00:00",
        failure_class="provider_progress_stalled", failure_structured=1,
        provider_liveness_route_locked=1)
    store.update_provider_request(request["request_id"], state="failed")
    store.update_worker(run["worker_id"], state="needs_input", pid=None,
        compute_released_at=datetime.now(timezone.utc).isoformat())
    session = store.get_provider_session_by_id(request["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    store.update_provider_session_history(session["session_id"], history_count=2,
        context_manifest={**manifest, "native_context_response_key": "msg:prior-answer"})
    payload["metadata"].update(main_context_protocol="main_context_v1", main_context_owner="core",
        message_id="next-answer", idempotency_key="next-turn", stream_id="next-stream", visible_message_chain=[{
            "id": "next-question", "role": "user",
            "sha256": hashlib.sha256(payload["messages"][-1]["content"].encode()).hexdigest(),
        }])
    return store, provider, payload, request, run, client


@pytest.mark.parametrize("binding_change", [False, True])
def test_released_foreground_attention_fails_promptly_without_occupancy_retry(
    foreground_attention, tmp_path, binding_change,
):
    store, provider, payload, request, run, _client_instance = foreground_attention
    if binding_change:
        payload["messages"][0]["content"] = "A different declared instruction."
    retained_run = dict(store.get_run(run["run_id"]))
    retained_request = dict(store.get_provider_request(request["request_id"]))
    retained_worker = dict(store.get_worker(run["worker_id"]))
    parsed = ChatCompletionRequest.model_validate(payload)

    with pytest.raises(HTTPException) as failure:
        provider._session(parsed, provider._model(parsed.model), tmp_path, "medium", tenant_id="local")

    assert failure.value.status_code == 409
    assert failure.value.detail["code"] == "provider_progress_stalled"
    assert store.get_run(run["run_id"]) == retained_run
    assert store.get_provider_request(request["request_id"]) == retained_request
    assert store.get_worker(run["worker_id"]) == retained_worker


@pytest.mark.parametrize("guard", [
    "live_request", "unreleased", "live_lease", "foreign_owner", "foreign_session",
    "restore_hold", "unknown", "stateless", "operator_pause", "queued_work",
])
def test_unproved_or_different_attention_keeps_the_existing_session_fence(
    foreground_attention, tmp_path, guard,
):
    store, provider, payload, request, run, _client_instance = foreground_attention
    if guard == "live_request":
        store.update_provider_request(request["request_id"], state="running")
    elif guard == "unreleased":
        store.update_worker(run["worker_id"], compute_released_at=None)
    elif guard == "live_lease":
        store.acquire_host_run_lease(runtime_family="codex", lane="conversation", tenant_id="local",
            owner_id="owner-a", worker_id=run["worker_id"], run_id=run["run_id"],
            executor_id="synthetic-active-owner", conversation_limit=2, mission_limit=3,
            account_mission_limit=4, tenant_mission_limit=12, lease_ttl_s=300)
    elif guard == "foreign_owner":
        store.update_provider_request(request["request_id"], owner_id="other-owner")
    elif guard == "foreign_session":
        other = store.upsert_provider_session(tenant_id="local", owner_id="owner-a",
            conversation_id="other-conversation", agent_id="other-agent",
            model_id=payload["model"], project_id=run["project_id"], worker_id=run["worker_id"],
            workspace_dir=str(tmp_path), access_mode="workspace")
        store.update_provider_request(request["request_id"], session_id=other["session_id"])
    elif guard == "restore_hold":
        store.update_provider_request(request["request_id"], restore_hold=1)
    elif guard == "unknown":
        store.update_run(run["run_id"], failure_class="unknown")
    elif guard == "stateless":
        store.update_provider_request(request["request_id"],
            replay_decision_json=json.dumps({"provider_session_mode": "stateless"}))
    elif guard == "operator_pause":
        store.update_worker(run["worker_id"], state="paused")
    else:
        store.create_run(run["worker_id"], run["project_id"], "Another accepted goal", state="queued")
    retained_run = dict(store.get_run(run["run_id"]))
    retained_worker = dict(store.get_worker(run["worker_id"]))
    parsed = ChatCompletionRequest.model_validate(payload)

    with pytest.raises(HTTPException) as failure:
        provider._session(parsed, provider._model(parsed.model), tmp_path, "medium", tenant_id="local")

    assert failure.value.status_code == 409
    assert failure.value.detail["code"] == "conversation_session_authority_conflict"
    assert store.get_run(run["run_id"]) == retained_run
    assert store.get_worker(run["worker_id"]) == retained_worker


def test_foreground_attention_reaches_http_client_without_admitting_a_new_request(foreground_attention):
    store, _provider, payload, request, run, client = foreground_attention
    payload["metadata"].update(stable_authority_sha256="a" * 64,
        main_context_snapshot_sha256="b" * 64, main_context_epoch="c" * 64,
        continuity_domain_id="d" * 64, continuity_agent_id="agent-main", logical_turn_id="next-turn")
    retained_run = dict(store.get_run(run["run_id"]))
    retained_request = dict(store.get_provider_request(request["request_id"]))

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_progress_stalled"
    assert response.json()["error"]["foregroundAttention"] == {
        "version": 1, "responseMessageId": request["message_id"],
    }
    assert store.get_run(run["run_id"]) == retained_run
    assert store.get_provider_request(request["request_id"]) == retained_request
    assert len(store.list_provider_requests_by_state({"queued", "running", "failed"})) == 1


def _native_result(client, store, request):
    store.update_provider_request(request["request_id"],
        native_invocation_id="synthetic-foreground-invocation", native_body_sha256="a" * 64)
    return client.get("/v1/requests/by-invocation/synthetic-foreground-invocation/result",
        headers=AUTH, params={"stream_id": request["stream_id"],
            "message_id": request["message_id"], "body_sha256": "a" * 64})


def test_exact_foreground_cancel_releases_only_the_failed_attention(foreground_attention):
    store, _provider, _payload_instance, request, run, client = foreground_attention
    observed = _native_result(client, store, request)
    assert observed.status_code == 200
    assert observed.json()["foreground_attention_control"] == {
        "version": 1, "can_cancel": True, "capacity_released": False,
    }
    response = client.post(f"/v1/requests/{request['request_id']}/cancel", headers={
        **AUTH, "X-Viventium-Foreground-Attention": "synthetic-foreground-invocation"})
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["capacityReleased"] is True
    assert store.get_run(run["run_id"])["state"] == "cancelled"
    again = client.post(f"/v1/requests/{request['request_id']}/cancel", headers={
        **AUTH, "X-Viventium-Foreground-Attention": "synthetic-foreground-invocation"})
    assert again.status_code == 200
    assert again.json()["capacityReleased"] is True


@pytest.mark.parametrize("changed", ["invoked_replacement", "resumed", "restore_hold", "wrong_invocation"])
def test_foreground_cancel_revalidates_before_touching_native_authority(foreground_attention, changed):
    store, _provider, _payload_instance, request, run, client = foreground_attention
    assert _native_result(client, store, request).status_code == 200
    invocation = "synthetic-foreground-invocation"
    if changed == "invoked_replacement":
        newer = store.create_run(run["worker_id"], run["project_id"], "Another accepted goal", state="running")
        store.update_run(newer["run_id"], runtime_invoked_at=datetime.now(timezone.utc).isoformat())
    elif changed == "resumed":
        store.update_run(run["run_id"], state="queued")
    elif changed == "restore_hold":
        store.update_provider_request(request["request_id"], restore_hold=1)
    else:
        invocation = "different-invocation"
    retained = dict(store.get_run(run["run_id"]))
    response = client.post(f"/v1/requests/{request['request_id']}/cancel", headers={
        **AUTH, "X-Viventium-Foreground-Attention": invocation})
    assert response.status_code == 409
    assert store.get_run(run["run_id"]) == retained
    assert store.get_provider_request(request["request_id"])["state"] == "failed"


def test_foreground_cancel_finishes_an_acknowledged_but_unreleased_exact_request(foreground_attention):
    store, _provider, _payload_instance, request, run, client = foreground_attention
    assert _native_result(client, store, request).status_code == 200
    store.update_provider_request(request["request_id"], state="cancelled")
    response = client.post(f"/v1/requests/{request['request_id']}/cancel", headers={
        **AUTH, "X-Viventium-Foreground-Attention": "synthetic-foreground-invocation"})
    assert response.status_code == 200
    assert response.json()["capacityReleased"] is True
    assert store.get_run(run["run_id"])["state"] == "cancelled"


def test_foreground_cancel_rechecks_attention_after_acquiring_the_lifecycle_guard(foreground_attention, monkeypatch):
    store, _provider, _payload_instance, request, run, client = foreground_attention
    assert _native_result(client, store, request).status_code == 200
    service = client.app.state.service
    original_guard = service._acquire_worker_lifecycle_guard

    def resumed_before_guard(worker_id, **kwargs):
        guard = original_guard(worker_id, **kwargs)
        store.update_run(run["run_id"], state="queued")
        return guard

    monkeypatch.setattr(service, "_acquire_worker_lifecycle_guard", resumed_before_guard)
    response = client.post(f"/v1/requests/{request['request_id']}/cancel", headers={
        **AUTH, "X-Viventium-Foreground-Attention": "synthetic-foreground-invocation"})
    assert response.status_code == 409
    assert store.get_run(run["run_id"])["state"] == "queued"
    assert store.get_provider_request(request["request_id"])["state"] == "failed"
