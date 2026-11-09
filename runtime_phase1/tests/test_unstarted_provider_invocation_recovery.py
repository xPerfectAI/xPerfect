"""A failed authorization attempt must not own a later conversation turn."""

from datetime import datetime, timezone
import json

import pytest

from test_conversation_provider_g2_g5 import failed_request
from test_conversation_provider import _payload
from workers_projects_runtime.conversation_provider import ChatCompletionRequest, GLASSHIVE_MODELS
from workers_projects_runtime.openclaw_runtime import HostCapacityError
from test_host_run_leases import _normal_host_broker_turn
from test_conversation_executor_authority import _consume


def blocked_invocation(failed_request):
    store, service, provider, request = failed_request
    run = store.get_run(request["run_id"])
    worker_id = run["worker_id"]
    store.update_run(
        run["run_id"], state="needs_input", runtime_invoked_at=None,
        native_session_id="", output_text="", failure_structured=1,
        failure_class="conversation_capability_grant_required", failure_retryable=0,
    )
    store.update_worker(
        worker_id, state="needs_input", execution_mode="host", trusted_run_lane="conversation",
        compute_released_at=datetime.now(timezone.utc).isoformat(),
    )
    request = store.update_provider_request(
        request["request_id"], state="failed",
        replay_decision_json=json.dumps({"provider_session_mode": "persistent"}),
    )
    return store, service, provider, request, worker_id


def test_distinct_turn_settles_only_failed_execution_and_preserves_original_work(failed_request):
    store, service, _, request, worker_id = blocked_invocation(failed_request)
    original = dict(store.get_run(request["run_id"]))
    session = dict(store.get_provider_session_by_id(request["session_id"]))
    result = service.settle_unstarted_provider_invocations(
        request["session_id"], tenant_id="local", owner_id="owner-a",
    )
    assert [run["run_id"] for run in result] == [request["run_id"]]
    settled = store.get_run(request["run_id"])
    assert settled["state"] == "failed" and settled["failure_retryable"] == 1
    for field in ("instruction", "runtime_bundle_json", "continuation_contract_json", "continuation_context_json"):
        assert settled[field] == original[field]
    assert store.get_provider_request(request["request_id"]) == request
    assert store.get_provider_session_by_id(request["session_id"]) == session
    assert store.get_worker(worker_id)["state"] == "ready"
    assert service.settle_unstarted_provider_invocations(
        request["session_id"], tenant_id="local", owner_id="owner-a",
    ) == []


@pytest.mark.parametrize("blocker", ["invoked", "native_session", "output", "tool", "started", "pause", "stop", "restore", "foreign"])
def test_failed_attempt_settlement_keeps_execution_and_authority_fences(failed_request, blocker):
    store, service, _, request, worker_id = blocked_invocation(failed_request)
    run_id = request["run_id"]
    owner_id = "owner-a"
    if blocker == "invoked":
        store.update_run(run_id, runtime_invoked_at=datetime.now(timezone.utc).isoformat())
    elif blocker == "native_session":
        store.update_run(run_id, native_session_id="native-session")
    elif blocker == "output":
        store.update_run(run_id, output_text="A genuine authored answer.")
    elif blocker == "tool":
        store.add_provider_activity(request["request_id"], "tool-call", "A real tool call.")
    elif blocker == "started":
        run = store.get_run(run_id)
        store.add_event(run["project_id"], worker_id, run_id, "run.started", "Native execution started.")
    elif blocker == "pause":
        run = store.get_run(run_id)
        store.add_event(run["project_id"], worker_id, run_id, "worker.paused", "Operator pause.")
    elif blocker == "stop":
        store.update_worker(worker_id, work_stop_id="operator-stop")
    elif blocker == "restore":
        store.update_provider_request(request["request_id"], restore_hold=1)
    elif blocker == "foreign":
        owner_id = "other-owner"
    before = dict(store.get_run(run_id))
    assert service.settle_unstarted_provider_invocations(
        request["session_id"], tenant_id="local", owner_id=owner_id,
    ) == []
    assert store.get_run(run_id) == before
    assert store.get_worker(worker_id)["state"] == "needs_input"


