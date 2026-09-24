from __future__ import annotations

from workers_projects_runtime.store import Store


def _held_provider_request(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite"))
    project = store.create_project("owner", "Continuity", "Hold", "codex-cli")
    worker = store.create_worker(
        project["project_id"], "owner", "Worker", "Hold", "codex-cli", "", "codex-cli", "model"
    )
    session = store.upsert_provider_session(
        tenant_id="local", owner_id="owner", conversation_id="conversation",
        agent_id="agent", model_id="model", project_id=project["project_id"],
        worker_id=worker["worker_id"], workspace_dir=str(tmp_path), access_mode="workspace",
    )
    record, created = store.create_provider_request(
        tenant_id="local", owner_id="owner", session_id=session["session_id"],
        idempotency_key="turn", message_id="turn", stream_id="stream", requested_history_count=0,
    )
    assert created
    run = store.create_run(worker["worker_id"], project["project_id"], "held")
    store.update_provider_request(record["request_id"], run_id=run["run_id"], restore_hold=1, restore_hold_set_hash="receipt")
    return store, record["request_id"], run["run_id"]


def test_held_provider_request_is_read_only_across_terminal_and_fallback_mutations(tmp_path):
    store, request_id, run_id = _held_provider_request(tmp_path)
    try:
        before = store.get_provider_request(request_id)
        assert before["restore_hold"] == 1
        assert store.list_provider_requests_by_state({"queued"}) == []
        assert store.claim_provider_request_fallback(request_id, expected_run_id=run_id) is None
        assert store.claim_provider_request_context_recovery(request_id, expected_run_id=run_id) is None
        assert store.start_provider_request_fallback(
            request_id, expected_run_id=run_id, fallback_run_id="fallback", session_id=before["session_id"]
        ) is None
        assert store.fail_stale_provider_request_fallback(request_id, claimed_before="9999-01-01") is None
        assert store.claim_provider_request_cancel(request_id) is None
        assert store.claim_provider_request_deadline(request_id) is None
        assert store.commit_provider_request_terminal(
            request_id, expected_run_id=run_id, state="completed", response_json='{"held":true}'
        )["state"] == before["state"]
        assert store.set_provider_request_response_if_empty(request_id, '{"held":true}')['response_json'] == before['response_json']
        assert store.update_provider_request_if_state(request_id, ("queued",), state="running") is None
        assert store.update_provider_request(request_id, state="failed")["state"] == before["state"]
        assert store.get_provider_request(request_id)["restore_hold_set_hash"] == "receipt"
    finally:
        store.close()
