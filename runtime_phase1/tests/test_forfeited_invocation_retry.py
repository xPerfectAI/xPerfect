"""A requeued turn whose invocation died with its executor fails at once, not at the queue deadline."""
from __future__ import annotations

import json
import time

import pytest

from workers_projects_runtime.conversation_provider import ChatCompletionRequest
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import MANAGED_SHUTDOWN_LEASE_RELEASE_REASON, Store
from test_conversation_provider import _client, _payload, lifecycle_owned_test_clients
from test_host_run_leases import _GrantCaptureRuntime, _normal_host_broker_turn
from test_stateless_restart_preservation import (
    BrokerBundleCaptureRuntime,
    _truthfully_invoke_conversation_run,
)

GRANT = "synthetic-owner-grant"
ORIGINS = [
    {"actor_kind": "system", "origin": "worker"},
    {"actor_kind": "external_user", "origin": "interactive"},
]


def _running_broker_turn(tmp_path, suffix, *, origin=None):
    store, owner, runtime, worker_id, run_id, bundle = _normal_host_broker_turn(
        tmp_path, suffix, grant=GRANT
    )
    if origin is not None:
        bundle["conversation_origin"] = origin
        store.update_worker(worker_id, bootstrap_bundle_json=json.dumps(bundle))
    run = store.get_run(run_id)
    assert store.invocation_executor_for_run(run) == owner._executor_id
    _truthfully_invoke_conversation_run(store, store.get_worker(worker_id), run)
    store.update_worker_state(worker_id, "running")
    return store, owner, runtime, worker_id, run_id


def _lose_dispatch(store, owner, run_id, how):
    """End the owner's dispatch lease the way each kind of executor loss leaves it."""
    lease = store.get_active_host_run_lease_for_run(run_id)
    assert lease is not None and lease["executor_id"] == owner._executor_id
    if how == "expired":
        # A killed executor releases nothing: its lease just stops being renewed.
        with store._connect() as conn:
            conn.execute(
                "UPDATE host_run_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE lease_id = ?",
                (lease["lease_id"],),
            )
    elif how == "stale_owner":
        assert store.reconcile_dead_host_run_lease(
            lease_id=lease["lease_id"], run_id=run_id,
            expected_attempt_id=str(lease["attempt_id"]),
            expected_executor_id=owner._executor_id,
            reason="stale_owner_no_verified_process",
        )
    else:
        assert store.release_host_run_lease(
            lease["lease_id"], executor_id=owner._executor_id, reason=how
        )


def _restart(store):
    """A new process on the same store: its own executor identity and no invocation memory."""
    restarted_store = Store(store.db_path)
    runtime = _GrantCaptureRuntime()
    service = WorkersProjectsService(
        restarted_store, runtime, reconcile_on_startup=False, start_background_consumers=False
    )
    runtime.service = service
    callbacks = []
    service._emit_callback = lambda _worker, event_type, **fields: callbacks.append(
        (event_type, str(fields["run"]["run_id"]), str(fields["run"]["state"]))
    )
    return restarted_store, service, runtime, callbacks


def _consume(service, worker_id):
    with service._processors_lock:
        generation = service._processor_generations.get(worker_id, 0) + 1
        service._processor_generations[worker_id] = generation
        service._active_processors.add(worker_id)
    service._process_worker_queue_parallel(worker_id, generation)