@pytest.mark.parametrize("history", ["capacity", "unknown_invocation", "active_lease", "confirmed_start"])
def test_failed_attempt_settlement_checks_historical_attempts_and_leases(failed_request, history):
    store, service, _, request, worker_id = blocked_invocation(failed_request)
    run_id = request["run_id"]
    if history in {"capacity", "unknown_invocation"}:
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO run_attempts (attempt_id,run_id,attempt_number,state,claimed_at,"
                "runtime_invoked_at,ended_at,terminal_reason) VALUES (?,?,1,'retry_queued',?,?,?,?)",
                ("prior-attempt", run_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00",
                 "2026-01-01T00:00:02+00:00", "host_capacity" if history == "capacity" else "unknown"),
            )
    else:
        lease = store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local", owner_id="owner-a",
            worker_id=worker_id, run_id=run_id, executor_id="synthetic-executor",
            conversation_limit=2, mission_limit=64, account_mission_limit=64,
            tenant_mission_limit=64, lease_ttl_s=300,
        )
        if history == "confirmed_start":
            store.release_host_run_lease(lease["lease_id"], executor_id="synthetic-executor", reason="synthetic_release")
            with store._connect() as conn:
                conn.execute("UPDATE host_run_leases SET startup_confirmed_at=? WHERE lease_id=?",
                             ("2026-01-01T00:00:01+00:00", lease["lease_id"]))
    original = dict(store.get_run(run_id))
    settled = service.settle_unstarted_provider_invocations(
        request["session_id"], tenant_id="local", owner_id="owner-a",
    )
    if history == "capacity":
        assert [run["run_id"] for run in settled] == [run_id]
        with store._connect() as conn:
            assert conn.execute("SELECT terminal_reason FROM run_attempts WHERE attempt_id='prior-attempt'").fetchone()[0] == "host_capacity"
    else:
        assert settled == []
        assert store.get_run(run_id) == original


@pytest.mark.parametrize("authority_changed", [False, True])
def test_distinct_turn_reuses_or_rebinds_only_after_failed_attempt_settles(
    failed_request, tmp_path, monkeypatch, authority_changed,
):
    store, service, provider, request, worker_id = blocked_invocation(failed_request)
    session = store.get_provider_session_by_id(request["session_id"])
    payload = _payload(tmp_path, model=session["model_id"])
    payload["metadata"].update({
        "conversation_id": session["conversation_id"], "agent_id": session["agent_id"],
        "actor_kind": session["actor_kind"], "origin": session["origin"],
        "message_id": "next-message", "stream_id": "next-stream", "idempotency_key": "next-key",
    })
    payload["messages"] = [{"role": "user", "content": "Next original request."}]
    if authority_changed:
        payload["messages"].insert(0, {"role": "system", "content": "New authorized application instructions."})
    created = {"session_id": "new-session"}
    monkeypatch.setattr(provider, "_create_native_session", lambda *args, **kwargs: created)
    result, fresh = provider._session(
        ChatCompletionRequest.model_validate(payload), GLASSHIVE_MODELS[session["model_id"]],
        tmp_path, "low", tenant_id="local",
    )
    assert store.get_run(request["run_id"])["state"] == "failed"
    assert store.get_provider_request(request["request_id"]) == request
    assert fresh is authority_changed
    assert result["session_id"] == ("new-session" if authority_changed else session["session_id"])
    if authority_changed:
        assert store.get_worker(worker_id)["state"] == "terminated"


def test_pre_native_capacity_retry_retains_exact_grant_until_confirmed_start(tmp_path, monkeypatch):
    store, service, runtime, worker_id, run_id, _ = _normal_host_broker_turn(
        tmp_path, "capacity-retry", grant="synthetic-exact-invocation-grant",
    )
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    original = runtime.run_task
    entries = []

    def capacity_then_start(worker, instruction, timeout_sec=None, run_id=None):
        bundle = json.loads(worker["bootstrap_bundle_json"])
        entries.append((run_id, instruction, bundle["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"]))
        if len(entries) == 1:
            raise HostCapacityError("The selected native lane is busy.", capacity_class="family_lane")
        return original(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)

    monkeypatch.setattr(runtime, "run_task", capacity_then_start)
    try:
        _consume(service, worker_id, "_process_worker_queue_parallel")
        assert store.get_run(run_id)["state"] == "queued"
        assert service.has_run_local_bundle(run_id)
        store.update_run(run_id, retry_after=None)
        _consume(service, worker_id, "_process_worker_queue_parallel")
        assert store.get_run(run_id)["state"] == "completed"
        assert len(entries) == 2 and entries[0] == entries[1]
        assert runtime.bearers == ["synthetic-exact-invocation-grant"]
        assert runtime.late_grants == [False]
        assert not service.has_run_local_bundle(run_id)
        assert "synthetic-exact-invocation-grant" not in store.get_worker(worker_id)["bootstrap_bundle_json"]
    finally:
        service.shutdown()
        store.close()
