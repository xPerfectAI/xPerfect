"""Native preclaim capacity must reserve a fresh generation after attention resume."""
from __future__ import annotations

import pytest
from workers_projects_runtime.store import HostRunLeaseCapacityError, Store, WorkAdmissionError

EXECUTOR = "synthetic-executor"


def subject(tmp_path):
    store = Store(str(tmp_path / "retry.sqlite3"))
    work = store.reserve_delegation(
        tenant_id="synthetic-tenant", owner_id="synthetic-owner", idempotency_key="synthetic-work",
        request_digest="synthetic-digest", origin_ref="synthetic-origin", title="Synthetic work",
        goal="Create a synthetic artifact", instruction="Synthetic", origin_surface="web",
        worker_name="Synthetic worker", worker_role="worker", profile="codex-cli", backend="codex-cli",
        runtime="codex-cli", model="synthetic", execution_mode="host",
    )
    return store, work["worker_id"], work["run_id"], work["work_ref"]


def reserve(store, worker_id, run_id, **overrides):
    args = dict(runtime_family="codex", lane="mission", tenant_id="synthetic-tenant",
                owner_id="synthetic-owner", worker_id=worker_id, run_id=run_id,
                executor_id=EXECUTOR, conversation_limit=2, mission_limit=8,
                account_mission_limit=8, tenant_mission_limit=8, lease_ttl_s=300)
    return store.acquire_host_run_lease(**(args | overrides))


def old_attempt(store, worker_id, run_id):
    lease = reserve(store, worker_id, run_id)
    claimed = store.claim_next_queued_run(worker_id, executor_id=EXECUTOR, lease_ttl_s=300)
    assert claimed
    admitted = store.admit_claimed_run(run_id, lease_id=lease["lease_id"], executor_id=EXECUTOR)
    assert admitted
    running = store.mark_run_runtime_invoked(run_id, lease_id=lease["lease_id"], executor_id=EXECUTOR)
    assert running
    attention = store.mark_run_needs_input(run_id, error_text="Synthetic provider stall",
        failure_class="provider_progress_stalled", failure_user_message="Synthetic attention")
    assert attention
    store.release_host_run_lease(lease["lease_id"], executor_id=EXECUTOR, reason="needs_input")
    store.update_worker(worker_id, state="needs_input", compute_released_at="2026-01-01T00:00:00+00:00")
    return running, lease


def resume(store, worker_id, run_id, work_ref):
    current = store.get_run(run_id)
    action = store.reserve_active_work_action(
        tenant_id="synthetic-tenant", owner_id="synthetic-owner", work_ref=work_ref,
        idempotency_key="synthetic-resume", action="resume", payload_digest="synthetic-action",
        expected_current_run_id=run_id, expected_source_run_id=run_id,
        expected_source_state="needs_input", expected_source_started_at=current["started_at"] or "",
        executor_id=EXECUTOR,
    )
    result = store.resume_needs_input_active_work_action(action["action_use_id"],
        worker_id=worker_id, run_id=run_id, executor_id=EXECUTOR)
    assert result
    return action, result


def legacy_resume(store, worker_id, run_id, work_ref, original):
    # Replay a row persisted by the old resume producer, with its native action
    # and queue-wait receipt, but the retired generation still in current fields.
    resume(store, worker_id, run_id, work_ref)
    with store._connect() as conn:
        conn.execute("UPDATE runs SET active_attempt_id=?, claimed_at=?, admitted_at=?, runtime_invoked_at=? WHERE run_id=?",
            (original["active_attempt_id"], original["claimed_at"], original["admitted_at"], original["runtime_invoked_at"], run_id))


def assert_fresh_preclaim(store, worker_id, run_id, original):
    history = store.get_run_attempt(original["active_attempt_id"])
    lease = reserve(store, worker_id, run_id)
    queued = store.get_run(run_id)
    assert queued["active_attempt_id"] == ""
    assert queued["claimed_at"] is None and queued["admitted_at"] is None
    assert queued["runtime_invoked_at"] is None
    assert queued["started_at"] == original["started_at"]
    assert lease["attempt_id"] == ""
    assert reserve(store, worker_id, run_id)["lease_id"] == lease["lease_id"]
    assert store.get_run_attempt(original["active_attempt_id"]) == history
    claimed = store.claim_next_queued_run(worker_id, executor_id=EXECUTOR, lease_ttl_s=300)
    assert claimed and claimed["active_attempt_id"] != original["active_attempt_id"]
    assert claimed["started_at"] == original["started_at"]
    assert store.get_active_host_run_lease_for_run(run_id)["attempt_id"] == claimed["active_attempt_id"]
    assert reserve(store, worker_id, run_id)["attempt_id"] == claimed["active_attempt_id"]
    assert store.admit_claimed_run(run_id, lease_id=lease["lease_id"], executor_id=EXECUTOR)
    assert store.mark_run_runtime_invoked(run_id, lease_id=lease["lease_id"], executor_id=EXECUTOR)