@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize("loss", ["expired", "stale_owner", MANAGED_SHUTDOWN_LEASE_RELEASE_REASON])
def test_restart_fails_the_requeued_turn_of_a_lost_executor(tmp_path, loss, origin):
    store, owner, _runtime, worker_id, run_id = _running_broker_turn(
        tmp_path, "forfeited", origin=origin
    )
    _lose_dispatch(store, owner, run_id, loss)
    # Opening the store is the restarted process's own invariant recovery.
    restarted_store, restarted, restarted_runtime, callbacks = _restart(store)
    try:
        requeued = restarted_store.get_run(run_id)
        assert requeued["state"] == "queued"
        assert requeued["retry_after"]
        assert restarted_store.invocation_executor_for_run(requeued) == owner._executor_id
        restarted.reconcile_all_workers()
        assert restarted_store.get_run(run_id)["state"] == "queued"
        assert restarted_store.list_due_retry_worker_ids() == [worker_id]

        assert restarted.process_due_worker_retries_once() == []

        failed = restarted_store.get_run(run_id)
        assert failed["state"] == "failed"
        assert failed["failure_class"] == "invocation_owner_lost"
        assert (failed["failure_retryable"], failed["failure_structured"]) == (0, 1)
        assert failed["ended_at"] and not failed["retry_after"]
        assert failed["queue_wait_open"] == 0
        assert int(failed["terminal_result_revision"]) >= 1
        assert not failed["runtime_bundle_json"]
        assert restarted_store.list_nonterminal_runs_for_worker(worker_id) == []
        assert restarted_store.get_worker(worker_id)["state"] == "ready"
        assert restarted_store.list_due_retry_worker_ids() == []
        assert restarted_store.get_active_host_run_lease_for_run(run_id) is None
        assert callbacks == [("run.failed", run_id, "failed")]
        terminal_events = [
            event for event in restarted_store.list_events(worker_id)
            if event["run_id"] == run_id and event["event_type"] == "run.failed"
        ]
        assert len(terminal_events) == 1
        assert json.loads(terminal_events[0]["payload_json"]) == {
            "failureCode": "invocation_owner_lost"
        }
        # Nothing ran without the grant, and a second pass has nothing left to settle.
        assert restarted_runtime.bearers == []
        assert restarted.process_due_worker_retries_once() == []
        assert callbacks == [("run.failed", run_id, "failed")]
        assert GRANT not in (store.db_path.read_bytes().decode("utf-8", errors="ignore"))
    finally:
        restarted.shutdown()
        owner.shutdown()
        restarted_store.close()
        store.close()


@pytest.mark.parametrize("partial_output", ["", "One action completed before interruption."])
def test_lost_executor_does_not_claim_prior_actions_are_safe_to_repeat(tmp_path, partial_output):
    store, owner, _runtime, worker_id, run_id = _running_broker_turn(tmp_path, "prior-effect")
    # A lost executor can leave a completed action even without a final authored answer.
    # This local marker is a synthetic action, not a claim about an external provider.
    effect = tmp_path / "synthetic-action.txt"
    effect.write_text("One action completed.\n")
    store.update_run(run_id, output_text=partial_output)
    _lose_dispatch(store, owner, run_id, "expired")
    restarted_store, restarted, restarted_runtime, _callbacks = _restart(store)
    try:
        assert restarted.process_due_worker_retries_once() == []
        failed = restarted_store.get_run(run_id)
        assert failed["state"] == "failed" and failed["failure_retryable"] == 0
        assert failed["output_text"] == partial_output
        assert effect.read_text() == "One action completed.\n"
        assert "Some actions may already have completed" in failed["failure_user_message"]
        assert "Inspect retained results" in failed["failure_recommended_recovery"]
        assert "Do not repeat actions whose outcome is unknown" in failed["failure_recommended_recovery"]
        assert restarted._callback_action_capabilities(
            restarted_store.get_worker(worker_id), {"event": "run.failed", "run_id": run_id},
            {"hmac_secret": "synthetic-callback-key"},
        ) == []
        assert restarted_runtime.bearers == []
    finally:
        restarted.shutdown()
        owner.shutdown()
        restarted_store.close()
        store.close()


