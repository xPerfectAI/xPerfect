"""Real request-store clocks and irreversible boundaries during active polling."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.store import Store


STAMP = "2001-01-01T00:00:00+00:00"


@pytest.fixture
def active_request(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner-a", "Synthetic poll", "Exact scope", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner-a", name="Synthetic worker",
        role="conversation-agent", profile="codex-cli", backend="",
        runtime="codex-cli", model="synthetic-model",
    )
    session = store.upsert_provider_session(
        tenant_id="local", owner_id="owner-a", conversation_id="conversation-a",
        agent_id="agent-a", model_id="codex-cli:synthetic-model",
        project_id=project["project_id"], worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path), access_mode="workspace",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Exact source", state="queued")
    record, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner-a", session_id=session["session_id"],
        idempotency_key="poll-a", message_id="message-a", stream_id="stream-a",
        requested_history_count=1, admitted_instruction="Exact source",
    )
    store.update_provider_request(record["request_id"], run_id=run["run_id"], state="queued")
    store.add_provider_activity_once(record["request_id"], "started", "Synthetic execution started.")
    provider = object.__new__(ConversationProvider)
    provider.store = store
    provider.service = SimpleNamespace(runtime=SimpleNamespace())
    provider._sync_native_activity = lambda *_: None
    # Control only the observed run state; request reads, writes and fences are real Store calls.
    observation = {"state": "queued"}
    get_run = store.get_run
    monkeypatch.setattr(store, "get_run", lambda run_id: {
        **get_run(run_id), "state": observation["state"],
    })
    calls = []
    update = store.update_provider_request_if_state
    monkeypatch.setattr(store, "update_provider_request_if_state", lambda *args, **fields: (
        calls.append(fields.copy()), update(*args, **fields)
    )[1])
    def prepared(state="queued", **fields):
        store.update_provider_request(record["request_id"], state=state, **fields)
        with store._connect() as conn:
            conn.execute("UPDATE provider_requests SET updated_at=? WHERE request_id=?",
                         (STAMP, record["request_id"]))
        return store.get_provider_request(record["request_id"])
    try:
        yield store, provider, observation, calls, prepared
    finally:
        store.close()


@pytest.mark.parametrize("state", ["queued", "running"])
def test_unchanged_attached_poll_preserves_request_clock(active_request, state):
    store, provider, observation, calls, prepared = active_request
    observation["state"] = state
    before = prepared(state)
    for _ in range(3):
        result = provider._sync_locked(before)
        assert result["state"] == state
        assert result["updated_at"] == STAMP
    assert store.get_provider_request(before["request_id"])["updated_at"] == STAMP
    assert calls == []


@pytest.mark.parametrize("before_state,run_state", [("queued", "running"), ("running", "queued")])
def test_changed_active_state_uses_conditional_transition(active_request, before_state, run_state):
    _, provider, observation, calls, prepared = active_request
    observation["state"] = run_state
    result = provider._sync_locked(prepared(before_state))
    assert result["state"] == run_state
    assert result["updated_at"] != STAMP
    assert calls == [{"state": run_state}]


@pytest.mark.parametrize("fields", [
    {"state": "cancelled"}, {"state": "failed", "fallback_state": "deadline_exceeded"},
    {"restore_hold": 1}, {"state": "completed", "response_json": "{}"},
])
def test_fresh_final_read_retains_control_that_wins_during_poll(active_request, monkeypatch, fields):
    store, provider, observation, calls, prepared = active_request
    observation["state"] = "running"
    before = prepared("running")
    activities = store.list_provider_activity
    reads = 0
    def racing_read(*args, **kwargs):
        nonlocal reads
        result = activities(*args, **kwargs)
        reads += 1
        if reads == 2:
            store.update_provider_request(before["request_id"], **fields)
        return result
    monkeypatch.setattr(store, "list_provider_activity", racing_read)
    result = provider._sync_locked(before)
    assert all(result[key] == value for key, value in fields.items())
    assert calls == []


@pytest.mark.parametrize("claim", ["claimed", "context_recovery_claimed"])
def test_active_poll_does_not_extend_claim_clock(active_request, claim):
    _, provider, observation, calls, prepared = active_request
    observation["state"] = "running"
    result = provider._sync_locked(prepared("running", fallback_state=claim))
    assert result["fallback_state"] == claim
    assert result["updated_at"] == STAMP
    assert calls == []
    if claim == "claimed":
        assert provider._fallback_claim_stale(result)


def test_unassigned_request_keeps_expiry_clock(active_request):
    store, provider, _, calls, prepared = active_request
    before = prepared(run_id=None)
    assert provider._sync_locked(before)["updated_at"] == STAMP
    result = store.fail_stale_unassigned_provider_requests(
        updated_before="2002-01-01T00:00:00+00:00", request_id=before["request_id"],
    )
    assert result[0]["state"] == "failed"
    assert result[0]["fallback_state"] == "admission_interrupted"
    assert calls == []


def test_poll_does_not_extend_explicit_deadline(active_request):
    store, provider, observation, _, prepared = active_request
    observation["state"] = "running"
    deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    before = prepared("running", response_timeout_s=30, response_deadline_at=deadline)
    provider._sync_locked(before)
    result = store.arbitrate_provider_request_deadline(
        before["request_id"], default_timeout_s=30, failure_class="provider_response_deadline_exceeded",
        failure_user_message="Synthetic deadline.", failure_recommended_recovery="retry",
        failure_diagnostic_summary="Synthetic elapsed deadline.",
    )
    assert result["request"]["state"] == "failed"
    assert result["request"]["fallback_state"] == "deadline_exceeded"
    assert result["request"]["response_deadline_at"] == deadline


def test_restore_hold_does_not_touch_request_clock(active_request):
    _, provider, observation, calls, prepared = active_request
    observation["state"] = "running"
    result = provider._sync_locked(prepared("running", restore_hold=1))
    assert result["restore_hold"] == 1
    assert result["updated_at"] == STAMP
    assert calls == []
