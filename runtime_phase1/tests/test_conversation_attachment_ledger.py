"""Selected attachment identities reach the existing accepted-turn context."""
import json
from types import SimpleNamespace

import pytest

from workers_projects_runtime import conversation_provider as provider
from workers_projects_runtime import upload_projection
from workers_projects_runtime.worker_configuration import contextual_instruction


@pytest.fixture
def selected_uploads(monkeypatch):
    # Isolate existing storage resolution; this test exercises the unchanged typed selector,
    # upload projector, public ledger and accepted-turn producer without external storage.
    monkeypatch.setattr(upload_projection, "_owner_sources_for_file_ids", lambda *a, **kw: {})
    monkeypatch.setattr(upload_projection, "_trusted_virtual_source", lambda *a, **kw: "")
    return [
        {"file_id": "synthetic-alpha", "filename": "alpha.txt", "source": "text",
         "type": "text/plain", "text": "Exact Alpha body.\n", "bytes": 18, "media_group_index": 0},
        {"file_id": "synthetic-bravo", "filename": "bravo.pdf", "source": "local",
         "type": "application/pdf", "bytes": 19, "media_group_index": 1,
         "filepath": "/private/source/bravo.pdf", "bootstrap_source_token": "private-grant"},
    ]


def _payload(files, selected=None):
    bundle = {"viventium_upload_context": {"selected_uploads": files}}
    if selected is not None:
        bundle["viventium_delegation_packet"] = {"selected_files": [
            {"ordinal": index, "ref": ref} for index, ref in enumerate(selected)
        ]}
    return provider.ChatCompletionRequest(
        model="codex-cli:gpt-6.1-sol",
        messages=[{"role": "user", "content": "Use the selected notes."}],
        metadata={"owner_id": "synthetic-owner", "bootstrap_bundle": bundle},
    )


def _ledger(context):
    return json.loads(context.splitlines()[1])["selected_uploads"]


@pytest.mark.parametrize("model", ["codex-cli:gpt-6.1-sol", "grok-build:grok-4.7-build-fast"])
@pytest.mark.parametrize("previous_messages", [0, 2])
def test_selected_file_ids_reach_current_accepted_turn(selected_uploads, model, previous_messages):
    payload = _payload(selected_uploads)
    context = provider.ConversationProvider._request_attachment_context(payload)
    ledger = _ledger(context)
    assert [item["file_id"] for item in ledger] == ["synthetic-alpha", "synthetic-bravo"]
    assert [item["filename"] for item in ledger] == ["alpha.txt", "bravo.pdf"]
    assert [item["inputKind"] for item in ledger] == ["extracted_text", "original"]
    assert [item["media_group_index"] for item in ledger] == [0, 1]
    messages = [provider.ChatMessage(role="system", content="Exact unchanged authority.")]
    if previous_messages:
        messages.extend([provider.ChatMessage(role="user", content="Earlier selected notes."),
                         provider.ChatMessage(role="assistant", content="Earlier answer.")])
    messages.append(provider.ChatMessage(role="user", content="Send only Alpha to work A."))
    instruction, decision, _ = provider._admit_conversation_history(
        messages, start_at=0, turn_context="", model=provider.GLASSHIVE_MODELS[model],
        current_input_indices={len(messages)-1}, attachment_context=context,
    )
    current = instruction.split("<viventium_current_accepted_turn_v1>")[1].split("</viventium_current_accepted_turn_v1>")[0]
    assert context in current
    assert all(item["file_id"] in current for item in ledger)
    assert decision["omitted_message_count"] == 0
    assert messages[0].content == "Exact unchanged authority."
    envelope = contextual_instruction(instruction, {"_context_projection": {
        "inline": [{"id": "bootstrap:file:0", "text": "Exact Alpha body.\n"}],
        "manifest": {"context": {"sources": []}},
    }})
    assert json.loads(envelope)["instruction"] == instruction
    assert json.loads(envelope)["context"][0]["text"] == "Exact Alpha body.\n"
    assert all(item["file_id"] in envelope for item in ledger)


def test_existing_paths_and_projected_body_are_unchanged(selected_uploads):
    payload = _payload(selected_uploads)
    projected = provider.ConversationProvider._projected_request_uploads(payload)
    assert projected[0]["content"] == "Exact Alpha body.\n"
    assert projected[1]["upload_blocker"]
    assert "original_bytes_unavailable" in projected[1]["content"]
    context = provider.ConversationProvider._request_attachment_context(payload)
    paths = [item["path"] for item in projected]
    old = "\n".join(["<viventium_attached_workspace_files version=\"1\">",
        "Read each original attachment directly from its ordered workspace path:",
        *(f"{index}. {path}" for index, path in enumerate(paths, 1)),
        "</viventium_attached_workspace_files>"])
    lines = context.splitlines()
    if len(lines) > 1 and lines[1].startswith('{"selected_uploads":'):
        lines.pop(1)
    assert "\n".join(lines) == old


def test_selected_branch_excludes_other_upload(selected_uploads):
    context = provider.ConversationProvider._request_attachment_context(_payload(selected_uploads, ["synthetic-alpha"]))
    assert "synthetic-bravo" not in context
    assert "bravo.pdf" not in context
    assert "alpha.txt" in context


def test_explicit_empty_selection_stays_empty(selected_uploads):
    assert provider.ConversationProvider._request_attachment_context(_payload(selected_uploads, [])) == ""


def test_missing_uploads_stays_empty():
    assert provider.ConversationProvider._request_attachment_context(_payload([])) == ""


def test_unknown_selected_ref_is_not_invented(selected_uploads):
    assert provider.ConversationProvider._request_attachment_context(_payload(selected_uploads, ["not-owned-here"])) == ""


def test_malformed_selected_authority_stays_rejected(selected_uploads):
    payload = _payload(selected_uploads)
    payload.metadata.bootstrap_bundle["viventium_delegation_packet"] = {"selected_files": [{"ordinal": 0, "name": "alpha.txt"}]}
    with pytest.raises(ValueError, match="stable upload ref"):
        provider.ConversationProvider._request_attachment_context(payload)


def test_safe_ledger_does_not_emit_paths_grants_or_body(selected_uploads):
    context = provider.ConversationProvider._request_attachment_context(_payload(selected_uploads))
    assert "/private/source" not in context
    assert "private-grant" not in context
    assert "Exact Alpha body" not in context


def test_invalid_id_is_not_promoted_to_selection_authority(monkeypatch):
    projected = [{"file_id": "../invalid", "filename": "safe.txt", "source": "text", "path": "uploads/safe.txt"}]
    monkeypatch.setattr(provider.ConversationProvider, "_projected_request_uploads", staticmethod(lambda payload: projected))
    context = provider.ConversationProvider._request_attachment_context(SimpleNamespace())
    assert "../invalid" not in context
    assert "safe.txt" in context
