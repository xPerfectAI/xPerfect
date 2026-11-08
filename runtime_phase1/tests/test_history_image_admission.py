"""Historical photos cannot exhaust a later turn's native image admission."""
import base64
import hashlib
import json
import struct
import zlib

import pytest

from workers_projects_runtime.conversation_provider import _visible_authored_text
from workers_projects_runtime.openclaw_runtime import StubRuntime
from test_conversation_provider import AUTH, _client, _payload, lifecycle_owned_test_clients


def png(index):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + bytes((index, 20, 40))))
            + chunk(b"IEND", b""))


def image(data):
    return {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(data).decode(),
    }}


class RecordingImageRuntime(StubRuntime):
    def __init__(self):
        self.calls = []

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        self.calls.append({"run_id": run_id, "instruction": instruction,
                           "bundle": json.loads(worker["bootstrap_bundle_json"])})
        return super().run_task(worker, instruction, timeout_sec, run_id)


def history_request(workspace, *, current_photo=False, message_count=993):
    payload = _payload(workspace, model="claude-code:claude-opus-5")
    payload["reasoning_effort"] = "low"
    messages = [payload["messages"][0]]
    historical = [png(index) for index in range(27)]
    cursor = 0
    for count in (3, 6, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 5, 2):
        messages.extend([
            {"role": "user", "content": [image(data) for data in historical[cursor:cursor + count]]},
            {"role": "assistant", "content": f"Retained history group {cursor}."},
        ])
        cursor += count
    while len(messages) < message_count - 1:
        messages.append({"role": "user" if len(messages) % 2 else "assistant",
                         "content": f"Retained conversation entry {len(messages)}."})
    text = "\n".join("Compare the options." for _ in range(26))
    text += (" Consider the trade-offs and the next useful step." * 20)[:698 - len(text)]
    assert len(text) == 698 and text.count("\n") == 25
    content = [{"type": "text", "text": text}]
    current = png(80)
    if current_photo:
        content.append(image(current))
    messages.append({"role": "user", "content": content})
    payload["messages"] = messages
    source_hash = hashlib.sha256(_visible_authored_text(content).encode()).hexdigest()
    payload["metadata"].update({
        "surface": "telegram", "provider_session_mode": "persistent",
        "main_context_protocol": "main_context_v1", "main_context_owner": "core",
        "stable_authority_sha256": "a" * 64, "main_context_snapshot_sha256": "b" * 64,
        "main_context_epoch": "c" * 64, "continuity_domain_id": "d" * 64,
        "continuity_agent_id": "agent-main", "logical_turn_id": "turn-current",
        "visible_message_chain": [{"id": "source-current", "role": "user",
                                   "sha256": source_hash, "accepted_source": True,
                                   "current_input": True}],
    })
    return payload, historical, current, text


@pytest.mark.parametrize("current_photo", [False, True])
@pytest.mark.parametrize("message_count", [30, 993])
def test_long_photo_history_admits_current_turn_and_replays_one_run(
    tmp_path, monkeypatch, current_photo, message_count,
):
    monkeypatch.setenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "1")
    runtime = RecordingImageRuntime()
    client = _client(tmp_path, monkeypatch, runtime)
    payload, historical, current, text = history_request(
        tmp_path, current_photo=current_photo, message_count=message_count,
    )
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    store = client.app.state.store
    request = store.get_provider_request(response.json()["id"])
    assert request["state"] == "completed"
    run = store.get_run(request["run_id"])
    assert run["state"] == "completed"
    assert len(runtime.calls) == 1 and runtime.calls[0]["run_id"] == run["run_id"]
    assert text in request["admitted_instruction"]
    decision = json.loads(request["replay_decision_json"])
    assert decision["input_accepted_sources"] == [{
        "id": "source-current", "sha256": payload["metadata"]["visible_message_chain"][0]["sha256"],
    }]
    bundle = runtime.calls[0]["bundle"]
    assert [item["sha256"] for item in bundle.get("native_input_images", [])] == (
        [hashlib.sha256(current).hexdigest()] if current_photo else []
    )
    archived = {item["sha256"] for item in bundle["files"] if "sha256" in item}
    assert {hashlib.sha256(data).hexdigest() for data in historical}.issubset(archived)
    assert len(archived) == 27 + int(current_photo)
    replay = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    assert len(runtime.calls) == 1
    assert store.get_provider_request(request["request_id"])["run_id"] == run["run_id"]
    with store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM provider_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_malformed_current_photo_is_rejected_before_native_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "1")
    runtime = RecordingImageRuntime()
    client = _client(tmp_path, monkeypatch, runtime)
    payload, _, _, _ = history_request(tmp_path, current_photo=True)
    payload["messages"][-1]["content"][-1] = image(b"not-an-image")
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid native inline image bytes"
    assert runtime.calls == []
    with client.app.state.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM provider_requests").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
