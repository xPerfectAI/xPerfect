"""Causal regression coverage for provider recovery, admission, and deadlines."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
from threading import Event, Lock
import sys
import time
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from workers_projects_runtime.conversation_provider import (
    ChatCompletionRequest,
    ConversationProvider,
    _versioned_idempotency_key,
)
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, StubRuntime
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import RunRestorationState, Store
from test_conversation_provider import lifecycle_owned_test_clients


@pytest.fixture
def failed_request(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    provider = ConversationProvider(store, service)
    project = store.create_project("owner-a", "Synthetic recovery", "Exact scope", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner-a", name="Synthetic worker",
        role="conversation-agent", profile="codex-cli", backend="",
        runtime="codex-cli", model="gpt-5.6-sol",
    )
    session = store.upsert_provider_session(
        tenant_id="local", owner_id="owner-a", conversation_id="synthetic",
        agent_id="agent-a", model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"], worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path), access_mode="workspace",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Exact admitted source", state="queued")
    store.update_run(run["run_id"], state="failed", failure_class="provider_context_limit_exceeded", failure_structured=1)
    request, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=session["session_id"],
        idempotency_key="synthetic-recovery", message_id="message-a", stream_id="stream-a",
        requested_history_count=1, admitted_instruction="Exact admitted source",
    )
    request = store.update_provider_request(request["request_id"], run_id=run["run_id"], state="running")
    try:
        yield store, service, provider, request
    finally:
        provider.shutdown()
        service.shutdown()
        store.close()


def test_sync_starts_one_context_recovery(failed_request, monkeypatch):
    store, _, provider, request = failed_request
    assert provider._context_recovery_eligible(request, store.get_run(request["run_id"]), set())
    reached = []
    monkeypatch.setattr(provider, "_start_context_recovery", lambda *args, **kwargs: reached.append(True) or request)
    provider._sync(request)
    assert reached == [True]


def _released_input_successor(failed_request):
    store, service, provider, previous = failed_request
    old_run = store.get_run(previous["run_id"])
    worker_id = old_run["worker_id"]
    store.update_run(old_run["run_id"], state="needs_input",
                     failure_class="provider_progress_stalled", failure_structured=1)
    store.update_provider_request(previous["request_id"], state="failed",
                                  replay_decision_json=json.dumps({"provider_session_mode": "stateless"}))
    store.update_worker(worker_id, state="needs_input", trusted_run_lane="conversation",
                        compute_released_at=datetime.now(timezone.utc).isoformat())
    request, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=previous["session_id"],
        idempotency_key="synthetic-successor", message_id="successor-message",
        stream_id="successor-stream", requested_history_count=1,
        admitted_instruction="A new, separately accepted request",
        replay_decision={"provider_session_mode": "stateless"},
        response_timeout_s=120,
        response_deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
    )
    return store, service, provider, previous, request, worker_id


@pytest.mark.parametrize("worker_state", ["needs_input", "paused"])
def test_fresh_request_reactivates_released_input_worker_without_replaying_old_run(
    failed_request, worker_state,
):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    store.update_worker_state(worker_id, worker_state)
    old_run = dict(store.get_run(previous["run_id"]))
    assigned = provider._assign_pre_run_request(request, worker_id=worker_id, run_local_bundle={})
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not store.get_run(assigned["run_id"])["started_at"]:
        time.sleep(0.01)
    successor = store.get_run(assigned["run_id"])
    assert successor["started_at"], {
        "worker": store.get_worker(worker_id)["state"],
        "run": successor["state"], "failure": successor["failure_class"],
    }
    assert successor["instruction"] == request["admitted_instruction"]
    assert assigned["run_id"] != previous["run_id"]
    assert store.get_run(previous["run_id"]) == old_run


def _failed_stateless_session_chain(tmp_path, monkeypatch):
    from test_conversation_provider import _client, _payload
    from test_stateless_restart_preservation import _truthfully_invoke_conversation_run
    client = _client(tmp_path, monkeypatch)
    store, service, provider = client.app.state.store, client.app.state.service, client.app.state.conversation_provider
    payload = _payload(tmp_path)
    payload["metadata"]["provider_session_mode"] = "stateless"
    payload["metadata"]["response_timeout_s"] = 120
    payload["messages"][-1]["content"] = "First accepted source"
    with monkeypatch.context() as setup:
        setup.setattr(service, "_ensure_worker_processor", lambda key: None)
        first = provider.start(ChatCompletionRequest.model_validate(payload))
        first_run = store.get_run(first["run_id"])
        worker_id = first_run["worker_id"]
        worker = store.get_worker(worker_id)
        _truthfully_invoke_conversation_run(store, worker, first_run)
        lease = store.get_active_host_run_lease_for_run(first_run["run_id"])
        assert store.release_host_run_lease(
            lease["lease_id"], executor_id="conversation-test-running-fixture",
            reason="synthetic_provider_stopped",
        )
        assert store.mark_run_needs_input(
            first_run["run_id"], expected_state="running", error_text="Synthetic provider stalled",
            failure_class="provider_progress_stalled", failure_user_message="Synthetic provider stalled",
        )
        store.update_worker_state(worker_id, "needs_input")
        service._release_needs_input_compute(store.get_worker(worker_id), store.get_run(first_run["run_id"]))
        assert store.get_worker(worker_id)["compute_released_at"]
        store.update_worker_state(worker_id, "needs_input")
        assert provider._sync(store.get_provider_request(first["request_id"]))["state"] == "failed"
        # Retain the historical pre-repair state: the successor queued without readiness.
        setup.setattr(service, "activate_prepared_conversation_worker", store.get_worker)
        payload["metadata"].update(message_id="queued-successor", idempotency_key="queued-successor")
        second = provider.start(ChatCompletionRequest.model_validate(payload))
        assert not store.get_run(second["run_id"])["started_at"]
        second = store.update_provider_request(second["request_id"], response_deadline_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        def unavailable_deadline_cleanup(*args, **kwargs):
            raise RuntimeError("Synthetic queued-run cleanup unavailable")
        setup.setattr(service.runtime, "interrupt_worker", unavailable_deadline_cleanup)
        assert provider._expire_response_deadline(second)[0]["state"] == "failed"
        assert store.get_worker(worker_id)["state"] == "needs_input"
        # Retain the actual pre-fix deadline-cleanup false failure for recovery coverage.
        store.update_worker_state(worker_id, "failed")
    retained = {key: dict(store.get_run(key)) for key in [first["run_id"], second["run_id"]]}
    assert store.can_reactivate_released_stateless_provider_session(
        first["session_id"], tenant_id="local", owner_id="owner-a",
    ), {key: store.get_worker(worker_id)[key] for key in (
        "state", "pid", "last_run_id", "compute_released_at", "compute_release_token", "work_stop_id",
    )}
    return store, service, provider, payload, first, second, worker_id, retained


def test_same_stateless_binding_recovers_after_queued_successor_deadline(tmp_path, monkeypatch):
    store, service, provider, payload, first, second, worker_id, retained = _failed_stateless_session_chain(
        tmp_path, monkeypatch,
    )
    requests = {item["request_id"]: dict(store.get_provider_request(item["request_id"]))
                for item in (first, second)}
    monkeypatch.setattr(provider, "_failed_request_has_resumable_run",
                        lambda row: pytest.fail("new source must not resume a failed request"))
    payload["metadata"].update(message_id="fresh-third", idempotency_key="fresh-third")
    third = provider.start(ChatCompletionRequest.model_validate(payload))
    assert third["session_id"] == first["session_id"]
    assert store.get_run(third["run_id"])["worker_id"] == worker_id
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not store.get_run(third["run_id"])["started_at"]:
        time.sleep(0.01)
    assert store.get_run(third["run_id"])["started_at"]
    for key, row in retained.items():
        assert store.get_run(key) == row
    for key, row in requests.items():
        assert store.get_provider_request(key) == row


@pytest.mark.parametrize("mode", ["released", "persistent", "live_lease"])
def test_core_branch_uses_same_released_stateless_session_predicate(tmp_path, monkeypatch, mode):
    import hashlib
    store, service, provider, payload, first, second, worker_id, retained = _failed_stateless_session_chain(
        tmp_path, monkeypatch,
    )
    session = store.get_provider_session_by_id(first["session_id"])
    store.update_worker_state(worker_id, "needs_input")
    manifest = json.loads(session["context_manifest_json"])
    store.update_provider_session_history(session["session_id"], history_count=2,
        context_manifest={**manifest, "native_context_response_key": "msg:prior-answer"})
    parsed = ChatCompletionRequest.model_validate(payload)
    parsed.metadata.main_context_protocol = "main_context_v1"
    parsed.metadata.main_context_owner = "core"
    parsed.metadata.message_id = "selected-branch-answer"
    parsed.metadata.visible_message_chain = [{"id": "selected-question", "role": "user",
        "sha256": hashlib.sha256(payload["messages"][-1]["content"].encode()).hexdigest()}]
    if mode == "persistent":
        parsed.metadata.provider_session_mode = "persistent"
    elif mode == "live_lease":
        reserved = store.create_run(worker_id, session["project_id"], "Synthetic native reservation", state="queued")
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=reserved["run_id"], executor_id="synthetic-live-owner",
            conversation_limit=2, mission_limit=3, account_mission_limit=4, tenant_mission_limit=12,
            lease_ttl_s=300,
        )
    if mode == "released":
        same, reseeded = provider._session(parsed, provider._model(parsed.model), tmp_path, "medium", tenant_id="local")
        assert same["session_id"] == session["session_id"] and reseeded
        assert same["worker_id"] == worker_id
    else:
        with pytest.raises(HTTPException) as failure:
            provider._session(parsed, provider._model(parsed.model), tmp_path, "medium", tenant_id="local")
        assert failure.value.status_code == 409
        assert failure.value.detail["code"] == "conversation_session_authority_conflict"
    for key, row in retained.items():
        assert store.get_run(key) == row


@pytest.mark.parametrize("mode", ["released", "persistent", "live_lease", "foreign_owner", "foreign_session"])
def test_core_branch_after_success_ignores_only_its_released_stateless_failure(tmp_path, monkeypatch, mode):
    import hashlib
    store, service, provider, payload, first, second, worker_id, retained = _failed_stateless_session_chain(
        tmp_path, monkeypatch,
    )
    payload["metadata"].update(message_id="successful-third", idempotency_key="successful-third")
    third = provider.start(ChatCompletionRequest.model_validate(payload))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        completed = provider._sync(store.get_provider_request(third["request_id"]))
        if completed["state"] == "completed" and store.get_worker(worker_id)["state"] == "ready":
            break
        time.sleep(0.01)
    assert completed["state"] == "completed"
    assert store.get_worker(worker_id)["state"] == "ready"
    assert store.get_worker(worker_id)["last_run_id"] == third["run_id"]
    assert not store.can_reactivate_released_stateless_provider_session(
        first["session_id"], tenant_id="local", owner_id="owner-a",
    )
    session = store.get_provider_session_by_id(first["session_id"])
    manifest = json.loads(session["context_manifest_json"])
    store.update_provider_session_history(session["session_id"], history_count=2,
        context_manifest={**manifest, "native_context_response_key": "msg:prior-answer"})
    parsed = ChatCompletionRequest.model_validate(payload)
    parsed.metadata.main_context_protocol = "main_context_v1"
    parsed.metadata.main_context_owner = "core"
    parsed.metadata.message_id = "branch-after-success"
    parsed.metadata.visible_message_chain = [{"id": "selected-question", "role": "user",
        "sha256": hashlib.sha256(payload["messages"][-1]["content"].encode()).hexdigest()}]
    if mode == "persistent":
        parsed.metadata.provider_session_mode = "persistent"
    elif mode == "live_lease":
        reserved = store.create_run(worker_id, session["project_id"], "Synthetic native reservation", state="queued")
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=reserved["run_id"], executor_id="synthetic-live-owner",
            conversation_limit=2, mission_limit=3, account_mission_limit=4, tenant_mission_limit=12,
            lease_ttl_s=300,
        )
    elif mode == "foreign_owner":
        store.update_provider_request(first["request_id"], owner_id="other-owner")
    elif mode == "foreign_session":
        other = store.upsert_provider_session(
            tenant_id="local", owner_id="owner-a", conversation_id="other-conversation",
            agent_id="other-agent", model_id=session["model_id"],
            project_id=session["project_id"], worker_id=worker_id,
            workspace_dir=str(tmp_path), access_mode="workspace",
        )
        store.update_provider_request(first["request_id"], session_id=other["session_id"])
    if mode == "released":
        same, reseeded = provider._session(parsed, provider._model(parsed.model), tmp_path, "medium", tenant_id="local")
        assert same["session_id"] == first["session_id"] and reseeded
        assert same["worker_id"] == worker_id
    else:
        with pytest.raises(HTTPException) as failure:
            provider._session(parsed, provider._model(parsed.model), tmp_path, "medium", tenant_id="local")
        assert failure.value.status_code == 409
        assert failure.value.detail["code"] == "conversation_session_authority_conflict"
    for key, row in retained.items():
        assert store.get_run(key) == row


@pytest.mark.parametrize("change", ["model", "authority", "workspace", "access", "policy", "persistent"])
def test_failed_stateless_worker_does_not_hide_binding_changes(tmp_path, monkeypatch, change):
    store, service, provider, payload, first, second, worker_id, retained = _failed_stateless_session_chain(
        tmp_path, monkeypatch,
    )
    payload["metadata"].update(message_id="changed-third", idempotency_key="changed-third")
    if change == "model":
        payload["model"] = "codex-cli:gpt-6.1-sol"
    elif change == "authority":
        payload["messages"][0]["content"] = "Changed declaration."
    elif change == "workspace":
        workspace = tmp_path / "another-workspace"
        workspace.mkdir()
        payload["metadata"]["glasshive_options"]["workspace"]["path"] = str(workspace)
    elif change == "access":
        payload["metadata"]["glasshive_options"]["access"] = "full"
    elif change == "policy":
        monkeypatch.setattr("workers_projects_runtime.conversation_provider._native_policy_sha256",
                            lambda model: "b" * 64)
    else:
        payload["metadata"]["provider_session_mode"] = "persistent"
    with pytest.raises(HTTPException) as failure:
        provider.start(ChatCompletionRequest.model_validate(payload))
    assert failure.value.status_code == 409
    assert failure.value.detail["code"] == "conversation_session_authority_conflict"
    for key, row in retained.items():
        assert store.get_run(key) == row


@pytest.mark.parametrize("blocker", [
    "stale_release", "no_release", "live_lease", "unconfirmed_start", "live_pid", "operator_pause",
    "work_stop", "release_token", "restore_hold", "persistent", "live_request",
    "foreign_owner", "foreign_session", "mission", "approval", "closed",
])
def test_released_failed_session_requires_current_scope_and_release(failed_request, monkeypatch, blocker):
    store, service, provider, first, second, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=second["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=second["admitted_instruction"],
    )
    store.update_run(run["run_id"], state="failed", failure_class="provider_response_deadline_exceeded")
    store.update_provider_request(second["request_id"], state="failed")
    store.update_worker(worker_id, state="failed", last_run_id=run["run_id"], pid=None)
    assert store.can_reactivate_released_stateless_provider_session(
        first["session_id"], tenant_id="local", owner_id="owner-a",
    )
    if blocker == "stale_release":
        store.update_run(first["run_id"], runtime_invoked_at=(
            datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    elif blocker == "no_release":
        store.update_worker(worker_id, compute_released_at=None)
    elif blocker == "live_lease":
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=first["run_id"], executor_id="synthetic-live-owner",
            conversation_limit=2, mission_limit=3, account_mission_limit=4, tenant_mission_limit=12,
            lease_ttl_s=300,
        )
    elif blocker == "unconfirmed_start":
        monkeypatch.setattr(store, "has_unconfirmed_host_run_start", lambda key: key == worker_id)
    elif blocker == "live_pid":
        store.update_worker(worker_id, pid=1234)
    elif blocker == "operator_pause":
        store.add_event(run["project_id"], worker_id, first["run_id"], "worker.paused", "Synthetic Pause")
    elif blocker in {"work_stop", "release_token"}:
        field = "work_stop_id" if blocker == "work_stop" else "compute_release_token"
        store.update_worker(worker_id, **{field: "synthetic-control"})
    elif blocker == "restore_hold":
        store.update_provider_request(first["request_id"], restore_hold=1)
    elif blocker == "persistent":
        store.update_provider_request(first["request_id"], replay_decision_json="{}")
    elif blocker == "live_request":
        store.update_provider_request(second["request_id"], state="running")
    elif blocker == "foreign_owner":
        store.update_provider_request(first["request_id"], owner_id="different-owner")
    elif blocker == "foreign_session":
        foreign = store.upsert_provider_session(
            tenant_id="local", owner_id="owner-a", conversation_id="foreign-session",
            agent_id="agent-a", model_id="codex-cli:gpt-5.6-sol", project_id=run["project_id"],
            worker_id=worker_id, workspace_dir="/tmp/synthetic", access_mode="workspace",
        )
        store.update_provider_request(first["request_id"], session_id=foreign["session_id"])
    elif blocker == "mission":
        store.update_worker(worker_id, trusted_run_lane="mission")
    elif blocker == "approval":
        store.update_run(first["run_id"], state="paused")
    else:
        store.update_worker_state(worker_id, "terminated")
    assert not store.can_reactivate_released_stateless_provider_session(
        first["session_id"], tenant_id="local", owner_id="owner-a",
    )


@pytest.mark.parametrize("ownership", ["none", "invoked", "attempt", "lease", "unconfirmed"])
def test_queued_deadline_only_skips_cleanup_when_native_never_owned_run(
    failed_request, monkeypatch, ownership,
):
    store, service, provider, first, request, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=request["admitted_instruction"],
    )
    request = store.update_provider_request(request["request_id"], response_deadline_at=(
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    if ownership == "invoked":
        store.update_run(run["run_id"], runtime_invoked_at=datetime.now(timezone.utc).isoformat())
    elif ownership == "attempt":
        store.update_run(run["run_id"], active_attempt_id="synthetic-attempt")
    elif ownership == "lease":
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=run["run_id"], executor_id="synthetic-live-owner",
            conversation_limit=2, mission_limit=3, account_mission_limit=4, tenant_mission_limit=12,
            lease_ttl_s=300,
        )
        assert store.get_active_host_run_lease_for_run(run["run_id"])
    elif ownership == "unconfirmed":
        monkeypatch.setattr(store, "has_unconfirmed_host_run_start", lambda key: key == worker_id)
    calls = []
    def cleanup(worker_id, run_id):
        calls.append(run_id)
        raise RuntimeError("Synthetic cleanup cannot be confirmed")
    monkeypatch.setattr(service, "release_provider_deadline_compute", cleanup)
    result, terminal = provider._expire_response_deadline(request)
    assert result["state"] == "failed"
    assert terminal["failure_class"] == "provider_response_deadline_exceeded"
    assert calls == ([] if ownership == "none" else [run["run_id"]])
    assert store.get_worker(worker_id)["state"] == ("needs_input" if ownership == "none" else "failed")


@pytest.mark.parametrize("guard", [
    "expired", "cancelled", "restore_hold", "paused", "closed", "operator_pause",
    "work_stop", "release_pending", "release_unknown", "start_unconfirmed",
])
def test_released_input_successor_keeps_start_fences(failed_request, monkeypatch, guard):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=request["admitted_instruction"],
    )
    if guard == "expired":
        store.update_provider_request(request["request_id"], response_deadline_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    elif guard == "cancelled":
        store.update_provider_request(request["request_id"], state="cancelled")
    elif guard == "restore_hold":
        store.update_provider_request(request["request_id"], restore_hold=1)
    elif guard in {"paused", "closed"}:
        store.update_worker(worker_id, state="paused" if guard == "paused" else "terminated")
        if guard == "paused":
            store.add_event(store.get_worker(worker_id)["project_id"], worker_id, None,
                            "worker.paused", "Explicit operator pause")
    elif guard == "operator_pause":
        monkeypatch.setattr(store, "has_active_operator_pause", lambda key: key == worker_id)
    elif guard == "work_stop":
        store.update_worker(worker_id, work_stop_id="synthetic-stop")
    elif guard == "release_pending":
        store.update_worker(worker_id, compute_release_token="synthetic-release")
    elif guard == "release_unknown":
        store.update_worker(worker_id, compute_released_at=None)
    else:
        monkeypatch.setattr(store, "has_unconfirmed_host_run_start", lambda key: key == worker_id)
    activations = []
    activate = service.activate_prepared_conversation_worker
    monkeypatch.setattr(service, "activate_prepared_conversation_worker",
                        lambda key: activations.append(key) or activate(key))
    provider._start_assigned_run_before_deadline(
        request["request_id"], run_id=run["run_id"], worker_id=worker_id,
    )
    assert activations == []
    assert not store.get_run(run["run_id"])["started_at"]
    assert store.get_run(previous["run_id"])["state"] == "needs_input"


@pytest.mark.parametrize("blocker", [
    "mission", "persistent", "prior_running", "prior_cancelled", "active_lease",
    "foreign_session", "foreign_owner", "foreign_tenant", "unbound_successor", "approval",
    "successor_persistent", "prior_restore_hold", "successor_restore_hold",
])
def test_stateless_successor_preserves_other_run_admission_fences(failed_request, blocker):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    worker = store.get_worker(worker_id)
    old_run_id = previous["run_id"]
    successor, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=worker["project_id"], instruction=request["admitted_instruction"],
    )
    store.update_worker(worker_id, state="ready")
    if blocker == "mission":
        store.update_worker(worker_id, trusted_run_lane="mission")
    elif blocker == "persistent":
        store.update_provider_request(previous["request_id"], replay_decision_json="{}")
    elif blocker == "prior_running":
        store.update_provider_request(previous["request_id"], state="running")
    elif blocker == "prior_cancelled":
        store.update_provider_request(previous["request_id"], state="cancelled")
    elif blocker == "active_lease":
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=old_run_id, executor_id="synthetic-active-owner",
            conversation_limit=2, mission_limit=3, account_mission_limit=4, tenant_mission_limit=12,
            lease_ttl_s=300,
        )
    elif blocker == "foreign_session":
        other = store.upsert_provider_session(
            tenant_id="local", owner_id="owner-a", conversation_id="different-conversation",
            agent_id="agent-a", model_id="codex-cli:gpt-5.6-sol", project_id=worker["project_id"],
            worker_id=worker_id, workspace_dir="/tmp/synthetic", access_mode="workspace",
        )
        store.update_provider_request(request["request_id"], session_id=other["session_id"])
    elif blocker == "foreign_owner":
        store.update_provider_request(request["request_id"], owner_id="foreign-owner")
    elif blocker == "foreign_tenant":
        store.update_provider_request(request["request_id"], tenant_id="foreign-tenant")
    elif blocker == "unbound_successor":
        store.update_provider_request(request["request_id"], run_id=None)
    elif blocker == "successor_persistent":
        store.update_provider_request(request["request_id"], replay_decision_json="{}")
    elif blocker in {"prior_restore_hold", "successor_restore_hold"}:
        target = previous if blocker == "prior_restore_hold" else request
        store.update_provider_request(target["request_id"], restore_hold=1)
    else:
        store.update_run(old_run_id, state="paused")
    old_run = dict(store.get_run(old_run_id))
    assert store.claim_next_queued_run(worker_id) is None
    assert store.get_run(successor["run_id"])["state"] == "queued"
    assert store.get_run(old_run_id) == old_run


@pytest.mark.parametrize("during_readiness", ["expired", "cancelled"])
def test_released_input_successor_rechecks_request_after_readiness(
    failed_request, monkeypatch, during_readiness,
):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=request["admitted_instruction"],
    )
    activate = service.activate_prepared_conversation_worker
    def prepare_then_end(key):
        result = activate(key)
        if during_readiness == "expired":
            store.update_provider_request(request["request_id"], response_deadline_at=(
                datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        else:
            store.update_provider_request(request["request_id"], state="cancelled")
        return result
    monkeypatch.setattr(service, "activate_prepared_conversation_worker", prepare_then_end)
    result, started = provider._start_assigned_run_before_deadline(
        request["request_id"], run_id=run["run_id"], worker_id=worker_id,
    )
    assert not started
    assert result["state"] == ("failed" if during_readiness == "expired" else "cancelled")
    assert not store.get_run(run["run_id"])["started_at"]


def test_released_input_successor_rejects_foreign_provider_session(failed_request):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=request["admitted_instruction"],
    )
    store.update_worker(worker_id, owner_id="foreign-owner")
    from workers_projects_runtime.service import ParallelExecutionIsolationError
    with pytest.raises(ParallelExecutionIsolationError):
        provider._start_assigned_run_before_deadline(
            request["request_id"], run_id=run["run_id"], worker_id=worker_id,
        )
    assert not store.get_run(run["run_id"])["started_at"]


@pytest.mark.parametrize("control", ["pause", "stop"])
def test_conversation_readiness_serializes_actual_lifecycle_control(
    failed_request, monkeypatch, control,
):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=request["admitted_instruction"],
    )
    entered, release, requested, finished = Event(), Event(), Event(), Event()
    prepare = service.runtime.ensure_worker_ready
    def blocked_readiness(worker):
        entered.set()
        assert release.wait(2)
        return prepare(worker)
    monkeypatch.setattr(service.runtime, "ensure_worker_ready", blocked_readiness)
    def apply_control():
        requested.set()
        try:
            if control == "pause":
                service.pause_worker(worker_id, run_id=run["run_id"])
            else:
                service.stop_run(worker_id, run["run_id"])
            return dict(store.get_worker(worker_id))
        finally:
            finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        activation = pool.submit(service.activate_prepared_conversation_worker, worker_id)
        assert entered.wait(1)
        change = pool.submit(apply_control)
        assert requested.wait(1)
        try:
            finished_during_readiness = finished.wait(0.1)
        finally:
            release.set()
        activation.result(2)
        controlled = change.result(2)
    assert not finished_during_readiness
    current = store.get_worker(worker_id)
    for field in ("state", "pid", "compute_released_at", "work_stop_id", "work_stop_settled_at"):
        assert current[field] == controlled[field]
    assert store.get_run(run["run_id"])["state"] == ("paused" if control == "pause" else "cancelled")


def test_conversation_readiness_does_not_hold_unrelated_provider_cancellation(
    failed_request, monkeypatch,
):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=request["admitted_instruction"],
    )
    other_worker = store.create_worker(
        project_id=run["project_id"], owner_id="owner-a", name="Independent conversation",
        role="conversation-agent", profile="codex-cli", backend="", runtime="codex-cli",
        model="gpt-5.6-sol",
    )
    store.update_worker_state(other_worker["worker_id"], "paused")
    session = store.upsert_provider_session(
        tenant_id="local", owner_id="owner-a", conversation_id="independent",
        agent_id="agent-a", model_id="codex-cli:gpt-5.6-sol", project_id=run["project_id"],
        worker_id=other_worker["worker_id"], workspace_dir="/tmp/synthetic", access_mode="workspace",
    )
    other_request, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=session["session_id"],
        idempotency_key="independent-request", message_id="independent-message",
        stream_id="independent-stream", requested_history_count=1, admitted_instruction="Independent goal",
    )
    other_run, _ = store.create_and_attach_provider_run(
        request_id=other_request["request_id"], worker_id=other_worker["worker_id"],
        project_id=run["project_id"], instruction=other_request["admitted_instruction"],
    )
    entered, release = Event(), Event()
    prepare = service.runtime.ensure_worker_ready
    def blocked_readiness(worker):
        if worker["worker_id"] == worker_id:
            entered.set()
            assert release.wait(2)
        return prepare(worker)
    monkeypatch.setattr(service.runtime, "ensure_worker_ready", blocked_readiness)
    with ThreadPoolExecutor(max_workers=2) as pool:
        activation = pool.submit(provider._start_assigned_run_before_deadline,
                                 request["request_id"], run_id=run["run_id"], worker_id=worker_id)
        assert entered.wait(1)
        cancellation = pool.submit(provider.cancel, other_request["request_id"])
        try:
            cancelled = cancellation.result(1)
            assert cancelled["state"] == "cancelled"
            assert store.get_run(other_run["run_id"])["state"] == "cancelled"
            assert not store.get_run(other_run["run_id"])["started_at"]
        finally:
            release.set()
        activation.result(2)


@pytest.mark.parametrize("outcome", ["failed", "unchanged"])
def test_unsuccessful_conversation_activation_does_not_dispatch(
    failed_request, monkeypatch, outcome,
):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    run, _ = store.create_and_attach_provider_run(
        request_id=request["request_id"], worker_id=worker_id,
        project_id=store.get_worker(worker_id)["project_id"], instruction=request["admitted_instruction"],
    )
    if outcome == "failed":
        def fail_readiness(worker):
            raise RuntimeError("Synthetic readiness failure")
        monkeypatch.setattr(service.runtime, "ensure_worker_ready", fail_readiness)
    else:
        monkeypatch.setattr(service, "activate_prepared_conversation_worker", store.get_worker)
    monkeypatch.setattr(service, "start_assigned_run", lambda key: pytest.fail("activation did not become ready"))
    result, started = provider._start_assigned_run_before_deadline(
        request["request_id"], run_id=run["run_id"], worker_id=worker_id,
    )
    assert not started
    assert result["run_id"] == run["run_id"]
    assert not store.get_run(run["run_id"])["started_at"]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("failure", [None, "copy", "reconcile"])
@pytest.mark.parametrize("sink_throws", [False, True])
def test_request_preparation_timing_preserves_admission_and_failure(
    failed_request, monkeypatch, caplog, enabled, failure, sink_throws,
):
    store, service, provider, previous = failed_request
    monkeypatch.setenv("VIVENTIUM_VOICE_LOG_LATENCY", "true" if enabled else "false")
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    request, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=previous["session_id"],
        idempotency_key="synthetic-preparation", message_id="synthetic-message",
        stream_id="synthetic-stream", requested_history_count=1,
        admitted_instruction="Synthetic private admitted goal",
    )
    worker_id = store.get_provider_session_by_id(previous["session_id"])["worker_id"]
    project_id = store.get_worker(worker_id)["project_id"]
    reached = []
    original_error = RuntimeError("Synthetic private preparation failure")

    def assign(worker, instruction, **options):
        reached.append("assign")
        assert worker == worker_id
        assert instruction == request["admitted_instruction"]
        run, _ = store.create_and_attach_provider_run(
            request_id=options["provider_request_id"], worker_id=worker,
            project_id=project_id, instruction=instruction,
        )
        return run

    remember = provider._remember_request_local_bundle

    def copy_bundle(*args):
        reached.append("copy")
        if failure == "copy":
            raise original_error
        return remember(*args)

    def reconcile():
        reached.append("reconcile")
        if failure == "reconcile":
            raise original_error

    monkeypatch.setattr(service, "assign_run", assign)
    monkeypatch.setattr(provider, "_remember_request_local_bundle", copy_bundle)
    monkeypatch.setattr(service, "reconcile_restart_authority_backlog_once", reconcile)
    monkeypatch.setattr(provider, "_start_assigned_run_before_deadline", lambda *args, **kwargs: reached.append("start"))
    if sink_throws:
        import workers_projects_runtime.native_model_selection as timing

        def rejected_sink(*args, **kwargs):
            raise RuntimeError("Synthetic private sink failure")

        monkeypatch.setattr(timing._logger, "info", rejected_sink)
    if failure:
        with pytest.raises(RuntimeError) as raised:
            provider._assign_pre_run_request(
                request, worker_id=worker_id,
                run_local_bundle={"synthetic_private": "Synthetic private bundle"},
            )
        assert raised.value is original_error
    else:
        result = provider._assign_pre_run_request(
            request, worker_id=worker_id,
            run_local_bundle={"synthetic_private": "Synthetic private bundle"},
        )
        assert result["run_id"]
    expected = ["assign", "copy"]
    if failure != "copy":
        expected.append("reconcile")
    if not failure:
        expected.append("start")
    assert reached == expected
    persisted = store.get_provider_request(request["request_id"])
    assert persisted["admitted_instruction"] == request["admitted_instruction"]
    assert persisted["run_id"]
    stages = ["request_local_bundle_copy", "restart_authority_reconcile"]
    records = [json.loads(record.getMessage().removeprefix("[NativeP0] "))
               for record in caplog.records if record.getMessage().startswith("[NativeP0] ")]
    records = [record for record in records if record["stage"] in stages]
    if not enabled or sink_throws:
        assert records == []
        return
    expected_stages = stages[:1] if failure == "copy" else stages
    assert [record["stage"] for record in records] == expected_stages
    for record in records:
        assert record["durationMs"] >= 0
        assert len(record["requestHash"]) == len(record["runHash"]) == 64
        assert record["status"] == (
            "failed" if record["stage"] == {"copy": stages[0], "reconcile": stages[1]}.get(failure)
            else "completed"
        )
        serialized = json.dumps(record)
        assert "Synthetic private" not in serialized
        assert request["request_id"] not in serialized
        assert persisted["run_id"] not in serialized


def test_stale_fallback_claim_is_terminal(failed_request, monkeypatch):
    store, _, provider, request = failed_request
    store.update_provider_request(request["request_id"], fallback_state="claimed")
    monkeypatch.setattr(
        "workers_projects_runtime.conversation_provider.SERIAL_FALLBACK_CLAIM_TIMEOUT_SEC",
        -1,
    )
    result = provider._sync(request)
    assert (result["state"], result["fallback_state"]) == ("failed", "failed")


def test_unrelated_start_reaches_model_selection_while_first_is_blocked():
    provider = object.__new__(ConversationProvider)
    provider._session_start_locks_guard = Lock()
    provider._session_start_locks = {}
    provider._maybe_apply_retention_policy = lambda: None
    first_entered, second_entered, release = Event(), Event(), Event()

    class StopProbe(Exception):
        pass

    def model(value, *, tenant_id, owner_id):
        assert tenant_id == "local"
        assert owner_id == "owner-a"
        if value == "first":
            first_entered.set()
            assert release.wait(2)
        else:
            second_entered.set()
        raise StopProbe()

    provider._model = model

    def start(value):
        try:
            provider.start(SimpleNamespace(
                model=value,
                metadata=SimpleNamespace(
                    owner_id="owner-a", conversation_id=value, agent_id="agent-a",
                    actor_kind="human", origin="api",
                ),
            ))
        except StopProbe:
            return

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start, "first")
        assert first_entered.wait(1)
        second = pool.submit(start, "second")
        try:
            independent = second_entered.wait(0.25)
        finally:
            release.set()
        first.result(1)
        second.result(1)
    assert independent


def test_new_request_persists_explicit_deadline(failed_request, monkeypatch, tmp_path):
    store, service, provider, request = failed_request
    session = store.get_provider_session_by_id(request["session_id"])
    monkeypatch.setattr("workers_projects_runtime.conversation_provider._resolve_workspace", lambda options: tmp_path)
    def slow_session(*args, **kwargs):
        time.sleep(0.15)
        return session, False
    monkeypatch.setattr(provider, "_session", slow_session)
    monkeypatch.setattr(provider, "_run_local_native_bundle", lambda *args: {})
    native_starts = []
    monkeypatch.setattr(service, "start_assigned_run", lambda *args: native_starts.append(args))
    monkeypatch.setattr(service, "reconcile_restart_authority_backlog_once", lambda: None)
    monkeypatch.setattr(service, "assign_run", lambda *args, **kwargs: pytest.fail("expired request assigned native work"))
    payload = ChatCompletionRequest.model_validate({
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Synthetic exact request"}],
        "metadata": {
            "owner_id": "owner-a", "conversation_id": "synthetic", "agent_id": "agent-a",
            "message_id": "deadline-message", "stream_id": "deadline-stream",
            "idempotency_key": "deadline-request", "response_timeout_s": 0.1,
        },
    })
    result = provider.start(payload)
    assert result["response_timeout_s"] == 0.1
    assert result["response_deadline_at"]
    assert datetime.fromisoformat(result["response_deadline_at"]) <= datetime.now(timezone.utc)
    assert result["state"] == "failed"
    assert result["run_id"] is None
    assert native_starts == []


def test_transient_no_run_request_retries_after_restart_with_original_deadline(
    monkeypatch, tmp_path
):
    store = Store(str(tmp_path / "retry.db"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = store.create_project("owner-a", "Synthetic recovery", "Exact scope", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner-a", name="Synthetic worker",
        role="conversation-agent", profile="codex-cli", backend="",
        runtime="codex-cli", model="gpt-5.6-sol",
    )
    session = store.upsert_provider_session(
        tenant_id="local", owner_id="owner-a", conversation_id="synthetic",
        agent_id="agent-a", model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"], worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path), access_mode="workspace",
    )
    provider = ConversationProvider(store, service)
    monkeypatch.setattr("workers_projects_runtime.conversation_provider._resolve_workspace", lambda _: tmp_path)
    monkeypatch.setattr(provider, "_session", lambda *args, **kwargs: (session, False))
    monkeypatch.setattr(provider, "_run_local_native_bundle", lambda *args, **kwargs: {})
    monkeypatch.setattr(service, "reconcile_restart_authority_backlog_once", lambda: None)
    starts = []
    monkeypatch.setattr(service, "start_assigned_run", lambda worker_id: starts.append(worker_id))
    payload = ChatCompletionRequest.model_validate({
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Exact synthetic input"}],
        "metadata": {
            "owner_id": "owner-a", "conversation_id": "synthetic", "agent_id": "agent-a",
            "message_id": "transient-message", "idempotency_key": "transient-request",
            "response_timeout_s": 30,
        },
    })

    class TransientAdmission(RuntimeError):
        code = "host_capacity"

    monkeypatch.setattr(service, "assign_run", lambda *args, **kwargs: (_ for _ in ()).throw(TransientAdmission()))
    with pytest.raises(TransientAdmission):
        provider.start(payload)
    original = store.get_provider_request(
        tenant_id="local", owner_id="owner-a",
        idempotency_key=_versioned_idempotency_key("transient-request", audio_eligible=False),
    )
    assert original and original["state"] == "queued" and not original["run_id"]
    assert original["response_deadline_at"] and starts == []

    restarted = ConversationProvider(store, service)
    monkeypatch.setattr(restarted, "_run_local_native_bundle", lambda *args, **kwargs: {})
    assignments = []

    def assign(worker_id, instruction, **kwargs):
        assignments.append(kwargs["provider_request_id"])
        run, _ = store.create_and_attach_provider_run(
            request_id=kwargs["provider_request_id"], worker_id=worker_id,
            project_id=session["project_id"], instruction=instruction,
        )
        return run

    monkeypatch.setattr(service, "assign_run", assign)
    try:
        recovered = restarted.start(payload)
        repeated = restarted.start(payload)
        assert recovered["request_id"] == original["request_id"] == repeated["request_id"]
        assert recovered["response_deadline_at"] == original["response_deadline_at"]
        assert recovered["run_id"] == repeated["run_id"]
        assert assignments == [original["request_id"]], (
            recovered["state"], recovered["run_id"], recovered.get("restore_hold"),
            repeated["state"], repeated["run_id"],
        )
        assert starts == [session["worker_id"]]
    finally:
        restarted.shutdown()
        provider.shutdown()
        service.shutdown()
        store.close()


def test_permanent_no_run_admission_is_terminal(failed_request, monkeypatch, tmp_path):
    store, service, provider, _ = failed_request
    session = store.get_provider_session(
        tenant_id="local", owner_id="owner-a", conversation_id="synthetic", agent_id="agent-a"
    )
    monkeypatch.setattr("workers_projects_runtime.conversation_provider._resolve_workspace", lambda _: tmp_path)
    monkeypatch.setattr(provider, "_session", lambda *args, **kwargs: (session, False))
    monkeypatch.setattr(provider, "_run_local_native_bundle", lambda *args, **kwargs: {})
    payload = ChatCompletionRequest.model_validate({
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Exact synthetic input"}],
        "metadata": {
            "owner_id": "owner-a", "conversation_id": "synthetic", "agent_id": "agent-a",
            "message_id": "permanent-message", "idempotency_key": "permanent-request",
        },
    })
    calls = []

    class PermanentAdmission(ValueError):
        code = "configuration_invalid"
        retryable = True  # A generic flag cannot override a permanent typed code.

    def reject(*args, **kwargs):
        calls.append(True)
        raise PermanentAdmission("unsupported exact configuration")

    monkeypatch.setattr(service, "assign_run", reject)
    with pytest.raises(PermanentAdmission):
        provider.start(payload)
    terminal = provider.start(payload)
    assert terminal["state"] == "failed" and not terminal["run_id"]
    assert calls == [True]


def test_deadline_during_run_assignment_fences_native_start(failed_request, monkeypatch, tmp_path):
    store, service, provider, request = failed_request
    session = store.get_provider_session_by_id(request["session_id"])
    monkeypatch.setattr("workers_projects_runtime.conversation_provider._resolve_workspace", lambda options: tmp_path)
    monkeypatch.setattr(provider, "_session", lambda *args, **kwargs: (session, False))
    monkeypatch.setattr(provider, "_run_local_native_bundle", lambda *args: {})
    monkeypatch.setattr(service, "reconcile_restart_authority_backlog_once", lambda: None)
    native_starts = []
    monkeypatch.setattr(service, "start_assigned_run", lambda *args: native_starts.append(args))

    def slow_assignment(worker_id, instruction, **kwargs):
        time.sleep(0.15)
        run = store.create_run(worker_id, session["project_id"], instruction, state="queued")
        store.update_provider_request(kwargs["provider_request_id"], run_id=run["run_id"])
        return run

    monkeypatch.setattr(service, "assign_run", slow_assignment)
    payload = ChatCompletionRequest.model_validate({
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Synthetic exact request"}],
        "metadata": {
            "owner_id": "owner-a", "conversation_id": "synthetic", "agent_id": "agent-a",
            "message_id": "assignment-deadline-message", "stream_id": "assignment-deadline-stream",
            "idempotency_key": "assignment-deadline-request", "response_timeout_s": 0.1,
        },
    })

    result = provider.start(payload)

    assert result["state"] == "failed"
    assert result["run_id"]
    assert store.get_run(result["run_id"])["failure_class"] == "provider_response_deadline_exceeded"
    assert native_starts == []


@pytest.mark.parametrize("late", [False, True])
def test_terminal_native_failure_keeps_correct_deadline_cause(failed_request, late):
    store, _, provider, request = failed_request
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(seconds=1)
    ended_at = now if late else now - timedelta(seconds=2)
    store.update_run(
        request["run_id"], state="failed", ended_at=ended_at.isoformat(),
        failure_class="native_start_denied", failure_structured=0,
    )
    store.update_provider_request(
        request["request_id"], state="running", response_timeout_s=1,
        response_deadline_at=deadline.isoformat(),
    )

    # Activity polling is also a terminal observer; it must classify the
    # deadline before it publishes a failed event, even without a waiter.
    activity = provider.activity_payload(request["request_id"])
    settled, run = provider.wait(request["request_id"], timeout=1)

    assert settled["state"] == "failed"
    assert settled["fallback_state"] == ("deadline_exceeded" if late else "")
    assert run["failure_class"] == (
        "provider_response_deadline_exceeded" if late else "native_start_denied"
    )
    failed_events = [
        event for event in store.list_provider_activity(request["request_id"])
        if event["event_type"] == "failed"
    ]
    assert len(failed_events) == 1
    assert json.loads(failed_events[0]["payload_json"])["failure_class"] == (
        "provider_response_deadline_exceeded" if late else "native_start_denied"
    )
    visible_failed = [item for item in activity["data"] if item["event"] == "failed"]
    assert len(visible_failed) == 1
    assert visible_failed[0]["data"]["failure_class"] == (
        "provider_response_deadline_exceeded" if late else "native_start_denied"
    )


def test_deadline_corrects_a_preexisting_generic_failed_activity(failed_request):
    store, _, provider, request = failed_request
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(seconds=1)
    store.update_run(
        request["run_id"], state="failed", ended_at=now.isoformat(),
        failure_class="native_start_denied", failure_structured=0,
    )
    store.update_provider_request(
        request["request_id"], response_timeout_s=1,
        response_deadline_at=deadline.isoformat(),
    )
    store.commit_provider_request_terminal(
        request["request_id"], expected_run_id=request["run_id"], state="failed",
        summary="Failed", activity_payload={"failure_class": "native_start_denied"},
    )

    activity = provider.activity_payload(request["request_id"])

    visible_failed = [item for item in activity["data"] if item["event"] == "failed"]
    assert len(visible_failed) == 1
    assert visible_failed[0]["data"]["failure_class"] == "provider_response_deadline_exceeded"
    assert store.get_provider_request(request["request_id"])["fallback_state"] == "deadline_exceeded"


@pytest.mark.parametrize("delay_inside_file_guard", [False, True])
def test_delayed_executor_cannot_start_after_provider_deadline(
    failed_request, monkeypatch, delay_inside_file_guard
):
    store, service, provider, prior = failed_request
    worker_id = store.get_run(prior["run_id"])["worker_id"]
    worker = store.get_worker(worker_id)
    run = store.create_run(
        worker_id, worker["project_id"], "Never launch late",
        state=RunRestorationState.RUNNING,
    )
    request, created = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=prior["session_id"],
        idempotency_key="delayed-executor", message_id="delayed-message",
        stream_id="delayed-stream", requested_history_count=1,
    )
    assert created
    store.update_provider_request(
        request["request_id"], run_id=run["run_id"], state="running",
        response_timeout_s=0.05,
        response_deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=0.05)).isoformat(),
    )
    monkeypatch.setattr(service, "_processor_is_current", lambda *args: True)
    monkeypatch.setattr(
        service.runtime, "interrupt_worker",
        lambda *args, **kwargs: pytest.fail("prelaunch deadline tried native interrupt"),
    )
    if delay_inside_file_guard:
        @contextmanager
        def delayed_file_guard(_worker):
            time.sleep(0.07)  # Storage/file lock admission can also wait.
            yield
        monkeypatch.setattr(service.files, "native_start_guard", delayed_file_guard)
    else:
        time.sleep(0.07)  # An admitted run can wait in the worker executor.
    assert store.get_worker(worker_id)["state"] == "running"
    assert store.get_run(run["run_id"])["state"] == "running"

    with pytest.raises(RuntimeErrorBase, match="deadline expired"):
        with service._runtime_execution_start_guard(worker_id, 1, run["run_id"]):
            pytest.fail("native start crossed the persisted provider deadline")

    settled = store.get_provider_request(request["request_id"])
    stopped = store.get_run(run["run_id"])
    assert settled["state"] == "failed"
    assert settled["fallback_state"] == "deadline_exceeded"
    assert stopped["state"] == "failed"
    assert stopped["failure_class"] == "provider_response_deadline_exceeded"
    assert any(
        item["event_type"] == "failed"
        for item in store.list_provider_activity(request["request_id"])
    )


def test_restore_held_provider_request_cannot_cross_native_start_fence(failed_request):
    store, _, provider, prior = failed_request
    worker_id = store.get_run(prior["run_id"])["worker_id"]
    worker = store.get_worker(worker_id)
    run = store.create_run(worker_id, worker["project_id"], "Held", state="queued")
    request, created = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=prior["session_id"],
        idempotency_key="held", message_id="held-message", stream_id="held-stream",
        requested_history_count=1,
    )
    assert created
    store.update_provider_request(
        request["request_id"], run_id=run["run_id"], state="running",
        restore_hold=1, response_timeout_s=10,
        response_deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
    )

    assert provider._fence_native_run_start(run["run_id"]) is False
    assert store.get_provider_request(request["request_id"])["state"] == "running"
    assert store.get_run(run["run_id"])["state"] == "queued"


def test_native_supervisor_denies_permit_consumed_after_provider_deadline(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    exit_path = run_root / "exit_code"
    marker = run_root / "native-child-started"
    command = HostCodexCliRuntime._durable_host_process_command(
        object(),
        [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)],
        run_root=run_root,
        exit_path=exit_path,
        response_deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=0.05)).isoformat(),
    )
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    try:
        ready = run_root / "supervisor-ready"
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        time.sleep(0.07)  # The service may grant only after a delayed native handoff.
        (run_root / "start-permit").write_text("start\n")
        _, stderr = process.communicate(timeout=3)
        assert process.returncode == 75, stderr
        assert not marker.exists()
        assert exit_path.read_text().strip() == "75"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)


def test_native_child_whose_identity_lookup_outlasts_the_provider_deadline_never_runs(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    exit_path = run_root / "exit_code"
    marker = run_root / "native-child-started"
    lookup = run_root / "identity-lookup-began"
    child_record = run_root / "native-child-run.json"
    command = HostCodexCliRuntime._durable_host_process_command(
        object(),
        [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)],
        run_root=run_root,
        exit_path=exit_path,
        response_deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        child_record_path=child_record,
    )
    supervisor = Path(command[1])
    source = supervisor.read_text()
    anchor = "def own_start_identity() -> str:\n"
    assert source.count(anchor) == 1
    # The supervisor forks in time; the child's identity lookup then returns just after the deadline.
    supervisor.write_text(source.replace(anchor, anchor + (
        f"    Path({str(lookup)!r}).write_text('began')\n"
        "    while not response_deadline_expired():\n"
        "        time.sleep(0.02)\n"
    )))
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    try:
        ready = run_root / "supervisor-ready"
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        (run_root / "start-permit").write_text("start\n")
        _, stderr = process.communicate(timeout=5)
        assert lookup.exists(), stderr
        assert process.returncode == 75, stderr
        assert "denied after the provider response deadline" in stderr
        assert not marker.exists()
        assert exit_path.read_text().strip() == "75"
        assert json.loads(child_record.read_text())["child"] is None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)


def _duplicate_authority_payload(message: str = "Exact admitted source") -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate({
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": message}],
        "metadata": {
            "owner_id": "owner-a",
            "conversation_id": "synthetic",
            "agent_id": "agent-a",
            "message_id": "duplicate-message",
            "idempotency_key": "duplicate-authority",
        },
    })


def _prepare_no_run_duplicate(store, provider, payload):
    session = store.get_provider_session(
        tenant_id="local",
        owner_id="owner-a",
        conversation_id="synthetic",
        agent_id="agent-a",
    )
    assert session is not None
    request, created = store.create_provider_request(
        tenant_id="local",
        owner_id="owner-a",
        session_id=session["session_id"],
        idempotency_key=_versioned_idempotency_key(
            payload.metadata.idempotency_key, audio_eligible=False
        ),
        message_id=payload.metadata.message_id,
        stream_id="",
        requested_history_count=len(payload.messages),
        replay_decision={},
    )
    assert created is True
    model = provider._model(payload.model)
    effort = provider._effort(payload, model)
    authority = provider._request_authority_sha256(
        payload, model, effort, session_id=session["session_id"]
    )
    store.update_provider_request(
        request["request_id"],
        state="failed",
        replay_decision_json=json.dumps({"request_authority_sha256": authority}),
    )
    return store.get_provider_request(request["request_id"])


def test_duplicate_failed_request_without_run_replays_only_when_authority_is_unchanged(
    failed_request, monkeypatch
):
    store, service, provider, _ = failed_request
    payload = _duplicate_authority_payload()
    duplicate = _prepare_no_run_duplicate(store, provider, payload)
    monkeypatch.setattr(provider, "_run_local_native_bundle", lambda *args: {})
    monkeypatch.setattr(service, "assign_run", lambda *args, **kwargs: pytest.fail("duplicate replay assigned a new run"))

    replayed = provider.start(payload)

    assert replayed["request_id"] == duplicate["request_id"]
    assert replayed["run_id"] is None


def test_no_run_retry_obeys_original_deadline_and_cancel_tombstone(failed_request, monkeypatch):
    store, service, provider, _ = failed_request
    payload = _duplicate_authority_payload()
    duplicate = _prepare_no_run_duplicate(store, provider, payload)
    monkeypatch.setattr(provider, "_run_local_native_bundle", lambda *args, **kwargs: {})
    monkeypatch.setattr(service, "assign_run", lambda *args, **kwargs: pytest.fail("expired or cancelled request assigned work"))
    original_deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.update_provider_request(
        duplicate["request_id"], state="queued",
        admitted_instruction="Exact accepted synthetic input",
        response_timeout_s=1, response_deadline_at=original_deadline,
    )
    expired = provider.start(payload)
    assert expired["state"] == "failed" and not expired["run_id"]
    assert expired["response_deadline_at"] == original_deadline

    other = _duplicate_authority_payload()
    other.metadata.idempotency_key = "cancel-before-retry"
    other.metadata.message_id = "cancel-before-retry-message"
    cancelled = _prepare_no_run_duplicate(store, provider, other)
    store.update_provider_request(
        cancelled["request_id"], state="queued",
        admitted_instruction="Exact accepted synthetic input",
        response_deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
    )
    provider.cancel_by_idempotency("cancel-before-retry", "owner-a")
    with pytest.raises(HTTPException) as blocked:
        provider.start(other)
    assert blocked.value.status_code == 409
    assert not store.get_provider_request(cancelled["request_id"])["run_id"]


def test_detached_no_run_request_expires_without_client_poll(failed_request):
    store, _, provider, _ = failed_request
    payload = _duplicate_authority_payload()
    duplicate = _prepare_no_run_duplicate(store, provider, payload)
    request_id = duplicate["request_id"]
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    store.update_provider_request(
        request_id, state="queued", admitted_instruction="Exact accepted synthetic input",
        response_timeout_s=30, response_deadline_at=deadline,
    )
    assert provider._reconcile_detached_request_once(request_id) is True
    assert store.get_provider_request(request_id)["state"] == "queued"
    store.update_provider_request(
        request_id,
        response_deadline_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert provider._reconcile_detached_request_once(request_id) is False
    assert store.get_provider_request(request_id)["state"] == "failed"
    assert not store.get_provider_request(request_id)["run_id"]


def test_duplicate_failed_request_without_run_rejects_changed_authority_and_missing_digest(
    failed_request, monkeypatch
):
    store, service, provider, _ = failed_request
    payload = _duplicate_authority_payload()
    duplicate = _prepare_no_run_duplicate(store, provider, payload)
    monkeypatch.setattr(provider, "_run_local_native_bundle", lambda *args: {})
    changed = _duplicate_authority_payload("Changed user input")

    with pytest.raises(HTTPException, match="request authority changed"):
        provider.start(changed)

    store.update_provider_request(
        duplicate["request_id"],
        replay_decision_json=json.dumps({}),
    )
    with pytest.raises(HTTPException, match="request authority is unavailable"):
        provider.start(payload)
    assert service.store.get_provider_request(duplicate["request_id"])["run_id"] is None


@pytest.mark.parametrize("release", ["stale", "live_lease", "unconfirmed"])
def test_released_paused_conversation_keeps_fresh_native_release_fence(
    failed_request, monkeypatch, release,
):
    store, service, provider, previous, request, worker_id = _released_input_successor(failed_request)
    store.update_worker_state(worker_id, "paused")
    if release == "stale":
        store.update_run(previous["run_id"], runtime_invoked_at=(
            datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    elif release == "live_lease":
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=previous["run_id"], executor_id="synthetic-live-owner",
            conversation_limit=2, mission_limit=3, account_mission_limit=4, tenant_mission_limit=12,
            lease_ttl_s=300,
        )
    else:
        monkeypatch.setattr(store, "has_unconfirmed_host_run_start", lambda key: key == worker_id)
    old = dict(store.get_run(previous["run_id"]))
    assert service.activate_prepared_conversation_worker(worker_id)["state"] == "paused"
    assert store.get_worker(worker_id)["state"] == "paused"
    assert store.get_run(previous["run_id"]) == old