def test_sibling_queued_behind_the_lost_turn_is_due_after_it_fails(tmp_path):
    store, owner, _runtime, worker_id, run_id = _running_broker_turn(tmp_path, "sibling")
    _lose_dispatch(store, owner, run_id, "expired")
    restarted_store, restarted, restarted_runtime, _callbacks = _restart(store)
    try:
        sibling = restarted.assign_run(
            worker_id, "A turn accepted after the restart.", start_processor=False,
            run_local_bundle={"env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-fresh-grant"}},
        )
        assert restarted.process_due_worker_retries_once() == []
        assert restarted_store.get_run(run_id)["state"] == "failed"
        assert restarted_store.get_run(sibling["run_id"])["state"] == "queued"
        assert restarted._scheduler_wake_event.is_set()
        assert restarted_store.list_due_retry_worker_ids() == [worker_id]
        _consume(restarted, worker_id)
        assert restarted_store.get_run(sibling["run_id"])["state"] == "completed"
        assert restarted_runtime.bearers == ["synthetic-fresh-grant"]
    finally:
        restarted.shutdown()
        owner.shutdown()
        restarted_store.close()
        store.close()


def test_live_owner_retries_its_own_requeued_turn(tmp_path):
    store, owner, runtime, worker_id, run_id = _running_broker_turn(tmp_path, "live-owner")
    _lose_dispatch(store, owner, run_id, "expired")
    try:
        assert store.reconcile_invalid_running_runs() == 1
        assert owner._fail_forfeited_invocation_retry(worker_id) is False
        assert store.fail_forfeited_invocation_retry(
            run_id, executor_id=owner._executor_id, failure_user_message="m",
            failure_recommended_recovery="r", failure_diagnostic_summary="d",
        ) is None
        assert store.get_run(run_id)["state"] == "queued"
        _consume(owner, worker_id)
        assert store.get_run(run_id)["state"] == "completed"
        assert runtime.bearers == [GRANT]
    finally:
        owner.shutdown()
        store.close()


def test_reattached_invocation_wins_over_the_lost_executor(tmp_path):
    store, owner, _runtime, worker_id, run_id = _running_broker_turn(tmp_path, "reattached")
    _lose_dispatch(store, owner, run_id, "expired")
    restarted_store, restarted, restarted_runtime, callbacks = _restart(store)
    try:
        assert restarted.attach_run_local_bundle(
            run_id, {"env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-replayed-grant"}}
        )
        assert restarted._fail_forfeited_invocation_retry(worker_id) is False
        # The durable proof is gone too: no third executor may fail the re-attached turn.
        assert restarted_store.fail_forfeited_invocation_retry(
            run_id, executor_id="synthetic-third-executor", failure_user_message="m",
            failure_recommended_recovery="r", failure_diagnostic_summary="d",
        ) is None
        _consume(restarted, worker_id)
        assert restarted_store.get_run(run_id)["state"] == "completed"
        assert restarted_runtime.bearers == ["synthetic-replayed-grant"]
        assert [event for event, _run, _state in callbacks] == ["run.completed"]
    finally:
        restarted.shutdown()
        owner.shutdown()
        restarted_store.close()
        store.close()


