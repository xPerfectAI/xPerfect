"""Historical originals do not consume a new invocation's visual-input budget."""
import base64
import hashlib
import shutil
import struct
import zlib

import pytest
from fastapi import HTTPException

from workers_projects_runtime import conversation_provider as provider
from workers_projects_runtime import deliverables, upload_projection
from workers_projects_runtime.bootstrap import _write_project_files


def png(value):
    def chunk(kind, body):
        return struct.pack("!I", len(body)) + kind + body + struct.pack(
            "!I", zlib.crc32(kind + body) & 0xFFFFFFFF,
        )

    return b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 2, 0, 0, 0),
    ) + chunk(b"IDAT", zlib.compress(bytes([0, value, 40, 80]))) + chunk(b"IEND", b"")


def image(data):
    return {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(data).decode(),
    }}


def request(history, current=None, *, model="claude-code:claude-opus-5", current_indices=None):
    messages = [{"role": "system", "content": "Unchanged application authority."}]
    for index, data in enumerate(history):
        messages.extend([
            {"role": "user", "content": [image(data), {"type": "text", "text": f"Earlier image {index}."}]},
            {"role": "assistant", "content": f"Earlier response {index}."},
        ])
    messages.append({"role": "user", "content": current or "Current text without an image."})
    payload = provider.ChatCompletionRequest(
        model=model, messages=messages, metadata={"owner_id": "synthetic-owner"},
    )
    if current_indices is not None:
        payload.metadata.visible_message_chain = [
            {"id": f"source-{index}", "role": message.role,
             "sha256": hashlib.sha256(provider._visible_authored_text(message.content).encode()).hexdigest(),
             "accepted_source": True, "current_input": index in current_indices}
            for index, message in enumerate(payload.messages) if message.role == "user"
        ]
    return payload


def bundle(payload):
    return provider.ConversationProvider._native_bundle(
        None, payload, provider.GLASSHIVE_MODELS[payload.model], "low",
    )


@pytest.mark.parametrize("model", ["claude-code:claude-opus-5", "codex-cli:gpt-6.1-sol"])
def test_history_above_native_image_count_keeps_text_turn_available(model):
    history = [png(index) for index in range(deliverables.NATIVE_MEDIA_MAX_ITEMS + 3)]
    result = bundle(request(history, model=model))
    assert result.get("native_input_images", []) == []
    assert {item["sha256"] for item in result["files"]} == {
        hashlib.sha256(data).hexdigest() for data in history
    }


def test_new_image_after_long_history_is_only_fresh_visual_input():
    history = [png(index) for index in range(deliverables.NATIVE_MEDIA_MAX_ITEMS + 3)]
    current = png(200)
    result = bundle(request(history, [image(current)]))
    assert [item["sha256"] for item in result["native_input_images"]] == [hashlib.sha256(current).hexdigest()]
    assert len(result["files"]) == len(history) + 1


def test_cold_inline_only_workspace_retains_every_historical_original(tmp_path):
    history = [png(index) for index in range(deliverables.NATIVE_MEDIA_MAX_ITEMS + 3)]
    result = bundle(request(history))
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_project_files(home, workspace, result, {"owner_id": "synthetic-owner", "tenant_id": "local"},
                         shutil.copyfile, shutil.copytree)
    for data in history:
        path = workspace / "uploads/native-images" / f"{hashlib.sha256(data).hexdigest()}.png"
        assert path.read_bytes() == data
    assert len(list((workspace / "uploads/native-images").iterdir())) == len(history)


def test_repeated_historical_image_is_still_a_current_input():
    history = [png(index) for index in range(deliverables.NATIVE_MEDIA_MAX_ITEMS + 3)]
    result = bundle(request(history, [image(history[5])]))
    assert [item["sha256"] for item in result["native_input_images"]] == [hashlib.sha256(history[5]).hexdigest()]
    assert len(result["files"]) == len(history)


def test_multiple_current_sources_keep_original_image_order():
    history = [png(index) for index in range(deliverables.NATIVE_MEDIA_MAX_ITEMS + 3)]
    payload = request(history, [image(png(200)), {"type": "text", "text": "Current Alpha."}])
    first = len(payload.messages) - 1
    payload.messages.extend([
        provider.ChatMessage(role="assistant", content="Waiting for the other current source."),
        provider.ChatMessage(role="user", content=[image(png(201)), {"type": "text", "text": "Current Bravo."}]),
    ])
    payload.metadata.visible_message_chain = [
        {"id": f"source-{index}", "role": message.role,
         "sha256": hashlib.sha256(provider._visible_authored_text(message.content).encode()).hexdigest(),
         "accepted_source": True, "current_input": index in {first, len(payload.messages)-1}}
        for index, message in enumerate(payload.messages) if message.role == "user"
    ]
    assert [item["sha256"] for item in bundle(payload)["native_input_images"]] == [
        hashlib.sha256(png(200)).hexdigest(), hashlib.sha256(png(201)).hexdigest(),
    ]


def test_aggregate_byte_budget_is_per_current_invocation(monkeypatch):
    history = [png(index) for index in range(3)]
    current = png(200)
    monkeypatch.setattr(deliverables, "NATIVE_MEDIA_MAX_TOTAL_BYTES", max(map(len, [*history, current])))
    result = bundle(request(history, [image(current)]))
    assert len(result["files"]) == 4
    assert [item["sha256"] for item in result["native_input_images"]] == [hashlib.sha256(current).hexdigest()]


def test_malformed_current_image_still_rejects_with_long_history():
    history = [png(index) for index in range(deliverables.NATIVE_MEDIA_MAX_ITEMS + 3)]
    with pytest.raises(HTTPException) as error:
        bundle(request(history, [image(b"not-image-bytes")]))
    assert error.value.status_code == 400
    assert str(error.value.detail) == "Invalid native inline image bytes"


def test_default_current_image_count_policy_still_rejects():
    current = [image(png(index)) for index in range(deliverables.NATIVE_MEDIA_MAX_ITEMS + 1)]
    with pytest.raises(HTTPException) as error:
        bundle(request([], current))
    assert error.value.status_code == 400
    assert str(error.value.detail) == "Inline image input exceeds the native media limit"


def test_archive_projection_does_not_bypass_per_file_validation():
    with pytest.raises(ValueError, match="Invalid native inline image bytes"):
        upload_projection.project_inline_image_files(
            [{"role": "user", "content": [image(b"not-image-bytes")]}], archive=True,
        )
