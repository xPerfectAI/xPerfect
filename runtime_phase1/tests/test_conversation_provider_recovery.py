"""Exact native result recovery uses the existing provider, never another author."""

import hashlib
import json
import base64

import pytest

from test_conversation_provider import (
    AUTH,
    ChatCompletionRequest,
    NativeUsageRuntime,
    _client,
    _scoped_client,
    _payload,
    lifecycle_owned_test_clients,
)


def _post_bound(client, payload, *, invocation="invocation-a", headers=None):
    body = json.dumps(payload, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()
    response = client.post(
        "/v1/chat/completions",
        headers={
            **AUTH,
            "Content-Type": "application/json",
            "X-Viventium-Native-Invocation-Id": invocation,
            "X-Viventium-Native-Body-SHA256": digest,
            **(headers or {}),
        },
        content=body,
    )
    return response, digest


def _result(client, digest, *, invocation="invocation-a", headers=None, **params):
    return client.get(
        f"/v1/requests/by-invocation/{invocation}/result",
        headers={**AUTH, **(headers or {})},
        params={"stream_id": "stream-a", "message_id": "message-a",
                "body_sha256": digest, **params},
    )


def test_exact_native_invocation_cannot_change_authoring_scope(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch, trust_identity_headers=True)
    payload = _payload(tmp_path)
    response, digest = _post_bound(client, payload)
    assert response.status_code == 200, response.text
    saved = _result(client, digest).json()
    changed, same_digest = _post_bound(client, payload, headers={
        "X-Viventium-Actor-Kind": "system", "X-Viventium-Origin": "scheduler",
    })
    assert same_digest == digest
    assert changed.status_code == 409, changed.text
    assert _result(client, digest).json() == saved
    assert len(client.app.state.store.list_provider_sessions(owner_id="owner-a")) == 1


def test_stream_completion_saves_canonical_result(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    response, digest = _post_bound(client, _payload(tmp_path, stream=True))
    assert response.status_code == 200
    chunks = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    record = client.app.state.store.get_provider_request(chunks[0]["id"])
    assert record["state"] == "completed"
    assert record["response_json"], "completed stream lost its canonical result"
    saved = json.loads(record["response_json"])
    assert saved["choices"][0]["message"]["content"] == "Native answer."
    assert _result(client, digest).json()["response"] == saved


def test_stale_terminal_observer_returns_first_saved_winner(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    response, _ = _post_bound(client, payload)
    assert response.status_code == 200
    first = response.json()
    store = client.app.state.store
    provider = client.app.state.conversation_provider
    record = store.get_provider_request(first["id"])
    monkeypatch.setattr(provider, "_conversation_output", lambda *_: "Later mutable output")
    actual = provider.response_payload(
        {**record, "response_json": ""}, store.get_run(record["run_id"]),
        ChatCompletionRequest.model_validate(payload),
    )
    assert actual == first
    assert json.loads(store.get_provider_request(first["id"])["response_json"]) == first


def test_result_lookup_is_read_only_and_owner_scoped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response, digest = _post_bound(client, _payload(tmp_path))
    assert response.status_code == 200
    provider = client.app.state.conversation_provider
    def forbidden(*_args, **_kwargs):
        raise AssertionError("result lookup attempted authoring or reconciliation")
    monkeypatch.setattr(provider, "start", forbidden)
    monkeypatch.setattr(provider, "_sync", forbidden)
    monkeypatch.setattr(provider, "_start_serial_fallback", forbidden)
    recovered = _result(client, digest, include_tool_evidence=True)
    assert recovered.status_code == 200
    assert "graph_tool_evidence" in recovered.json()
    assert recovered.json()["response"] == response.json()
    assert recovered.json()["authority_sha256"]
    assert _result(client, digest, headers={"X-Viventium-User-Id": "owner-b"}).status_code == 404
    assert _result(client, digest, stream_id="another-stream").status_code == 409
    assert _result(client, digest, message_id="another-message").status_code == 409
    assert _result(client, "0" * 64).status_code == 409


@pytest.mark.parametrize("state", ["queued", "running", "failed", "cancelled"])
def test_unsuccessful_or_pending_result_never_contains_answer(tmp_path, monkeypatch, state):
    client = _client(tmp_path, monkeypatch)
    response, digest = _post_bound(client, _payload(tmp_path))
    assert response.status_code == 200
    # Even a corrupt/legacy unsuccessful row with a stale cached artifact must
    # not expose that artifact as a completed answer.
    store = client.app.state.store
    store.update_provider_request(response.json()["id"], state=state)
    result = _result(client, digest)
    assert result.status_code == 200
    assert result.json()["state"] == state
    assert "response" not in result.json()


@pytest.mark.parametrize("headers", [
    {"X-Viventium-Surface": "telegram"},
    {"X-GlassHive-Turn-Context-B64": base64.b64encode(b"Changed trusted turn context").decode()},
    {"X-Viventium-Audio-Eligible": "true"},
])
def test_duplicate_invocation_rejects_changed_effective_header_authority(tmp_path, monkeypatch, headers):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    first, _ = _post_bound(client, payload)
    assert first.status_code == 200
    second, _ = _post_bound(client, payload, headers=headers)
    assert second.status_code == 409


def test_other_invocation_cannot_adopt_bound_native_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    first, _ = _post_bound(client, payload)
    assert first.status_code == 200
    second, _ = _post_bound(client, payload, invocation="invocation-b")
    assert second.status_code == 409


def test_identical_invocation_reuses_exact_native_request(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    first, _ = _post_bound(client, payload)
    second, _ = _post_bound(client, payload)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()


def test_terminal_commit_cannot_overwrite_stop_or_another_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response, _ = _post_bound(client, _payload(tmp_path))
    store = client.app.state.store
    record = store.get_provider_request(response.json()["id"])
    store.update_provider_request(record["request_id"], state="running", response_json="")
    wrong_run = store.commit_provider_request_terminal(
        record["request_id"], expected_run_id="another-run", state="completed",
        response_json=json.dumps(response.json()),
    )
    assert wrong_run["state"] == "running" and not wrong_run["response_json"]
    store.update_provider_request(record["request_id"], state="cancelled")
    stopped = store.commit_provider_request_terminal(
        record["request_id"], expected_run_id=record["run_id"], state="completed",
        response_json=json.dumps(response.json()),
    )
    assert stopped["state"] == "cancelled" and not stopped["response_json"]


@pytest.mark.parametrize("rebound", [False, True])
def test_retained_contract_repairs_missing_artifact_without_another_author(tmp_path, monkeypatch, rebound):
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    response, digest = _post_bound(client, _payload(tmp_path))
    store = client.app.state.store
    provider = client.app.state.conversation_provider
    record = store.get_provider_request(response.json()["id"])
    original_session = store.get_provider_session_by_id(record["session_id"])
    original_worker_id = original_session["worker_id"]
    collected = []
    original_collector = provider.service.runtime.provider_activity_log
    def collect_original(worker, run_id):
        collected.append((worker["worker_id"], run_id))
        assert worker["worker_id"] == original_worker_id
        assert run_id == record["run_id"]
        return original_collector(worker, run_id)
    monkeypatch.setattr(provider.service.runtime, "provider_activity_log", collect_original)
    def collect_citations(worker, run_id):
        assert worker["worker_id"] == original_worker_id
        assert run_id == record["run_id"]
        return [{"url": "https://example.test/source", "title": "Synthetic source"}]
    monkeypatch.setattr(provider.service.runtime, "provider_citation_sources", collect_citations, raising=False)
    if rebound:
        replacement = store.create_worker(
            project_id=original_session["project_id"], owner_id="owner-a",
            name="Replacement worker", role="conversation-agent", profile="codex-cli",
            backend="", runtime="codex-cli", model="gpt-5.6-sol",
        )
        updated = store.upsert_provider_session(
            tenant_id="local", owner_id="owner-a",
            conversation_id=original_session["conversation_id"],
            agent_id=original_session["agent_id"], model_id=original_session["model_id"],
            project_id=original_session["project_id"], worker_id=replacement["worker_id"],
            workspace_dir=str(tmp_path), access_mode="workspace",
        )
        assert updated["session_id"] == original_session["session_id"]
    store.update_provider_request(record["request_id"], response_json="")
    assert _result(client, digest).json()["state"] == "unsupported"
    assert [row["request_id"] for row in store.list_provider_completed_without_response()] == [record["request_id"]]
    def forbidden(*_args, **_kwargs):
        raise AssertionError("artifact repair attempted another author")
    monkeypatch.setattr(provider, "start", forbidden)
    monkeypatch.setattr(provider, "_start_serial_fallback", forbidden)
    monkeypatch.setattr(provider.service, "assign_run", forbidden)
    assert provider._reconcile_detached_request_once(record["request_id"]) is False
    result = _result(client, digest).json()
    assert result["state"] == "completed"
    assert result["run_id"] == record["run_id"]
    assert result["response"]["choices"][0]["message"]["content"] == "Native answer."
    assert collected


def test_body_digest_is_verified_before_any_native_start(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid body reached native start")
    monkeypatch.setattr(client.app.state.conversation_provider, "start", forbidden)
    response, _ = _post_bound(client, _payload(tmp_path), headers={"X-Viventium-Native-Body-SHA256": "0" * 64})
    assert response.status_code == 400


@pytest.mark.parametrize("stream", [False, True])
def test_current_memory_body_survives_initial_resume_and_configured_fallback(tmp_path, monkeypatch, stream):
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    text = "Décision suivante: vérifier. 次の行動を確認する。 " * 1200
    first_context = "Current time: synthetic.\n" + json.dumps({"saved_memory": {"status": "available", "text": text}}, ensure_ascii=False)
    assert len(first_context.encode()) > 32 * 1024
    sessions = []
    for turn, context in enumerate([first_context, json.dumps({"saved_memory": {"status": "available", "text": "Corrected exact preference."}})]):
        payload = _payload(tmp_path, stream=stream)
        payload["metadata"].update(turn_context=context, message_id=f"message-{turn}", idempotency_key=f"turn-{turn}")
        if turn:
            payload["messages"] += [{"role": "assistant", "content": "Earlier reply."}, {"role": "user", "content": "Confirm the current preference."}]
        response, digest = _post_bound(client, payload, invocation=f"turn-{turn}", headers={"X-GlassHive-Fallback-Model": "claude-code:opus", "X-GlassHive-Fallback-Reasoning-Effort": "high"})
        assert response.status_code == 200, response.text
        if stream:
            event = next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]")
            request_id = event["id"]
        else:
            request_id = response.json()["id"]
        store = client.app.state.store
        record = store.get_provider_request(request_id)
        run = store.get_run(record["run_id"])
        assert context in record["admitted_instruction"]
        assert context in record["fallback_instruction"]
        assert run["instruction"] == record["admitted_instruction"]
        assert record["native_body_sha256"] == digest
        if turn:
            assert text not in record["admitted_instruction"]
        sessions.append(record["session_id"])
    assert sessions[0] == sessions[1]


def test_current_memory_capacity_keeps_legacy_header_and_native_input_guards(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    context = "x" * (200 * 1024)
    payload["metadata"]["turn_context"] = context
    body, _ = _post_bound(client, payload)
    assert body.status_code == 413, body.text
    assert "No partial source was admitted" in body.text
    payload["metadata"].pop("turn_context")
    header, _ = _post_bound(client, payload, invocation="header-capacity", headers={"X-GlassHive-Turn-Context-B64": base64.b64encode(context.encode()).decode()})
    assert header.status_code == 400
    assert "turn context is too large" in header.text


def test_native_body_identity_rejects_changed_current_memory(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload["metadata"]["turn_context"] = '{"saved_memory":{"status":"available","text":"First value"}}'
    first, _ = _post_bound(client, payload)
    assert first.status_code == 200
    payload["metadata"]["turn_context"] = '{"saved_memory":{"status":"available","text":"Changed value"}}'
    changed, _ = _post_bound(client, payload)
    assert changed.status_code == 409


def test_response_deadline_after_session_rebind_stops_only_original_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())
    service = client.app.state.service
    store = client.app.state.store
    provider = client.app.state.conversation_provider
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    record = provider.start(ChatCompletionRequest.model_validate(_payload(tmp_path)))
    original = store.get_provider_session_by_id(record["session_id"])
    replacement = store.create_worker(
        project_id=original["project_id"], owner_id="owner-a", name="Replacement worker",
        role="conversation-agent", profile="codex-cli", backend="",
        runtime="codex-cli", model="gpt-5.6-sol",
    )
    rebound = store.upsert_provider_session(
        tenant_id="local", owner_id="owner-a", conversation_id=original["conversation_id"],
        agent_id=original["agent_id"], model_id=original["model_id"],
        project_id=original["project_id"], worker_id=replacement["worker_id"],
        workspace_dir=str(tmp_path), access_mode="workspace",
    )
    assert rebound["session_id"] == record["session_id"]
    later = store.create_run(replacement["worker_id"], original["project_id"],
                             "Preserve this later work.", state="queued")
    interrupts = []
    monkeypatch.setattr(service.runtime, "interrupt_worker",
                        lambda worker, run_id=None: interrupts.append((worker["worker_id"], run_id)))
    record = store.update_provider_request(
        record["request_id"], response_timeout_s=1,
        response_deadline_at="2000-01-01T00:00:00+00:00",
    )
    expired, run = provider._expire_response_deadline(record)
    assert expired["state"] == "failed"
    assert run["run_id"] == record["run_id"]
    assert run["state"] == "failed"
    assert interrupts == [(original["worker_id"], record["run_id"])]
    assert store.get_run(later["run_id"])["state"] == "queued"
    assert store.get_provider_session_by_id(record["session_id"])["worker_id"] == replacement["worker_id"]
    provider._expire_response_deadline(expired)
    assert interrupts == [(original["worker_id"], record["run_id"])]
    assert sum(event["event_type"] == "failed" for event in
               store.list_provider_activity(record["request_id"])) == 1
