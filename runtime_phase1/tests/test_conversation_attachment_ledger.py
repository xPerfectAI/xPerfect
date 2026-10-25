"""Selected attachment identities reach the existing accepted-turn context."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers_projects_runtime import conversation_provider as provider
from workers_projects_runtime import upload_projection
from workers_projects_runtime.worker_configuration import contextual_instruction
from workers_projects_runtime.bootstrap import _write_project_files


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
    assert [item["inputKind"] for item in ledger] == ["extracted_text", "metadata_only"]
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
    for index, path in enumerate(paths, 1):
        assert f"{index}. {path}" in context.splitlines()
    assert "Read each original attachment" not in context
    assert "Metadata-only entries report unavailable input" in context


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
    context = provider.ConversationProvider._request_attachment_context(_payload(projected))
    assert "../invalid" not in context
    assert "safe.txt" in context


@pytest.mark.parametrize(("filename", "text", "workspace_name"), [
    ("report.pdf", "Exact PDF extraction.\r\n", "report.pdf.txt"),
    ("scan.png", "Exact image extraction.\n", "scan.png.txt"),
    ("empty.txt", "", "empty.txt"),
    ("space.txt", " \t\r\n", "space.txt"),
    ("untitled", "Exact résumé.\n", "untitled.txt"),
])
def test_declared_text_materializes_exact_body_before_any_original_path(
    tmp_path, monkeypatch, filename, text, workspace_name,
):
    uploads = tmp_path / "uploads"
    original = uploads / "synthetic-owner" / f"synthetic-file__{filename}"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"synthetic-original-binary-different-from-extraction")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads))
    record = {"file_id": "synthetic-file", "filename": filename, "source": "text",
              "text": text, "bytes": original.stat().st_size, "media_group_index": 4,
              "filepath": f"/uploads/synthetic-owner/{original.name}"}
    payload = _payload([record])
    projected = provider.ConversationProvider._projected_request_uploads(payload)
    assert len(projected) == 1
    item = projected[0]
    assert item["path"] == f"uploads/{workspace_name}"
    assert item["filename"] == filename
    assert item["content"] == text
    assert item["bytes"] == record["bytes"]
    assert item["media_group_index"] == 4
    assert "source_path" not in item
    assert "upload_blocker" not in item
    assert _ledger(provider.ConversationProvider._request_attachment_context(payload))[0]["inputKind"] == "extracted_text"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project_files(tmp_path / "home", workspace, {"files": projected}, {},
                         shutil.copyfile, shutil.copytree)
    assert (workspace / item["path"]).read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("body", [None, 42])
def test_declared_text_without_string_body_stays_metadata_only(tmp_path, monkeypatch, body):
    uploads = tmp_path / "uploads"
    original = uploads / "synthetic-owner" / "synthetic-file__report.pdf"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original-binary-is-not-the-declared-extraction")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads))
    record = {"file_id": "synthetic-file", "filename": "report.pdf", "source": "text"}
    if body is not None:
        record["text"] = body
    payload = _payload([record])
    [item] = provider.ConversationProvider._projected_request_uploads(payload)
    assert item["upload_blocker"]
    assert json.loads(item["content"])["source_status"] == "extracted_text_unavailable"
    assert "source_path" not in item
    assert _ledger(provider.ConversationProvider._request_attachment_context(payload))[0]["inputKind"] == "metadata_only"


def test_declared_text_keeps_selection_order_and_distinct_workspace_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(tmp_path / "missing"))
    files = [
        {"file_id": "first", "filename": "same.pdf", "source": "text", "text": "First.\n", "media_group_index": 7},
        {"file_id": "second", "filename": "same.pdf", "source": "text", "text": "", "media_group_index": 2},
        {"file_id": "binary", "filename": "ordinary.pdf", "source": "local", "text": "Not original bytes."},
    ]
    projected = provider.ConversationProvider._projected_request_uploads(_payload(files))
    assert [item["file_id"] for item in projected] == ["first", "second", "binary"]
    assert [item["media_group_index"] for item in projected[:2]] == [7, 2]
    assert [item["content"] for item in projected[:2]] == ["First.\n", ""]
    assert [Path(item["path"]).suffix for item in projected[:2]] == [".txt", ".txt"]
    assert len({item["path"] for item in projected}) == 3
    assert projected[2]["path"] == "uploads/ordinary.pdf.metadata.json"
    assert projected[2]["upload_blocker"]
