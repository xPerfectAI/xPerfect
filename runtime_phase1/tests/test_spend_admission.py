from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from workers_projects_runtime.spend_admission import AdmissionPolicyHold, SpendPolicyRevisionConflict
from workers_projects_runtime.store import Store
from test_usage_accounting import fixture_run, invoked_attempt, reconcile


def policy(**overrides):
    return {"version":1,"enabled":True,"currency":"USD","limit_amount":"1.00","unknown_policy":"reserve","reserve_amount":"0.60",**overrides}


def test_operator_hold_prevents_parallel_and_schedule_claim_churn_and_survives_restart(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    hold = store.place_admission_hold("local", "owner")
    for _ in range(6):
        assert store.peek_next_queued_run(worker["worker_id"]) is None
        assert store.claim_next_queued_run(worker["worker_id"]) is None
        assert store.claim_next_queued_run(worker["worker_id"], require_schedule_principal_authority=True) is None
    assert store.list_run_attempts(run["run_id"]) == []
    assert store.get_run(run["run_id"])["state"] == "queued"
    reopened = Store(store.db_path)
    with pytest.raises(SpendPolicyRevisionConflict):
        reopened.resume_admission_hold(hold["hold_id"], "local", "owner", 99)
    reopened.resume_admission_hold(hold["hold_id"], "local", "owner", hold["generation"])
    assert reopened.claim_next_queued_run(worker["worker_id"]) is not None


def test_scopes_do_not_pause_another_project_or_owner(tmp_path):
    store, project, worker, run = fixture_run(tmp_path)
    _, other, other_worker, other_run = fixture_run(tmp_path, "two", store)
    store.place_admission_hold("local", "owner", "project", project["project_id"])
    assert store.claim_next_queued_run(worker["worker_id"]) is None
    assert store.claim_next_queued_run(other_worker["worker_id"]) is not None
    with pytest.raises(ValueError):
        store.place_admission_hold("local", "foreign", "project", project["project_id"])


def test_hold_freezes_queue_clock_and_resumes_same_episode(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    now = datetime.now(timezone.utc)
    hold = store.place_admission_hold("local", "owner", reason_code="transport_integrity")
    with store._connect() as conn:
        conn.execute("UPDATE runs SET admission_hold_started_at=?,queue_deadline_at=? WHERE run_id=?",
                     ((now-timedelta(seconds=60)).isoformat(), (now-timedelta(seconds=1)).isoformat(),run["run_id"]))
    assert store.list_due_queue_timeouts(now=now.isoformat()) == []
    assert store.list_due_queue_status(now=(now+timedelta(hours=1)).isoformat()) == []
    assert store.next_queue_maintenance_at(now=now.isoformat()) is None
    before = store.get_run(run["run_id"])
    store.resume_admission_hold(hold["hold_id"],"local","owner",hold["generation"])
    after = store.get_run(run["run_id"])
    assert after["queue_wait_episode"] == before["queue_wait_episode"]
    assert after["queue_wait_generation"] == before["queue_wait_generation"]
    assert datetime.fromisoformat(after["queue_deadline_at"]) > now
    assert not after["admission_hold_started_at"]
    assert store.claim_next_queued_run(worker["worker_id"]) is not None


def test_final_invocation_hold_is_typed_and_leaves_active_work_unchanged(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    claimed = store.claim_next_queued_run(worker["worker_id"])
    lease = store.acquire_host_run_lease(runtime_family="codex",lane="mission",tenant_id="local",owner_id="owner",worker_id=worker["worker_id"],run_id=run["run_id"],executor_id="executor",conversation_limit=2,mission_limit=4,account_mission_limit=4,tenant_mission_limit=4,lease_ttl_s=300)
    store.admit_claimed_run(run["run_id"],lease_id=lease["lease_id"],executor_id="executor")
    hold = store.place_admission_hold("local", "owner")
    with pytest.raises(AdmissionPolicyHold):
        store.mark_run_runtime_invoked(run["run_id"],lease_id=lease["lease_id"],executor_id="executor")
    assert not store.get_run(run["run_id"])["runtime_invoked_at"]
    store.resume_admission_hold(hold["hold_id"],"local","owner",hold["generation"])
    active = store.mark_run_runtime_invoked(run["run_id"],lease_id=lease["lease_id"],executor_id="executor")
    store.place_admission_hold("local","owner")
    assert store.get_run(run["run_id"])["state"] == "running"
    assert store.get_run(run["run_id"])["runtime_invoked_at"] == active["runtime_invoked_at"]


def test_changed_operator_reason_fences_stale_resume(tmp_path):
    store, _, _, _ = fixture_run(tmp_path)
    old = store.place_admission_hold("local", "owner")
    changed = store.place_admission_hold("local", "owner", reason_code="transport_integrity")
    assert changed["generation"] == old["generation"] + 1
    with pytest.raises(SpendPolicyRevisionConflict):
        store.resume_admission_hold(old["hold_id"],"local","owner",old["generation"])


def test_interrupt_terminal_artifact_owner_closes_attempt_and_unknown_reserve(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    store.put_spend_policy("local","owner",policy(limit_amount="10"),0)
    run, _ = invoked_attempt(store,worker,run)
    with store._connect() as conn:
        conn.execute("UPDATE runs SET state='interrupted',ended_at=? WHERE run_id=?", (run["runtime_invoked_at"],run["run_id"]))
        store._record_terminal_artifact_trace_conn(conn,run_id=run["run_id"],observed_at=run["runtime_invoked_at"])
    attempt=store.list_run_attempts(run["run_id"])[0]
    assert attempt["state"] == "interrupted" and attempt["ended_at"]
    assert attempt["usage"]["coverage"] == "unknown" and len(attempt["usage"]["receipt_ids"]) == 1
    with store._connect() as conn:
        assert conn.execute("SELECT state FROM spend_reservations WHERE attempt_id=?",(attempt["attempt_id"],)).fetchone()[0] == "unknown"


def test_interrupt_held_uninvoked_work_uses_cancellation_without_native_binding(tmp_path):
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.service import WorkersProjectsService
    store, _, worker, run = fixture_run(tmp_path)
    store.place_admission_hold("local","owner")
    class NoNativeInterrupt(StubRuntime):
        def interrupt_worker(self, *args, **kwargs):
            raise AssertionError("Uninvoked queued work must not reach native interruption")
    service = WorkersProjectsService(store, NoNativeInterrupt(), reconcile_on_startup=False, start_background_consumers=False)
    try:
        result=service.interrupt_worker(worker["worker_id"])
        assert result["worker_id"] == worker["worker_id"]
        assert store.get_run(run["run_id"])["state"] == "cancelled"
        assert store.list_run_attempts(run["run_id"]) == []
        assert store.list_admission_holds("local","owner")[0]["active"]
    finally:
        service.shutdown()


def test_concurrent_reservations_admit_only_one_then_unknown_retains_reserve(tmp_path):
    store, _, worker1, run1 = fixture_run(tmp_path)
    _, _, worker2, run2 = fixture_run(tmp_path, "two", store)
    configured = store.put_spend_policy("local","owner",policy(),0)
    assert configured["revision"] == 1
    claims = [store.claim_next_queued_run(w["worker_id"]) for w in (worker1,worker2)]
    leases = [store.acquire_host_run_lease(runtime_family="codex",lane="mission",tenant_id="local",owner_id="owner",worker_id=w["worker_id"],run_id=r["run_id"],executor_id="executor",conversation_limit=2,mission_limit=4,account_mission_limit=4,tenant_mission_limit=4,lease_ttl_s=300) for w,r in ((worker1,run1),(worker2,run2))]
    def admit(index):
        try:
            return store.admit_claimed_run(claims[index]["run_id"],lease_id=leases[index]["lease_id"],executor_id="executor") is not None
        except AdmissionPolicyHold:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(admit, (0,1))) == [False,True]
    assert len([item for item in store.list_admission_holds("local","owner") if item["active"]]) == 1


def test_late_money_and_unknown_policy_pause_new_work_without_cancelling_active(tmp_path):
    store, _, worker, run = fixture_run(tmp_path)
    store.put_spend_policy("local","owner",policy(limit_amount="10", unknown_policy="pause"),0)
    run,_ = invoked_attempt(store,worker,run)
    reconcile(store,run)
    _,_,other,other_run = fixture_run(tmp_path,"two",store)
    assert store.claim_next_queued_run(other["worker_id"]) is None
    assert store.get_run(run["run_id"])["state"] == "running"
    hold = store.list_admission_holds("local","owner")[0]
    reconcile(store,run,total_cost_usd="0.50",revision=1)
    store.finalize_run(run["run_id"], "completed")
    store.resume_admission_hold(hold["hold_id"],"local","owner",hold["generation"])
    assert store.claim_next_queued_run(other["worker_id"]) is not None
