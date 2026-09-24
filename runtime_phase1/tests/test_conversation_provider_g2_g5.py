"""Causal regression coverage for provider recovery, admission, and deadlines."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
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

    def model(value):
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
