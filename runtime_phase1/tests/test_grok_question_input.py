"""Native questions retain their real wire identity through the existing mailbox."""
import json
import time
from threading import Event, Thread

import pytest

from test_grok_mission_input import NativeSession
from workers_projects_runtime.grok_control import ControlMailbox, read_message, write_private, permission_message_id
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.profile_runtime import RuntimeErrorBase
from workers_projects_runtime.provider_native_input import project_native_request, native_question_response


QUESTIONS = [
    {"question": "Which entrance?", "id": "question-id-a", "options": [
        {"label": "North", "id": "option-id-a", "description": "North entrance"},
        {"label": "South", "description": "South entrance"}], "multiSelect": False},
    {"question": "Which checks?", "options": [{"label": "Doors"}, {"label": "Lights"}],
        "multi_select": True},
    {"question": "What note?", "options": []},
]


@pytest.fixture
def question(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path / "runtime"))
    worker = {"worker_id": "worker-question", "owner_id": "owner-a", "tenant_id": "tenant-a",
        "profile": "grok-build", "execution_mode": "host", "_run_attempt_id": "attempt-a"}
    active = {"run_id": "run-a", "attempt_id": "attempt-a", "native_session_id": "native-mission"}
    monkeypatch.setattr(runtime, "_read_active_session", lambda _: active or None)
    monkeypatch.setattr(runtime, "_read_session_key", lambda _: "native-mission")
    root = runtime._control_dir(worker, "run-a", "attempt-a")
    seen, result, events = Event(), {}, []
    def observe(event):
        events.append(event)
        if event["type"] == "grok.permission.requested":
            seen.set()
    mailbox = ControlMailbox(root, run_id="run-a", attempt_id="attempt-a",
        session=NativeSession(), event=observe, permission_timeout=30)
    mailbox.start()
    params = {"sessionId": "native-mission", "toolCallId": "native-tool-a",
        "mode": "default", "questions": QUESTIONS}
    thread = Thread(target=lambda: result.update(response=mailbox.interaction("x.ai/ask_user_question", params)))
    thread.start()
    assert seen.wait(1)
    yield runtime, worker, active, root, mailbox, result, events
    mailbox.cancel_turn()
    mailbox.close()
    thread.join(2)
    assert not thread.is_alive()


def answer(runtime, worker, pending, **kwargs):
    return runtime.respond_native_input(worker, run_id="run-a", request_id=pending["requestId"],
        request_fingerprint=pending["requestFingerprint"], **kwargs)


@pytest.mark.parametrize("content", [
    {"answers": {"Which entrance?": ["North"]}},
    {"answers": {"Which checks?": ["Doors", "Lights"]}},
    {"answers": {"What note?": ["Other"]}, "annotations": {"What note?": {"notes": "Synthetic note."}}},
    {"answers": {"Which entrance?": ["Other"]}},
])
def test_real_question_mailbox_ack_preserves_exact_native_keys_and_partial_answers(question, content):
    runtime, worker, _, root, _, result, _ = question
    pending = runtime.pending_native_input(worker, run_id="run-a")
    assert pending["kind"] == "question"
    assert pending["message"] == "Which entrance?\nNorth; South; Other.\nWhich checks?\nDoors; Lights; Other.\nWhat note?"
    fields = pending["requestedSchema"]["properties"]["answers"]["properties"]
    assert fields["Which entrance?"]["items"]["enum"] == ["North", "South", "Other"]
    assert fields["Which entrance?"]["maxItems"] == 1
    assert fields["Which checks?"]["maxItems"] == 3
    assert "question-id-a" not in fields
    assert answer(runtime, worker, pending, action="accept", content=content) == {"status": "accepted"}
    assert result["response"] == {"outcome": "accepted", **content}
    receipt = read_message(next(root.glob("*.response")))
    assert receipt["status"] == "permission_submitted"
    assert receipt["_request"]["response"] == result["response"]
    assert answer(runtime, worker, pending, action="accept", content=content) == {"status": "already_accepted"}


@pytest.mark.parametrize("content", [
    {"answers": {}}, {"answers": {"Invented?": ["North"]}},
    {"answers": {"Which entrance?": ["Invented"]}},
    {"answers": {"Which entrance?": ["North", "South"]}},
    {"answers": {"Which checks?": ["Doors", "Doors"]}},
    {"answers": {"Which checks?": "Doors"}},
    {"answers": {"Which entrance?": ["Other"]}, "annotations": {"Which entrance?": {"notes": ""}}},
    {"answers": {"Which entrance?": ["North"]}, "annotations": {"Which entrance?": {"notes": "Not Other"}}},
    {"answers": {"Which entrance?": ["Other"]}, "annotations": {"Which entrance?": {"notes": "x", "preview": "unoffered"}}},
    {"answers": {"Which entrance?": ["Other"]}, "annotations": {"Wrong?": {"notes": "x"}}},
    {"answers": {"Which entrance?": ["Other"]}, "annotations": {"Which entrance?": {"notes": "x" * 8001}}},
    {"answers": {"Which entrance?": ["North"]}, "optionId": "allow-once"},
])
def test_invalid_question_response_has_no_native_effect(question, content):
    runtime, worker, _, root, _, result, _ = question
    pending = runtime.pending_native_input(worker, run_id="run-a")
    with pytest.raises(RuntimeErrorBase, match="native_input_invalid"):
        answer(runtime, worker, pending, action="accept", content=content)
    assert result == {}
    assert not list(root.glob("*.request"))
    assert not list(root.glob("*.response"))


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_question_cancel_and_decline_never_grant_operation_permission(question, action):
    runtime, worker, _, _, mailbox, result, _ = question
    pending = runtime.pending_native_input(worker, run_id="run-a")
    assert answer(runtime, worker, pending, action=action) == {"status": "accepted"}
    assert result["response"] == {"outcome": "cancelled"}
    assert mailbox.terminal_cause() == "dismissed"
    assert answer(runtime, worker, pending, action=action) == {"status": "already_accepted"}


