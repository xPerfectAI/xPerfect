"""Native presentation uses offered labels and existing model-authored purpose."""
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from workers_projects_runtime.provider_native_input import project_native_request, native_input_state


def project(request):
    return project_native_request(request, run_id="run-synthetic", attempt_id="attempt-synthetic", session_id="session-synthetic")


def question(key="Which shape?", options=None, multi=False):
    return {"request_id": "question-synthetic", "expires_at": time.time() + 60,
        "method": "x.ai/ask_user_question", "request": {"sessionId": "session-synthetic",
        "questions": [{"question": key, "options": [{"label": x} for x in (options if options is not None else ["Circle", "Square"])], "multiSelect": multi}]}}


def permission(description="Check the selected file"):
    call = {"kind": "execute", "title": "Execute `test -f selected.txt`", "rawInput": {"command": "test -f selected.txt"},
        "_meta": {"x.ai/tool": {"version": 1, "kind": "execute", "name": "run_terminal_command", "input": {"description": description}}}}
    return {"request_id": "permission-synthetic", "expires_at": time.time() + 60,
        "method": "session/request_permission", "request": {"sessionId": "session-synthetic", "toolCall": call,
        "options": [{"optionId": "allow-once", "name": "Yes, proceed", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "No, stop", "kind": "reject_once"}]}}


def test_question_presentation_offers_choices_without_changing_answer_keys():
    request = question()
    before = json.dumps(request, sort_keys=True)
    result = project(request)
    assert result["prompt"] == "Which shape?\nCircle; Square; Other."
    assert result["requestedSchema"]["properties"]["answers"]["properties"]["Which shape?"]["items"]["enum"] == ["Circle", "Square", "Other"]
    assert result["requestFingerprint"] == hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert json.dumps(request, sort_keys=True) == before


def test_multi_question_presentation_keeps_each_question_and_offered_values():
    request = question(multi=True)
    request["request"]["questions"].append({"question": "Which size?", "options": [{"label": "Small"}], "multiSelect": False})
    result = project(request)
    assert result["prompt"] == "Which shape?\nCircle; Square; Other.\nWhich size?\nSmall; Other."
    fields = result["requestedSchema"]["properties"]["answers"]["properties"]
    assert fields["Which shape?"]["maxItems"] == 3
    assert fields["Which size?"]["maxItems"] == 1


def test_free_text_native_question_keeps_original_prompt():
    assert project(question(options=[]))["prompt"] == "Which shape?"


def test_original_valid_question_at_cap_is_not_rejected_or_truncated():
    key = "Q? " * 2666 + "Q?"
    result = project(question(key))
    assert result["prompt"] == key
    assert result["requestedSchema"]["properties"]["answers"]["properties"][key]["items"]["enum"] == ["Circle", "Square", "Other"]


def test_permission_presentation_uses_native_purpose_and_retains_command_detail():
    request = permission()
    original = json.dumps(request, sort_keys=True)
    result = project(request)
    assert result["prompt"] == "Check the selected file\nYes, proceed; No, stop."
    assert result["operationTitle"] == "Execute `test -f selected.txt`"
    assert result["choices"] == [{"value": "allow-once", "label": "Yes, proceed", "kind": "allow_once"}, {"value": "reject-once", "label": "No, stop", "kind": "reject_once"}]
    assert json.dumps(request, sort_keys=True) == original
    assert request["request"]["toolCall"]["rawInput"]["command"] == "test -f selected.txt"


@pytest.mark.parametrize("invalid", ["missing", "version", "boolean_version", "kind", "unknown_tool", "description_type", "description_oversized"])
def test_permission_without_supported_native_purpose_keeps_existing_title(invalid):
    request = permission()
    call = request["request"]["toolCall"]
    meta = call["_meta"]["x.ai/tool"]
    if invalid == "missing": call.pop("_meta")
    elif invalid == "version": meta["version"] = 2
    elif invalid == "boolean_version": meta["version"] = True
    elif invalid == "kind": meta["kind"] = "read"
    elif invalid == "unknown_tool": meta["name"] = "unregistered_tool"
    elif invalid == "description_type": meta["input"]["description"] = ["invented"]
    else: meta["input"]["description"] = "X" * 8001
    result = project(request)
    assert result["prompt"] == "Execute `test -f selected.txt`\nYes, proceed; No, stop."


def test_permission_at_cap_keeps_original_valid_prompt_and_choices():
    title = "T? " * 2666 + "T?"
    request = permission("D? " * 2666 + "D?")
    request["request"]["toolCall"]["title"] = title
    result = project(request)
    assert result["prompt"] == title
    assert [item["value"] for item in result["choices"]] == ["allow-once", "reject-once"]


@pytest.mark.parametrize("at_cap", [False, True])
def test_foreground_choice_envelope_keeps_purpose_title_and_exact_choices(at_cap):
    request = permission()
    title = "T? " * 2666 + "T?" if at_cap else request['request']['toolCall']['title']
    request['request']['toolCall']['title'] = title
    run = {'run_id': 'run-synthetic', 'state': 'running', 'worker_id': 'worker-synthetic', 'active_attempt_id': 'attempt-synthetic'}
    worker = {'worker_id': 'worker-synthetic', 'owner_id': 'owner-synthetic', 'tenant_id': 'tenant-synthetic', 'profile': 'grok-build'}
    provider = SimpleNamespace(store=SimpleNamespace(get_run=lambda *args, **kwargs: run, get_worker=lambda *args: worker),
        service=SimpleNamespace(native_worker_control=lambda *args, **kwargs: {'run_id': run['run_id'],
            'attempt_id': run['active_attempt_id'], 'session_id': 'session-synthetic', 'pending_requests': [request]}))
    record = {'state': 'running', 'run_id': run['run_id'], **{key: worker[key] for key in ('owner_id', 'tenant_id')}}
    pending = native_input_state(provider, record)['pending'][0]
    assert pending['prompt'] == 'Check the selected file\nYes, proceed; No, stop.'
    assert pending['operationTitle'] == title and 'kind' not in pending
    assert pending['choices'] == project(request)['choices']
    assert pending['requestFingerprint'] == project(request)['requestFingerprint']