@pytest.mark.parametrize(
    "fence",
    ["never_started", "owner_requeue", "unproved_release", "operator_pause", "work_stop", "no_broker"],
)
def test_other_queued_turns_keep_their_existing_owner(tmp_path, fence):
    if fence == "never_started":
        store, owner, _runtime, worker_id, run_id, _bundle = _normal_host_broker_turn(
            tmp_path, fence, grant=GRANT
        )
    else:
        store, owner, _runtime, worker_id, run_id = _running_broker_turn(tmp_path, fence)
        _lose_dispatch(
            store, owner, run_id,
            "synthetic_unproved_release" if fence == "unproved_release" else "expired",
        )
    restarted_store, restarted, restarted_runtime, callbacks = _restart(store)
    try:
        worker = restarted_store.get_worker(worker_id)
        if fence == "owner_requeue":
            # The owner itself requeued for capacity; only it knows whether it still waits.
            restarted_store.update_run(
                run_id, failure_class="host_capacity", last_retry_class="host_capacity",
                queue_blocker_class="host_capacity",
            )
        elif fence == "operator_pause":
            restarted_store.add_event(
                worker["project_id"], worker_id, run_id, "worker.paused", "Worker paused"
            )
        elif fence == "work_stop":
            restarted_store.update_worker(worker_id, work_stop_id="synthetic-stop")
        elif fence == "no_broker":
            bundle = json.loads(worker["bootstrap_bundle_json"])
            bundle.pop("glasshive_capability_broker")
            restarted_store.update_worker(worker_id, bootstrap_bundle_json=json.dumps(bundle))
        assert restarted._fail_forfeited_invocation_retry(worker_id) is False
        assert restarted_store.get_run(run_id)["state"] == "queued"
        assert callbacks == []
        assert restarted_runtime.bearers == []
    finally:
        restarted.shutdown()
        owner.shutdown()
        restarted_store.close()
        store.close()


def test_restarted_service_fails_the_lost_turn_and_frees_its_conversation(tmp_path, monkeypatch):
    """The real startup and scheduler settle it; the request fails and the next turn runs."""

    def payload(message_id, bearer):
        body = _payload(tmp_path)
        body["metadata"].update({"message_id": message_id, "idempotency_key": f"idem-{message_id}"})
        body["metadata"]["bootstrap_bundle"] = {
            "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": bearer},
            "glasshive_capability_broker": {"allowed_host_tools": ["active_work"]},
        }
        return ChatCompletionRequest.model_validate(body)

    first = _client(tmp_path, monkeypatch, runtime=BrokerBundleCaptureRuntime())
    first_service, first_store = first.app.state.service, first.app.state.store
    monkeypatch.setattr(first_service, "_ensure_worker_processor", lambda _worker_id: None)
    accepted = first.app.state.conversation_provider.start(payload("message-lost", "synthetic-lost-grant"))
    run_id = str(accepted["run_id"])
    run = first_store.get_run(run_id)
    worker_id = str(run["worker_id"])
    assert first_store.invocation_executor_for_run(run) == first_service._executor_id
    _truthfully_invoke_conversation_run(first_store, first_store.get_worker(worker_id), run)
    first_store.update_worker_state(worker_id, "running")
    # A killed process settles nothing: its threads stop and its lease is never renewed.
    monkeypatch.setattr(first_service, "release_owned_host_run_leases", lambda **_kwargs: 0)
    first_service.shutdown()
    assert first_store.get_run(run_id)["state"] == "running"
    with first_store._connect() as conn:
        conn.execute(
            "UPDATE host_run_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run_id,),
        )

    restarted_runtime = BrokerBundleCaptureRuntime()
    restarted = _client(tmp_path, monkeypatch, runtime=restarted_runtime)
    store, provider = restarted.app.state.store, restarted.app.state.conversation_provider
    deadline = time.time() + 3
    while time.time() < deadline and not (
        store.get_run(run_id)["state"] == "failed"
        and store.get_provider_request(str(accepted["request_id"]))["state"] == "failed"
    ):
        time.sleep(0.01)
    failed = store.get_run(run_id)
    assert (failed["state"], failed["failure_class"]) == ("failed", "invocation_owner_lost")
    assert store.get_provider_request(str(accepted["request_id"]))["state"] == "failed"
    assert store.list_nonterminal_runs_for_worker(worker_id) == []
    assert store.list_due_retry_worker_ids() == []
    assert restarted_runtime.run_bundles == []

    fresh = provider.start(payload("message-next", "synthetic-next-grant"))
    record, fresh_run = provider.wait(str(fresh["request_id"]), timeout=3)
    assert (record["state"], fresh_run["state"]) == ("completed", "completed")
    assert restarted_runtime.run_bundles[-1]["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == (
        "synthetic-next-grant"
    )
