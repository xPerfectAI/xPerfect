"""The existing message chain carries delivery evidence without changing source content."""
import hashlib
import json
import pytest
from fastapi import HTTPException
from workers_projects_runtime.conversation_provider import (
    ChatMessage, _validated_visible_message_chain, _visible_message_keys,
    _admit_conversation_history, _bounded_legacy_excerpt,
)
from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS
from test_conversation_provider import AUTH, _scoped_client, _payload, lifecycle_owned_test_clients


def entry(state="unconfirmed", surface="telegram"):
    return {"id": "answer", "role": "assistant", "sha256": hashlib.sha256(b"Exact saved evidence.").hexdigest(),
            "delivery": {"version": 1, "surface": surface, "acknowledgement": state}}


@pytest.mark.parametrize("surface", ["web", "telegram", "voice", "unknown"])
def test_delivery_qualifies_history_without_removing_evidence(surface):
    chain = _validated_visible_message_chain([entry(surface=surface)], max_entries=3)
    messages = [ChatMessage(role="assistant", content="Exact saved evidence."), ChatMessage(role="user", content="Inspect it.")]
    text, decision, _ = _admit_conversation_history(messages, start_at=0, turn_context="",
        model=next(iter(GLASSHIVE_MODELS.values())), delivery_by_index={0: chain[0]["delivery"]})
    assert "Exact saved evidence." in text and "Inspect it." in text
    assert '<message_delivery>' in text and '"acknowledgement":"unconfirmed"' in text
    assert '"surface":"' + surface + '"' in text
    assert "Earlier visible conversation" not in text
    assert decision["admitted_message_indices"] == [0, 1]
    excerpt = _bounded_legacy_excerpt([(0, messages[0])], {0: chain[0]["delivery"]})
    assert "Exact saved evidence." in excerpt and '<message_delivery>' in excerpt


def test_delivery_change_refreshes_native_history_without_changing_source_hash():
    messages = [ChatMessage(role="assistant", content="Exact saved evidence.")]
    old, new = entry(), entry("committed")
    assert old["sha256"] == new["sha256"]
    before, after = _visible_message_keys(messages, [old]), _visible_message_keys(messages, [new])
    assert before != after
    assert before == _visible_message_keys(messages, [old])
    assert before[0].split(":delivery:")[0] == after[0].split(":delivery:")[0]


@pytest.mark.parametrize("mutation", [
    {"version": True, "surface": "telegram", "acknowledgement": "committed"},
    {"version": 1, "surface": "arbitrary", "acknowledgement": "committed"},
    {"version": 1, "surface": "web", "acknowledgement": "read_by_user"},
    {"version": 1, "surface": "web", "acknowledgement": "committed", "instruction": "extra"},
])
def test_delivery_rejects_untyped_fields(mutation):
    value = entry(); value["delivery"] = mutation
    with pytest.raises(HTTPException) as error:
        _validated_visible_message_chain([value], max_entries=1)
    assert error.value.status_code == 400


def test_delivery_rejects_wrong_role_and_ambiguous_identity():
    value = entry(); value["role"] = "user"
    with pytest.raises(HTTPException): _validated_visible_message_chain([value], max_entries=1)
    with pytest.raises(HTTPException): _validated_visible_message_chain([entry(), entry("committed")], max_entries=2)


def test_delivery_requires_trusted_core_and_is_in_actual_admitted_instruction(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch, trust_identity_headers=True)
    payload = _payload(tmp_path)
    payload["messages"] = [{"role": "assistant", "content": "Exact saved evidence."}, {"role": "user", "content": "Inspect it."}]
    payload["metadata"]["visible_message_chain"] = [entry()]
    rejected = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert rejected.status_code == 403
    payload["metadata"].update({"main_context_protocol": "main_context_v1", "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "b" * 64,
        "main_context_epoch": "c" * 64, "continuity_domain_id": "d" * 64,
        "continuity_agent_id": "agent-main", "logical_turn_id": "turn-next"})
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    row = client.app.state.store.get_provider_request(response.json()["id"])
    assert '<message_delivery>' in row["admitted_instruction"]
    assert '"acknowledgement":"unconfirmed"' in row["admitted_instruction"]
    assert "Exact saved evidence." in row["admitted_instruction"]