def test_provider_end_fences_unanswered_question_and_late_owner_answer(question):
    runtime, worker, _, root, mailbox, _, _ = question
    pending = runtime.pending_native_input(worker, run_id='run-a')
    assert mailbox.finish_unanswered_question() is True
    with pytest.raises(RuntimeErrorBase):
        answer(runtime, worker, pending, action='accept',
               content={'answers': {'Which entrance?': ['North']}})
    assert not list(root.glob('*.response')) or all(
        read_message(path)['status'] == 'rejected' for path in root.glob('*.response'))


@pytest.mark.parametrize('action', ['accept', 'decline', 'cancel'])
def test_confirmed_question_owner_answer_wins_provider_end_fence(question, action):
    runtime, worker, _, root, mailbox, _, _ = question
    pending = runtime.pending_native_input(worker, run_id='run-a')
    content = {'answers': {'Which entrance?': ['North']}} if action == 'accept' else None
    assert answer(runtime, worker, pending, action=action, content=content)['status'] == 'accepted'
    assert mailbox.finish_unanswered_question() is False
    assert answer(runtime, worker, pending, action=action, content=content)['status'] == 'already_accepted'
    assert len(list(root.glob('*.response'))) == 1


def test_owner_stop_wins_question_end_fence_while_native_cancel_is_in_flight(question, monkeypatch):
    _, _, _, _, mailbox, _, events = question
    entered, release = Event(), Event()
    def blocked_cancel():
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(mailbox.session, 'cancel', blocked_cancel)
    thread = Thread(target=mailbox.cancel_turn)
    thread.start()
    try:
        assert entered.wait(1)
        assert mailbox.turn_cancelled() is True
        assert mailbox.finish_unanswered_question() is False
        with mailbox._lock:
            assert all(item['reason'] == 'turn_cancelled' for item in mailbox._permissions.values())
            assert any(not item['resolved'].is_set() for item in mailbox._permissions.values())
    finally:
        release.set(); thread.join(2)
    assert not thread.is_alive()
    deadline = time.monotonic() + 1
    while mailbox.terminal_cause() is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert mailbox.terminal_cause() == 'turn_cancelled'
    assert not any(event.get('reason') == 'provider_ended_without_owner_response' for event in events)


def test_question_end_fence_does_not_classify_pending_permission(tmp_path):
    seen = Event()
    mailbox = ControlMailbox(tmp_path / 'controls', run_id='run-a', attempt_id='attempt-a',
        session=NativeSession(), event=lambda event: seen.set()
            if event['type'] == 'grok.permission.requested' else None)
    mailbox.start()
    thread = Thread(target=lambda: mailbox.permission({'sessionId':'native-mission',
        'options':[{'optionId':'allow-once','kind':'allow_once'}]}))
    thread.start()
    try:
        assert seen.wait(1)
        assert mailbox.finish_unanswered_question() is False
        assert not mailbox.stopped.is_set()
    finally:
        mailbox.cancel_turn(); mailbox.close(); thread.join(2)


def test_question_ack_cannot_replay_a_changed_answer_or_permission_shape(question):
    runtime, worker, _, _, _, _, _ = question
    pending = runtime.pending_native_input(worker, run_id="run-a")
    assert answer(runtime, worker, pending, action="accept", content={"answers": {"Which entrance?": ["North"]}})["status"] == "accepted"
    for content in ({"answers": {"Which entrance?": ["South"]}}, {"optionId": "allow-once"}):
        with pytest.raises(RuntimeErrorBase, match="native_input_conflict"):
            answer(runtime, worker, pending, action="accept", content=content)


