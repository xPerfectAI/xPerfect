"""Preserve accepted stateless work and exact authorization retries across restart."""
from __future__ import annotations
import json
import time
import pytest
from workers_projects_runtime.conversation_provider import ChatCompletionRequest
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.store import Store
from test_conversation_provider import _client, _payload, lifecycle_owned_test_clients

def _truthfully_invoke_conversation_run(
    store: Store, worker: dict, run: dict
) -> dict:
    executor_id = "conversation-test-running-fixture"
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=executor_id
    )
    assert claimed is not None and claimed["run_id"] == run["run_id"]
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="conversation",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    assert store.admit_claimed_run(
        run["run_id"], lease_id=lease["lease_id"], executor_id=executor_id
    )
    invoked = store.mark_run_runtime_invoked(
        run["run_id"], lease_id=lease["lease_id"], executor_id=executor_id
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=executor_id,
        identity_kind="in_process",
        pid=None,
        process_group=None,
        process_start_identity="",
        container_id="",
        session_id="in-process",
    )
    assert confirmed is not None
    return confirmed["run"]



class BrokerBundleCaptureRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.run_bundles: list[dict] = []
        self.instructions: list[str] = []

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = timeout_sec, run_id
        super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)
        self.run_bundles.append(json.loads(str(worker["bootstrap_bundle_json"])))
        self.instructions.append(instruction)
        return "Broker-backed conversation completed."



def test_restart_settles_lost_authority_backlog_and_fresh_stateless_turn_runs(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    first_runtime = BrokerBundleCaptureRuntime()
    first_client = _client(tmp_path, monkeypatch, runtime=first_runtime)
    first_service = first_client.app.state.service
    first_store = first_client.app.state.store
    first_provider = first_client.app.state.conversation_provider
    monkeypatch.setattr(first_service, "_ensure_worker_processor", lambda _worker_id: None)

    old_payload = _payload(workspace)
    old_payload["metadata"]["provider_session_mode"] = "stateless"
    old_payload["metadata"]["bootstrap_bundle"] = {
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "old-lost-bearer"},
        "glasshive_capability_broker": {
            "authority_kind": "conversation_orchestrator",
            "allowed_host_tools": ["active_work"],
        },
    }
    old_request = first_provider.start(
        ChatCompletionRequest.model_validate(old_payload)
    )
    old_run_id = str(old_request["run_id"])
    session = first_store.get_provider_session_by_id(str(old_request["session_id"]))
    worker = first_store.get_worker(str(session["worker_id"]))
    old_run = first_store.get_run(old_run_id)
    assert worker is not None and old_run is not None

    _truthfully_invoke_conversation_run(first_store, worker, old_run)
    first_store.update_worker_state(str(worker["worker_id"]), "running")
    old_lease = first_store.get_active_host_run_lease_for_run(old_run_id)
    assert old_lease is not None
    assert first_store.release_host_run_lease(
        str(old_lease["lease_id"]),
        executor_id="conversation-test-running-fixture",
        reason="synthetic_full_stack_restart",
    )
    assert first_store.reconcile_invalid_running_runs() == 1
    assert first_store.mark_run_needs_input(
        old_run_id,
        expected_state="queued",
        error_text="The conversation capability grant must be refreshed.",
        failure_class="conversation_capability_grant_required",
        failure_user_message="The conversation capability grant must be refreshed.",
    )
    first_store.update_worker_state(str(worker["worker_id"]), "needs_input")

    sibling_request, _ = first_store.create_provider_request(
        tenant_id="local",
        owner_id="owner-a",
        session_id=str(session["session_id"]),
        idempotency_key="idem-lost-sibling",
        message_id="message-lost-sibling",
        stream_id="stream-lost-sibling",
        requested_history_count=2,
        replay_decision={"provider_session_mode": "stateless"},
    )
    sibling_bundle = {
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "sibling-lost-bearer"},
        "glasshive_capability_broker": {
            "authority_kind": "conversation_orchestrator",
            "allowed_host_tools": ["active_work"],
        },
    }
    sibling_run = first_service.assign_run(
        str(worker["worker_id"]),
        "This sibling was accepted just before the full-stack restart.",
        start_processor=False,
        run_local_bundle=sibling_bundle,
    )
    first_store.update_provider_request(
        str(sibling_request["request_id"]),
        run_id=str(sibling_run["run_id"]),
        state="queued",
    )
    assert first_service.has_run_local_bundle(str(sibling_run["run_id"]))
    first_service.shutdown()

    restarted_runtime = BrokerBundleCaptureRuntime()
    restarted = _client(tmp_path, monkeypatch, runtime=restarted_runtime)
    store = restarted.app.state.store
    provider = restarted.app.state.conversation_provider
    service = restarted.app.state.service
    deadline = time.time() + 3
    while time.time() < deadline:
        durable_old_request = store.get_provider_request(str(old_request["request_id"]))
        durable_sibling_request = store.get_provider_request(
            str(sibling_request["request_id"])
        )
        durable_sibling_run = store.get_run(str(sibling_run["run_id"]))
        if (
            durable_old_request
            and durable_old_request["state"] == "failed"
            and durable_sibling_request
            and durable_sibling_request["state"] == "failed"
            and durable_sibling_run
            and durable_sibling_run["state"] == "needs_input"
        ):
            break
        time.sleep(0.01)

    assert store.get_provider_request(str(old_request["request_id"]))["state"] == (
        "failed"
    )
    assert store.get_run(old_run_id)["state"] == "needs_input"
    assert store.get_provider_request(str(sibling_request["request_id"]))[
        "state"
    ] == "failed"
    assert store.get_run(str(sibling_run["run_id"]))["state"] == "needs_input"
    assert store.list_due_retry_worker_ids() == []
    assert restarted_runtime.run_bundles == []
    assert [
        item["event_type"]
        for item in store.list_provider_activity(str(old_request["request_id"]))
    ].count("failed") == 1
    assert [
        item["event_type"]
        for item in store.list_provider_activity(str(sibling_request["request_id"]))
    ].count("failed") == 1

    fresh_payload = _payload(workspace)
    fresh_payload["metadata"].update(
        {
            "message_id": "message-fresh-after-restart",
            "idempotency_key": "idem-fresh-after-restart",
            "provider_session_mode": "stateless",
            "bootstrap_bundle": {
                "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "fresh-live-bearer"},
                "glasshive_capability_broker": {
                    "authority_kind": "conversation_orchestrator",
                    "allowed_host_tools": ["active_work"],
                },
            },
        }
    )
    fresh_request = provider.start(
        ChatCompletionRequest.model_validate(fresh_payload)
    )
    fresh_record, fresh_run = provider.wait(str(fresh_request["request_id"]), timeout=3)

    assert fresh_record["state"] == "completed"
    assert fresh_run["state"] == "completed"
    assert restarted_runtime.run_bundles[-1]["env"][
        "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
    ] == "fresh-live-bearer"
    assert restarted_runtime.run_bundles[-1]["env"][
        "GLASSHIVE_PROVIDER_SESSION_MODE"
    ] == "stateless"

    replay_payload = json.loads(json.dumps(old_payload))
    replay_payload["metadata"]["bootstrap_bundle"]["env"] = {
        "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "fresh-replay-bearer"
    }
    replayed = provider.start(
        ChatCompletionRequest.model_validate(replay_payload)
    )
    replay_record, replay_run = provider.wait(str(replayed["request_id"]), timeout=3)

    assert replayed["request_id"] == old_request["request_id"]
    assert replay_record["state"] == "completed"
    assert replay_run["run_id"] == old_run_id
    assert restarted_runtime.run_bundles[-1]["env"][
        "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
    ] == "fresh-replay-bearer"
    database = (tmp_path / "runtime.db").read_bytes().decode("utf-8", errors="ignore")
    for bearer in (
        "old-lost-bearer",
        "sibling-lost-bearer",
        "fresh-live-bearer",
        "fresh-replay-bearer",
    ):
        assert bearer not in database
    service.shutdown()