def test_resume_retires_old_generation_before_preclaim_and_preserves_receipt(tmp_path):
    store, worker_id, run_id, work_ref = subject(tmp_path)
    original, _ = old_attempt(store, worker_id, run_id)
    action, result = resume(store, worker_id, run_id, work_ref)
    assert result["run"]["active_attempt_id"] == ""
    replay = store.resume_needs_input_active_work_action(action["action_use_id"],
        worker_id=worker_id, run_id=run_id, executor_id=EXECUTOR)
    assert replay and replay["run"]["active_attempt_id"] == ""
    assert_fresh_preclaim(store, worker_id, run_id, original)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND event_type='run.resumed'", (run_id,)).fetchone()[0] == 1


def test_retained_queued_attention_recovers_at_native_preclaim(tmp_path):
    store, worker_id, run_id, work_ref = subject(tmp_path)
    original, _ = old_attempt(store, worker_id, run_id)
    legacy_resume(store, worker_id, run_id, work_ref, original)
    assert_fresh_preclaim(store, worker_id, run_id, original)


@pytest.mark.parametrize("invalid", ["unended", "missing", "mismatched", "lease_mismatched", "active_lease", "not_latest"])
def test_queued_live_or_unproven_generation_cannot_be_retired(tmp_path, invalid):
    store, worker_id, run_id, work_ref = subject(tmp_path)
    original, lease = old_attempt(store, worker_id, run_id)
    legacy_resume(store, worker_id, run_id, work_ref, original)
    other = store.create_run(worker_id, store.get_worker(worker_id)["project_id"], "Other synthetic") if invalid == "mismatched" else None
    with store._connect() as conn:
        if invalid == "unended":
            conn.execute("UPDATE run_attempts SET ended_at=NULL WHERE attempt_id=?", (original["active_attempt_id"],))
        elif invalid == "missing":
            conn.execute("UPDATE runs SET active_attempt_id='absent' WHERE run_id=?", (run_id,))
        elif invalid == "mismatched":
            conn.execute("UPDATE run_attempts SET run_id=? WHERE attempt_id=?", (other["run_id"], original["active_attempt_id"]))
        elif invalid == "lease_mismatched":
            conn.execute("UPDATE host_run_leases SET attempt_id='other-attempt' WHERE lease_id=?", (lease["lease_id"],))
        elif invalid == "active_lease":
            conn.execute("UPDATE host_run_leases SET status='active' WHERE lease_id=?", (lease["lease_id"],))
        else:
            conn.execute("INSERT INTO run_attempts(attempt_id,run_id,attempt_number,state,claimed_at,ended_at) VALUES('newer',?,2,'needs_input','synthetic','synthetic')", (run_id,))
    with pytest.raises(WorkAdmissionError) as rejected:
        reserve(store, worker_id, run_id)
    assert rejected.value.code == "run_not_claimed"
    assert store.get_run(run_id)["active_attempt_id"] in {original["active_attempt_id"], "absent"}


def test_capacity_refusal_rolls_back_generation_retirement(tmp_path):
    store, worker_id, run_id, work_ref = subject(tmp_path)
    original, _ = old_attempt(store, worker_id, run_id)
    legacy_resume(store, worker_id, run_id, work_ref, original)
    project = store.create_project("synthetic-owner", "Other", "Synthetic", "codex-cli")
    other_worker = store.create_worker(project_id=project["project_id"], owner_id="synthetic-owner",
        name="Other", role="worker", profile="codex-cli", backend="codex-cli", runtime="codex-cli",
        model="synthetic", execution_mode="host")
    other = store.create_run(other_worker["worker_id"], project["project_id"], "Synthetic")
    reserve(store, other_worker["worker_id"], other["run_id"])
    with pytest.raises(HostRunLeaseCapacityError):
        reserve(store, worker_id, run_id, mission_limit=1)
    assert store.get_run(run_id)["active_attempt_id"] == original["active_attempt_id"]
    assert len(store.list_run_attempts(run_id)) == 1
    assert_fresh_preclaim(store, worker_id, run_id, original)


@pytest.mark.parametrize("invalid", ["unended", "active_lease"])
def test_resume_rejects_unsettled_generation_without_receipt(tmp_path, invalid):
    store, worker_id, run_id, work_ref = subject(tmp_path)
    original, lease = old_attempt(store, worker_id, run_id)
    with store._connect() as conn:
        if invalid == "unended":
            conn.execute("UPDATE run_attempts SET ended_at=NULL WHERE attempt_id=?", (original["active_attempt_id"],))
        else:
            conn.execute("UPDATE host_run_leases SET status='active' WHERE lease_id=?", (lease["lease_id"],))
    action = store.reserve_active_work_action(
        tenant_id="synthetic-tenant", owner_id="synthetic-owner", work_ref=work_ref,
        idempotency_key="synthetic-resume", action="resume", payload_digest="synthetic-action",
        expected_current_run_id=run_id, expected_source_run_id=run_id,
        expected_source_state="needs_input", expected_source_started_at=original["started_at"],
        executor_id=EXECUTOR)
    assert store.resume_needs_input_active_work_action(action["action_use_id"],
        worker_id=worker_id, run_id=run_id, executor_id=EXECUTOR) is None
    assert store.get_run(run_id)["state"] == "needs_input"
    assert store.get_active_work_action(action["action_use_id"])["effect_phase"] == ""