@pytest.mark.parametrize("changed", ["run", "attempt", "session", "request", "fingerprint", "stop", "expiry"])
def test_changed_question_generation_or_expiry_never_submits(question, changed):
    runtime, worker, active, root, mailbox, result, events = question
    pending = runtime.pending_native_input(worker, run_id="run-a")
    if changed in {"run", "attempt", "session"}:
        active[{"run": "run_id", "attempt": "attempt_id", "session": "native_session_id"}[changed]] = "different"
    elif changed == "request":
        pending["requestId"] = "different"
    elif changed == "fingerprint":
        pending["requestFingerprint"] = "0" * 64
    elif changed == "stop":
        mailbox.cancel_turn()
    else:
        path = next(root.glob("*.pending"))
        stored = read_message(path); stored["expires_at"] = time.time() - 1; write_private(path, stored)
    with pytest.raises(RuntimeErrorBase, match="native_input_stale"):
        answer(runtime, worker, pending, action="accept", content={"answers": {"Which entrance?": ["North"]}})
    assert not list(root.glob("*.request"))
    receipts = list(root.glob("*.response"))
    if changed == "stop":
        # Stop can leave a rejected delivery receipt, never an accepted answer.
        assert all(read_message(path).get("status") == "rejected" for path in receipts)
        deadline = time.monotonic() + 1
        while "response" not in result and time.monotonic() < deadline:
            time.sleep(0.005)
        assert result == {"response": None}
        cancelled = [event for event in events if event.get("type") == "grok.permission.response_submitted"]
        assert len(cancelled) == 1
        assert cancelled[0]["outcome"] == "cancelled"
        assert cancelled[0]["option_id"] is None
    else:
        assert not receipts


@pytest.mark.parametrize("mutation", ["duplicate", "duplicate-label", "conflicting-multi", "invalid-multi", "invalid-alias", "long-label", "too-many", "wrong-session", "unsupported-method"])
def test_unsupported_question_shapes_are_not_advertised_as_supported(mutation):
    item = {"request_id": "request-a", "expires_at": time.time() + 60,
        "method": "x.ai/ask_user_question", "request": {"sessionId": "native-mission", "questions": json.loads(json.dumps(QUESTIONS))}}
    questions = item["request"]["questions"]
    if mutation == "duplicate": questions.append(questions[0])
    elif mutation == "duplicate-label": questions[0]["options"].append(questions[0]["options"][0])
    elif mutation == "conflicting-multi": questions[0]["multi_select"] = True
    elif mutation == "invalid-multi": questions[0]["multiSelect"] = "true"
    elif mutation == "invalid-alias": questions[0]["multi_select"] = 0
    elif mutation == "long-label": questions[0]["options"][0]["label"] = "x" * 161
    elif mutation == "too-many": item["request"]["questions"] = questions * 6
    elif mutation == "wrong-session": item["request"]["sessionId"] = "different"
    else: item["method"] = "x.ai/exit_plan_mode"
    assert project_native_request(item, run_id="run-a", attempt_id="attempt-a", session_id="native-mission") is None


def test_question_total_response_limit_is_below_private_mailbox_limit():
    content = {"answers": {f"Question {i}?": ["Other"] for i in range(16)},
        "annotations": {f"Question {i}?": {"notes": "x" * 8000} for i in range(16)}}
    with pytest.raises(ValueError, match="native_input_invalid"):
        native_question_response(content)


@pytest.mark.parametrize("content", [
    {"answers": {"Which checks?": ["Doors", "Lights"]}},
    {"answers": {"What note?": ["Other"]}, "annotations": {"What note?": {"notes": "Fixture freeform note."}}},
])
def test_native_acp_reverse_question_roundtrip_keeps_exact_typed_answer(tmp_path, monkeypatch, content):
    from test_grok_acp import peer
    from workers_projects_runtime.grok_acp import AcpClient
    runtime = HostGrokBuildRuntime(str(tmp_path / "runtime"))
    worker = {"worker_id": "worker-acp-question", "_run_attempt_id": "attempt-a"}
    active = {"run_id": "run-a", "attempt_id": "attempt-a", "native_session_id": "native-mission"}
    monkeypatch.setattr(runtime, "_read_active_session", lambda _: active)
    monkeypatch.setattr(runtime, "_read_session_key", lambda _: "native-mission")
    seen, result = Event(), {}
    root = runtime._control_dir(worker, "run-a", "attempt-a")
    mailbox = ControlMailbox(root, run_id="run-a", attempt_id="attempt-a",
        session=NativeSession(), event=lambda event: seen.set()
            if event["type"] == "grok.permission.requested" else None, permission_timeout=5)
    params = {"sessionId": "native-mission", "toolCallId": "native-tool-a", "mode": "default", "questions": QUESTIONS}
    script = "import sys,json\nr=json.loads(sys.stdin.readline())\nprint(json.dumps(" + repr({
        "jsonrpc": "2.0", "id": "native-question-a", "method": "_x.ai/ask_user_question", "params": params}) + "),flush=True)\na=json.loads(sys.stdin.readline())\nprint(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':a['result']}),flush=True)\n"
    mailbox.start()
    try:
        with AcpClient(peer(script), interaction=mailbox.interaction, permission_timeout=5) as client:
            thread = Thread(target=lambda: result.update(native=client.request("fixture", {}, timeout=5)))
            thread.start()
            assert seen.wait(1)
            pending = runtime.pending_native_input(worker, run_id="run-a")
            assert answer(runtime, worker, pending, action="accept", content=content)["status"] == "accepted"
            thread.join(2)
            assert not thread.is_alive()
            assert result["native"] == {"outcome": "accepted", **content}
    finally:
        mailbox.cancel_turn(); mailbox.close()
