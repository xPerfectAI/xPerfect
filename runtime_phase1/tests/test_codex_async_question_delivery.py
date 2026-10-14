"""Native async question acceptance must reach the current conversational user."""
from types import SimpleNamespace
import json

import pytest
from test_conversation_provider import lifecycle_owned_test_clients

from workers_projects_runtime import conversation_provider as module


QUESTION = {"title": "Which shape?", "options": ["Blue circle", "Green square"]}
BLOCK = "Which shape?\n- Blue circle\n- Green square"
FINAL = "No SVG written. Waiting for your choice."


def native_log(questions=None, accepted=None, call_id="call-a"):
    call = {"type": "function_call", "call_id": call_id,
            "name": "request_user_input_async",
            "arguments": json.dumps({"questions": [QUESTION] if questions is None else questions})}
    output = {"type": "function_call_output", "call_id": call_id,
              "output": json.dumps({"accepted": True} if accepted is None else accepted)}
    return "\n".join(json.dumps({"type": "response_item", "payload": item}) for item in [call, output])


def projected(log, content=FINAL):
    return getattr(module, "_codex_async_question_content", lambda _log, text: text)(log, content)


def canonical(log, *, origin="interactive", actor="external_user", profile="codex-cli", mutation=None):
    provider = object.__new__(module.ConversationProvider)
    request = {"request_id": "request-a", "run_id": "run-a", "session_id": "session-a",
               "owner_id": "owner-a", "tenant_id": "local", "state": "running",
               "native_invocation_id": "invocation-a", "message_id": "message-a",
               "replay_decision_json": json.dumps({"instruction_sha256": "a" * 64})}
    run = {"run_id": "run-a", "worker_id": "worker-a", "state": "completed"}
    session = {"owner_id": "owner-a", "tenant_id": "local", "actor_kind": actor,
               "origin": origin, "conversation_id": "conversation-a"}
    worker = {"owner_id": "owner-a", "tenant_id": "local"}
    contract = {"model": "codex-cli:gpt-6.1-sol", "prompt_tokens": 10,
                "delivery_control": {"version": 1, "audio_eligible": True}}
    provider.store = SimpleNamespace(get_provider_session_by_id=lambda _: session,
                                     get_worker=lambda _: worker)
    calls = []
    def collector(*args, **kwargs):
        calls.append((args, kwargs))
        return profile, log
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_tool_evidence_log=collector))
    provider._conversation_output = lambda *_args, **_kwargs: json.dumps({
        "type": "assistant_response", "tool_name": None, "content": FINAL, "voice": "skip"})
    provider._native_usage_snapshot = lambda *_: {}
    provider._native_effective_model = lambda *_: contract["model"]
    provider._graph_transfer_call_id = lambda *_: "transfer-a"
    if mutation:
        mutation(request, run, session, worker, contract, provider)
    response = provider._build_canonical_response(request, run, contract)
    return response, calls


def test_native_async_question_survives_terminal_selection_and_keeps_prefix():
    response, calls = canonical(native_log())
    message = response["choices"][0]["message"]
    assert message["content"] == FINAL + "\n\n" + BLOCK
    assert message["provider_specific_fields"]["viventium"]["delivery_disposition"]["audio"] == "skip"
    assert response["choices"][0]["finish_reason"] == "stop"
    assert len(calls) == 1
    assert calls[0][1] == {"instruction_sha256": "a" * 64}
    assert response["glasshive"]["tool_evidence"]["results"][0]["name"] == "request_user_input_async"


def test_question_is_once_and_meaningful_final_prefix_is_preserved():
    content = FINAL + "\n\n" + BLOCK
    assert projected(native_log(), content) == content
    assert projected(native_log() + "\n" + native_log(call_id="call-b")) == content
    assert projected(native_log(), "Useful actual result.").startswith("Useful actual result.\n\n")


@pytest.mark.parametrize("accepted", [{"accepted": False}, {"accepted": 1}, {}, [], {"accepted": True, "extra": True}])
def test_only_exact_native_acceptance_presents_a_question(accepted):
    assert projected(native_log(accepted=accepted)) == FINAL


