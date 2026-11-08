"""New visual input belongs to current sources, not the whole transcript."""
import base64
import hashlib

import pytest
from fastapi import HTTPException

from workers_projects_runtime import conversation_provider as provider


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
OLD = PNG + b"historical-image"
NEW = PNG + b"current-image"


def image(data):
    return {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(data).decode(),
    }}


def request(model, *, text_only=False, current=(3,), declared=True):
    messages = [
        {"role": "system", "content": "Unchanged application authority."},
        {"role": "user", "content": [image(OLD), {"type": "text", "text": "Earlier."}]},
        {"role": "assistant", "content": "Earlier answer."},
        {"role": "user", "content": "Yep" if text_only else [image(NEW), {"type": "text", "text": "Yep"}]},
    ]
    payload = provider.ChatCompletionRequest(
        model=model, messages=messages, metadata={"owner_id": "synthetic-owner"},
    )
    if declared:
        payload.metadata.visible_message_chain = [
            {"id": f"source-{index}", "role": message.role,
             "sha256": hashlib.sha256(provider._visible_authored_text(message.content).encode()).hexdigest(),
             "accepted_source": True, "current_input": index in current}
            for index, message in enumerate(payload.messages) if message.role == "user"
        ]
    return payload


def bundle(payload):
    return provider.ConversationProvider._native_bundle(
        None, payload, provider.GLASSHIVE_MODELS[payload.model], "low",
    )


@pytest.mark.parametrize("model", ["claude-code:claude-opus-5", "codex-cli:gpt-6.1-sol",
                                  "direct-openai:gpt-6.1-sol", "direct-anthropic:claude-opus-5-5"])
@pytest.mark.parametrize("declared", [True, False])
def test_history_photo_is_not_reattached_as_current_input(model, declared):
    result = bundle(request(model, declared=declared))
    assert [item["sha256"] for item in result["native_input_images"]] == [hashlib.sha256(NEW).hexdigest()]
    assert {item["sha256"] for item in result["files"]} == {
        hashlib.sha256(OLD).hexdigest(), hashlib.sha256(NEW).hexdigest(),
    }


@pytest.mark.parametrize("declared", [True, False])
def test_text_only_turn_does_not_resend_historical_photos(declared):
    result = bundle(request("claude-code:claude-opus-5", text_only=True, declared=declared))
    assert not result.get("native_input_images")
    assert result["files"][0]["sha256"] == hashlib.sha256(OLD).hexdigest()


def test_combined_current_sources_keep_both_images_in_source_order():
    result = bundle(request("claude-code:claude-opus-5", current=(1, 3)))
    assert [item["sha256"] for item in result["native_input_images"]] == [
        hashlib.sha256(OLD).hexdigest(), hashlib.sha256(NEW).hexdigest(),
    ]


def test_repeated_current_image_is_not_lost_because_it_is_also_in_history():
    payload = request("claude-code:claude-opus-5", declared=False)
    payload.messages[-1].content[0] = image(OLD)
    assert [item["sha256"] for item in bundle(payload)["native_input_images"]] == [hashlib.sha256(OLD).hexdigest()]


def test_changed_declared_current_source_still_fails_closed():
    payload = request("claude-code:claude-opus-5")
    payload.messages[-1].content[-1]["text"] = "Changed after acceptance."
    with pytest.raises(HTTPException) as error:
        bundle(payload)
    assert error.value.status_code == 413
