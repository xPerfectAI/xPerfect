"""Current conversation evidence follows existing native work controls unchanged."""
import copy
import json

import pytest

from test_account_api import account_client, account_headers, delegation_payload_with_origin
from workers_projects_runtime.workspace_continuation import build_workspace_continuation_context


def inherited():
    return {"version": 1, "conversation_id": "conversation-synthetic",
        "assigned_source_ordinals": [1], "sources": [
            {"id": "after-launch", "role": "user", "text": "The chosen colour is amber."},
            {"id": "current", "role": "user", "text": "Use that one.",
             "current_input": True, "source_ordinals": [1], "assigned_input": True}]}


def action_body(action):
    return {"action": action, "instruction": "Use that one.",
        "idempotencyKey": "inherited-context-operation",
        "sourceContext": {"version": 1, "originRef": "ghi_synthetic_origin_0001",
            "sourceEventId": "event-current", "sourceRevision": 1,
            "surface": "voice", "outputContract": {"mode": "inherit"}},
        "inheritedConversation": inherited()}


@pytest.mark.parametrize("action", ["queue", "message", "steer", "retry"])
def test_current_context_follows_native_control_and_exact_receipt(account_client, action):
    service, store = account_client.app.state.service, account_client.app.state.store
    service.executor.submit = lambda *args, **kwargs: None
    accepted = account_client.post("/v1/delegations",
        headers=account_headers(idempotency_key="inherited-delegation"),
        json=delegation_payload_with_origin()).json()
    before = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    if action == "retry":
        store.finalize_run(before["current_run_id"], "failed", error_text="Temporary outage",
            failure_class="provider_transport_error", failure_retryable=1, failure_structured=1)
    body = action_body(action)
    path = f"/v1/work/{accepted['workRef']}/actions"
    response = account_client.post(path, headers=account_headers(), json=body)
    assert response.status_code == 202, response.text
    after = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    run = store.get_run(after["current_run_id"])
    context = json.loads(run["continuation_context_json"])
    assert context["inherited_conversation"] == inherited()
    assert context["guidance"] == ["Use that one."]
    assert "amber" not in run["instruction"]
    assert "inherited_conversation" not in json.loads(store.get_worker(after["worker_id"])["bootstrap_bundle_json"])
    receipt = account_client.post(path + "/receipt", headers=account_headers(), json=body)
    assert receipt.status_code == 202, receipt.text
    altered = copy.deepcopy(body)
    altered["inheritedConversation"]["sources"][0]["text"] = "Different evidence."
    mismatch = account_client.post(path + "/receipt", headers=account_headers(), json=altered)
    assert mismatch.status_code == 409, mismatch.text
    replay = account_client.post(path, headers=account_headers(), json=body)
    assert replay.status_code == 202, replay.text
    assert store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")["current_run_id"] == run["run_id"]


def test_context_survives_later_continuation_without_becoming_guidance():
    prior = {"instruction": "Create a colour chart.", "continuation_context_json": json.dumps({
        "version": 1, "base_instruction": "Create a colour chart.", "guidance": ["Use that one."],
        "inherited_conversation": inherited()})}
    result = build_workspace_continuation_context(previous_run=prior, continuation_goal="Continue.")
    assert result["inherited_conversation"] == inherited()
    assert result["guidance"] == ["Use that one.", "Continue."]


@pytest.mark.parametrize("change", ["no_source", "wrong_action", "bad_source", "wrong_owner"])
def test_context_cannot_bypass_existing_owner_and_action_authority(account_client, change):
    service = account_client.app.state.service
    service.executor.submit = lambda *args, **kwargs: None
    accepted = account_client.post("/v1/delegations",
        headers=account_headers(idempotency_key="inherited-authority"),
        json=delegation_payload_with_origin()).json()
    body = action_body("message")
    headers = account_headers()
    if change == "no_source": body.pop("sourceContext")
    if change == "wrong_action": body["action"] = "pause"
    if change == "bad_source": body["inheritedConversation"]["sources"][0]["text"] = 3
    if change == "wrong_owner": headers = account_headers(owner_id="owner-b")
    response = account_client.post(f"/v1/work/{accepted['workRef']}/actions", headers=headers, json=body)
    assert response.status_code == (404 if change == "wrong_owner" else 422), response.text