@pytest.mark.parametrize("questions", [[], [{"title": ""}], [{"title": "Q", "options": []}],
    [{"title": "Q", "options": ["A", "A"]}], [{"title": "Q", "options": [1]}],
    [{"title": "Q", "options": ["A" * 161]}], [{"title": "Q", "id": "invented"}],
    [{"title": "🌲" * 3000, "options": ["A"]}]])
def test_unknown_or_oversized_question_never_emits_partial_choices(questions):
    assert projected(native_log(questions)) == FINAL


def test_native_free_text_question_and_ordered_multiple_questions():
    assert projected(native_log([{"title": "Your preference?"}])) == FINAL + "\n\nYour preference?"
    text = projected(native_log([QUESTION, {"title": "Anything else?", "options": None}]))
    assert text == FINAL + "\n\n" + BLOCK + "\n\nAnything else?"


@pytest.mark.parametrize("mutation", ["unmatched", "duplicate_call", "duplicate_output", "unavailable", "other_tool", "malformed"])
def test_only_unique_complete_current_native_call_pairs_are_used(mutation):
    rows = [json.loads(line) for line in native_log().splitlines()]
    if mutation == "unmatched": rows[1]["payload"]["call_id"] = "other"
    elif mutation == "duplicate_call": rows.insert(1, rows[0])
    elif mutation == "duplicate_output": rows.append(rows[1])
    elif mutation == "unavailable": rows.append({"type": "glasshive.tool_evidence_unavailable"})
    elif mutation == "other_tool": rows[0]["payload"]["name"] = "request_permission"
    elif mutation == "malformed": rows[0]["payload"]["arguments"] = '{"questions":[],"questions":[{}]}'
    assert projected("\n".join(map(json.dumps, rows))) == FINAL


@pytest.mark.parametrize("guard", ["system", "noninteractive", "other_owner", "other_tenant",
    "wrong_run", "failed", "cancelled", "not_completed", "activity_only", "graph_transfer", "other_provider"])
def test_current_public_conversation_fences_and_graph_authority_stay_intact(guard):
    def mutate(request, run, session, worker, contract, provider):
        if guard == "system": session["actor_kind"] = "system"
        elif guard == "noninteractive": session["origin"] = "system"
        elif guard == "other_owner": session["owner_id"] = "owner-b"
        elif guard == "other_tenant": worker["tenant_id"] = "other"
        elif guard == "wrong_run": run["run_id"] = "other"
        elif guard in {"failed", "cancelled"}: request["state"] = guard
        elif guard == "not_completed": run["state"] = "running"
        elif guard == "activity_only":
            provider.service.runtime.provider_activity_log = provider.service.runtime.provider_tool_evidence_log
            del provider.service.runtime.provider_tool_evidence_log
        elif guard == "graph_transfer":
            contract["graph_control"] = {"version": 1, "tools": [{"name": "lc_transfer_to_consultant", "description": "Consult"}]}
            provider._conversation_output = lambda *_args, **_kwargs: json.dumps({
                "type": "tool_call", "tool_name": "lc_transfer_to_consultant", "content": FINAL, "voice": "skip"})
    response, _ = canonical(native_log(), profile="claude-code" if guard == "other_provider" else "codex-cli", mutation=mutate)
    assert response["choices"][0]["message"]["content"] == FINAL