@pytest.mark.parametrize("blocker", ["persistent", "operator_pause", "active_lease", "work_stop", "compute_release", "paused_run"])
def test_restart_backlog_preserves_other_admission_fences(
    tmp_path,
    monkeypatch,
    blocker,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=BrokerBundleCaptureRuntime())
    service = client.app.state.service
    store = client.app.state.store
    provider = client.app.state.conversation_provider
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    payload = _payload(workspace)
    if blocker != "persistent":
        payload["metadata"]["provider_session_mode"] = "stateless"
    payload["metadata"]["bootstrap_bundle"] = {
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "persistent-lost-bearer"},
        "glasshive_capability_broker": {
            "authority_kind": "conversation_orchestrator",
            "allowed_host_tools": ["active_work"],
        },
    }
    accepted = provider.start(
        ChatCompletionRequest.model_validate(payload)
    )
    session = store.get_provider_session_by_id(str(accepted["session_id"]))
    worker_id = str(session["worker_id"])
    assert store.mark_run_needs_input(
        str(accepted["run_id"]),
        expected_state="queued",
        error_text="The conversation capability grant must be refreshed.",
        failure_class="conversation_capability_grant_required",
        failure_user_message="The conversation capability grant must be refreshed.",
    )
    store.update_worker_state(worker_id, "needs_input")
    assert provider._sync(store.get_provider_request(str(accepted["request_id"])))[
        "state"
    ] == "failed"
    queued = service.assign_run(
        worker_id,
        "This persistent sibling must remain ordered behind user input.",
        start_processor=False,
    )

    if blocker == "operator_pause":
        store.add_event(session["project_id"], worker_id, accepted["run_id"],
                        "worker.paused", "Synthetic operator pause")
    elif blocker == "active_lease":
        store.acquire_host_run_lease(
            runtime_family="codex", lane="conversation", tenant_id="local",
            owner_id="owner-a", worker_id=worker_id, run_id=accepted["run_id"],
            executor_id="synthetic-active-owner", conversation_limit=2,
            mission_limit=64, account_mission_limit=64, tenant_mission_limit=64,
            lease_ttl_s=300,
        )
    elif blocker in {"work_stop", "compute_release"}:
        field = "work_stop_id" if blocker == "work_stop" else "compute_release_token"
        with store._connect() as conn:
            conn.execute(f"UPDATE workers SET {field} = ? WHERE worker_id = ?",
                         ("synthetic-control-fence", worker_id))
    elif blocker == "paused_run":
        paused = store.create_run(worker_id, session["project_id"],
                                  "Preserve the operator-paused work.", state="queued")
        store.update_run(paused["run_id"], state="paused")

    try:
        assert service.reconcile_restart_authority_backlog_once() == []
        assert (store.get_worker(worker_id) or {})["state"] == "needs_input"
        assert store.claim_next_queued_run(worker_id) is None
        assert (store.get_run(str(queued["run_id"])) or {})["state"] == "queued"
    finally:
        service.shutdown()
