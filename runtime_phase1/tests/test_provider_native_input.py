import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from workers_projects_runtime.provider_native_input import native_input_state, submit_native_input


@pytest.fixture
def setup():
    record = {"state": "running", "run_id": "run-a", "tenant_id": "tenant-a", "owner_id": "owner-a"}
    run = {"state": "running", "run_id": "run-a", "worker_id": "worker-a", "active_attempt_id": "attempt-a"}
    worker = {"worker_id": "worker-a", "profile": "grok-build", "tenant_id": "tenant-a", "owner_id": "owner-a"}
    pending = {"request_id": "question-a", "expires_at": time.time() + 60,
        "method": "session/request_permission", "request": {"toolCall": {"title": "Write the requested file", "rawInput": "private arguments"},
        "options": [{"optionId": "allow-a", "name": "Allow once", "kind": "allow_once"}, {"optionId": "deny-a", "name": "Deny", "kind": "reject_once"}]}}
    control = Mock(return_value={"run_id": "run-a", "attempt_id": "attempt-a", "pending_requests": [pending]})
    provider = SimpleNamespace(store=SimpleNamespace(get_run=Mock(return_value=run), get_worker=Mock(return_value=worker)),
        service=SimpleNamespace(native_worker_control=control))
    return provider, record, worker, pending, control


def test_native_input_projection_keeps_choices_and_omits_private_arguments(setup):
    provider, record, _, _, control = setup
    state = native_input_state(provider, record)
    assert state["pending"][0]["choices"][0] == {"value": "allow-a", "label": "Allow once", "kind": "allow_once"}
    assert "private arguments" not in str(state)
    control.assert_called_once_with("worker-a", run_id="run-a", attempt_id="attempt-a")


@pytest.mark.parametrize("input", ["allow-a", "deny-a"])
def test_native_input_submits_only_the_exact_offered_response(setup, input):
    provider, record, _, _, control = setup
    request = native_input_state(provider, record)["pending"][0]
    original = control.return_value
    control.side_effect = [original, {"status": "permission_submitted"}]
    assert submit_native_input(provider, record, {**request, "input": input})["accepted"] is True
    assert control.call_args.kwargs == {"run_id": "run-a", "attempt_id": "attempt-a", "action": "permission", "payload": {"request_id": "question-a", "option_id": input}}


@pytest.mark.parametrize("field,value", [("requestId", "old"), ("runId", "old"), ("attemptId", "old"), ("requestFingerprint", "old"), ("input", "invented")])
def test_stale_or_unoffered_input_causes_no_native_effect(setup, field, value):
    provider, record, _, _, control = setup
    request = native_input_state(provider, record)["pending"][0]
    control.reset_mock()
    with pytest.raises(HTTPException):
        submit_native_input(provider, record, {**request, "input": "allow-a", field: value})
    assert control.call_count == 1
    assert "action" not in control.call_args.kwargs


def test_expired_and_terminal_input_is_not_advertised(setup):
    provider, record, _, pending, control = setup
    pending["expires_at"] = time.time() - 1
    assert native_input_state(provider, record)["pending"] == []
    control.reset_mock()
    assert native_input_state(provider, {**record, "state": "completed"})["pending"] == []
    control.assert_not_called()


@pytest.mark.parametrize("field", ["owner_id", "tenant_id"])
def test_changed_worker_owner_is_rejected(setup, field):
    provider, record, worker, _, control = setup
    worker[field] = "another"
    with pytest.raises(HTTPException):
        native_input_state(provider, record)
    control.assert_not_called()


def test_permission_description_is_complete_including_long_command_tail(setup):
    provider, record, _, pending, _ = setup
    title = "Execute requested command " + "argument " * 70 + "end of command"
    pending["request"]["toolCall"]["title"] = title
    projected = native_input_state(provider, record)["pending"][0]
    assert projected["prompt"] == title + "\nMay I proceed?"
    assert projected["operationTitle"] == title


@pytest.fixture
def question_setup(setup):
    provider, record, worker, pending, control = setup
    pending.update(method="x.ai/ask_user_question", request={"sessionId": "native-a",
        "toolCallId": "tool-a", "questions": [{"question": "Which synthetic room?",
        "options": [{"label": "North"}, {"label": "South"}], "multiSelect": False}]})
    return provider, record, worker, pending, control


def test_foreground_question_reuses_owner_scope_and_string_input_transport(question_setup):
    import json
    provider, record, _, _, control = question_setup
    request = native_input_state(provider, record)["pending"][0]
    assert request["kind"] == "question"
    assert "choices" not in request
    content = {"answers": {"Which synthetic room?": ["North"]}}
    original = control.return_value
    control.side_effect = [original, {"status": "permission_submitted"}]
    assert submit_native_input(provider, record, {**request, "input": json.dumps(content)})["accepted"] is True
    assert control.call_args.kwargs == {"run_id": "run-a", "attempt_id": "attempt-a",
        "action": "permission", "payload": {"request_id": "question-a",
        "request_fingerprint": request["requestFingerprint"], "response": {"outcome": "accepted", **content}}}


@pytest.mark.parametrize("encoded", [
    "not JSON", '["North"]',
    '{"answers":{"Which synthetic room?":["Invented"]}}',
    '{"answers":{"Which synthetic room?":["North","South"]}}',
    '{"answers":{"Which synthetic room?":["Other"]},"annotations":{"Which synthetic room?":{"notes":""}}}',
    '{"answers":{"Which synthetic room?":["North"]},"answers":{"Which synthetic room?":["South"]}}',
    '{"answers":{"Which synthetic room?":["North"],"Which synthetic room?":["South"]}}',
    'x' * (64 * 1024 + 1),
])
def test_foreground_question_invalid_response_never_reaches_native_control(question_setup, encoded):
    provider, record, _, _, control = question_setup
    request = native_input_state(provider, record)["pending"][0]
    control.reset_mock()
    with pytest.raises(HTTPException) as error:
        submit_native_input(provider, record, {**request, "input": encoded})
    assert error.value.status_code == 422
    assert control.call_count == 1
    assert "action" not in control.call_args.kwargs


@pytest.mark.parametrize("field,value", [("requestId", "old"), ("runId", "old"),
    ("attemptId", "old"), ("requestFingerprint", "old")])
def test_foreground_question_stale_identity_causes_no_native_effect(question_setup, field, value):
    import json
    provider, record, _, _, control = question_setup
    request = native_input_state(provider, record)["pending"][0]
    control.reset_mock()
    with pytest.raises(HTTPException) as error:
        submit_native_input(provider, record, {**request, field: value,
            "input": json.dumps({"answers": {"Which synthetic room?": ["North"]}})})
    assert error.value.status_code == 409
    assert control.call_count == 1


def test_foreground_question_expiry_is_not_advertised(question_setup):
    provider, record, _, pending, _ = question_setup
    pending["expires_at"] = time.time() - 1
    assert native_input_state(provider, record)["pending"] == []


def test_foreground_other_is_offered_without_inventing_mandatory_notes(question_setup):
    import json
    provider, record, _, _, control = question_setup
    request = native_input_state(provider, record)["pending"][0]
    original = control.return_value
    control.side_effect = [original, {"status": "permission_submitted"}]
    content = {"answers": {"Which synthetic room?": ["Other"]}}
    assert submit_native_input(provider, record, {**request, "input": json.dumps(content)})["accepted"] is True
    assert control.call_args.kwargs["payload"]["response"] == {"outcome": "accepted", **content}