@pytest.mark.parametrize("stream", [False, True])
def test_http_and_sse_replay_keep_one_question_and_one_native_run(tmp_path, monkeypatch, stream):
    from test_conversation_provider import AUTH, _client, _payload, _publish_in_process_test_start
    from workers_projects_runtime.openclaw_runtime import StubRuntime

    class QuestionRuntime(StubRuntime):
        calls = 0
        reads = 0

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            self.calls += 1
            _publish_in_process_test_start(worker)
            return json.dumps({"type": "assistant_response", "tool_name": None,
                               "content": FINAL, "voice": "skip"})

        def provider_tool_evidence_log(self, worker, run_id, *, instruction_sha256):
            self.reads += 1
            assert instruction_sha256
            return "codex-cli", native_log()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = QuestionRuntime()
    client = _client(tmp_path, monkeypatch, runtime)
    payload = _payload(workspace, stream=stream)
    payload["metadata"]["native_invocation_id"] = "invocation-a"
    import hashlib
    body = json.dumps(payload, separators=(",", ":"))
    for _ in range(2):
        response = client.post("/v1/chat/completions", headers={**AUTH,
            "Content-Type": "application/json", "X-Viventium-Audio-Eligible": "true",
            "X-Viventium-Native-Invocation-Id": "invocation-a",
            "X-Viventium-Native-Body-Sha256": hashlib.sha256(body.encode()).hexdigest()}, content=body)
        assert response.status_code == 200
        if stream:
            chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: {")]
            content = "".join(choice.get("delta", {}).get("content", "")
                              for chunk in chunks for choice in chunk.get("choices", []))
        else:
            content = response.json()["choices"][0]["message"]["content"]
        assert content == FINAL + "\n\n" + BLOCK
    assert runtime.calls == 1


@pytest.mark.parametrize("scope", ["current", "old_turn", "wrong_input_digest",
    "contextual", "multimodal", "contextual_multimodal", "wrong_embedded_digest",
    "unknown_envelope", "duplicate_envelope_key", "duplicate_part", "duplicate_frame",
    "ambiguous_turn"])
def test_actual_rollout_reader_admits_only_exact_current_input_turn(tmp_path, monkeypatch, scope):
    import hashlib
    from workers_projects_runtime.profile_runtime import CodexCliRuntime
    from workers_projects_runtime.worker_configuration import contextual_instruction

    runtime = object.__new__(CodexCliRuntime)
    instruction = "Synthetic current request."
    stdin = instruction
    if scope in {"contextual", "contextual_multimodal", "wrong_embedded_digest",
                 "unknown_envelope", "duplicate_envelope_key"}:
        stdin = contextual_instruction(instruction, {"_context_projection": {
            "inline": [{"type": "synthetic"}], "manifest": {"context": {"version": 1}}}})
        if scope == "wrong_embedded_digest":
            stdin = stdin.replace(instruction, "Changed current request.")
        elif scope == "unknown_envelope":
            stdin = json.dumps({**json.loads(stdin), "unknown": True})
        elif scope == "duplicate_envelope_key":
            stdin = stdin[:-1] + ',"instruction":' + json.dumps(instruction) + '}'
    (tmp_path / "instruction.stdin").write_text(stdin)
    rows = []
    for turn, text in [("old", stdin if scope == "ambiguous_turn" else "Old request."), ("current", stdin)]:
        rows.append({"type": "turn_context", "payload": {"turn_id": turn}})
        parts = [{"type": "input_text", "text": text}]
        if turn == "current" and scope in {"multimodal", "contextual_multimodal"}:
            parts = [{"type": "input_text", "text": "Native attachment description."},
                     {"type": "input_image", "image_url": "data:image/png;base64,c3ludGhldGlj"}] + parts
        if turn == "current" and scope == "duplicate_part": parts.append(parts[0])
        frame = {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": parts,
            "internal_chat_message_metadata_passthrough": {"turn_id": turn}}}
        rows.append(frame)
        if turn == "current" and scope == "duplicate_frame": rows.append(frame)
        if turn == ("old" if scope == "old_turn" else "current"):
            for line in native_log().splitlines():
                row = json.loads(line)
                row["payload"]["internal_chat_message_metadata_passthrough"] = {"turn_id": turn}
                rows.append(row)
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)))
    monkeypatch.setattr(runtime, "_codex_run_rollout_path", lambda *_: path)
    monkeypatch.setattr(runtime, "_run_root", lambda *_: tmp_path)
    digest = hashlib.sha256(instruction.encode()).hexdigest()
    log = runtime.provider_rollout_tool_log({"worker_id": "worker-a"}, "run-a",
        instruction_sha256="0" * 64 if scope == "wrong_input_digest" else digest)
    assert projected(log) == (FINAL + "\n\n" + BLOCK if scope in {
        "current", "contextual", "multimodal", "contextual_multimodal"} else FINAL)
