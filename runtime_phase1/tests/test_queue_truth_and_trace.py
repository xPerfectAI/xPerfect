from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

import workers_projects_runtime.service as service_module
import workers_projects_runtime.bootstrap as bootstrap_module
import workers_projects_runtime.mcp_server as mcp_server_module
import workers_projects_runtime.store as store_module
from workers_projects_runtime.openclaw_runtime import HostCapacityError, StubRuntime
from workers_projects_runtime.native_team import NativeTeamProjection
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


def _expire_wait_in_process(
    db_path: str,
    run_id: str,
    deadline: str,
    generation: int,
    callback_id: str,
    callback_intent: dict,
    now_iso: str,
    start_event,
    result_queue,
) -> None:
    store = Store(db_path)
    start_event.wait(timeout=10)
    expired = store.expire_queued_run_if_due(
        run_id,
        expected_deadline=deadline,
        expected_generation=generation,
        expected_callback_id=callback_id,
        callback_intent=callback_intent,
        now=now_iso,
    )
    result_queue.put(expired is not None)


@pytest.fixture(autouse=True)
def _disable_background_scheduler(monkeypatch):
    monkeypatch.setattr(
        WorkersProjectsService,
        "_process_scheduler_cycle",
        lambda _self: None,
    )


def _reserve_callback_delegation(store: Store, *, suffix: str = "queue") -> tuple[dict, dict]:
    record = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key=f"idempotency-{suffix}",
        request_digest=f"digest-{suffix}",
        origin_ref=f"origin_{suffix}_0001",
        title="Synthetic queued work",
        goal="Prove truthful queue behavior",
        instruction="Create one synthetic artifact.",
        origin_surface="telegram",
        worker_name="Synthetic worker",
        worker_role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test-model",
        execution_mode="docker",
        bootstrap_bundle={
            "callbacks": {
                "origin_ref": f"origin_{suffix}_0001",
                "events_webhook_url": "https://callback.example.invalid/events",
            }
        },
    )
    worker = store.get_worker(record["worker_id"])
    assert worker is not None
    return record, worker


def _capacity_error() -> HostCapacityError:
    error = HostCapacityError(
        "Synthetic memory pressure",
        capacity_class="resource_pressure",
        retry_after_s=5,
    )
    error.available = {"memoryBytes": int(4.3 * 1024**3)}
    error.required = {"memoryBytes": 5 * 1024**3}
    error.shortage = {"memoryBytes": int(0.7 * 1024**3)}
    error.reservation = {"memoryBytes": 0}
    return error


def _capacity_failure_fields() -> dict[str, object]:
    return {
        "failure_class": "host_capacity",
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "Waiting for measured host capacity.",
        "failure_recommended_recovery": "Wait for capacity or stop other work.",
        "failure_diagnostic_summary": "Synthetic resource-pressure admission failure.",
    }


def _callbacks_for_run(store: Store, run_id: str) -> list[dict]:
    return store.list_callback_outbox_for_run(
        run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
        limit=100,
    )


def _acquire_test_lease(
    store: Store,
    worker: dict,
    run: dict,
    *,
    executor_id: str,
    mission_limit: int = 4,
) -> dict:
    return store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker.get("owner_id") or "owner-a"),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=mission_limit,
        account_mission_limit=mission_limit,
        tenant_mission_limit=mission_limit,
        lease_ttl_s=300,
        now=datetime.fromisoformat(store_module.utc_now()),
    )


def _admit_test_run(store: Store, worker: dict, run: dict, *, executor_id: str) -> tuple[dict, dict]:
    lease = _acquire_test_lease(
        store,
        worker,
        run,
        executor_id=executor_id,
    )
    admitted = store.admit_claimed_run(
        str(run["run_id"]),
        lease_id=str(lease["lease_id"]),
        executor_id=executor_id,
    )
    assert admitted is not None
    return admitted, lease


def _terminal_generation(run: dict, lease: dict) -> dict[str, str]:
    return {
        "expected_attempt_id": str(run["active_attempt_id"]),
        "expected_lease_id": str(lease["lease_id"]),
        "expected_executor_id": str(lease["executor_id"]),
        "expected_startup_token": str(lease["startup_token"]),
        "expected_runtime_invoked_at": str(run["runtime_invoked_at"]),
    }


def test_six_minute_wait_emits_one_transition_and_one_coalesced_refresh(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "1800")
    monkeypatch.setenv("GLASSHIVE_QUEUE_STATUS_REFRESH_INTERVAL_S", "120")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())

    store = Store(str(tmp_path / "six-minute.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="six_minute")
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._now_datetime = lambda: clock[0]
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        run = store.get_run(record["current_run_id"])
        waiting = service._requeue_retryable_run(
            worker,
            run,
            _capacity_error(),
            failure_fields=_capacity_failure_fields(),
        )
        assert waiting is not None
        assert waiting["first_queued_at"] == start.isoformat()
        assert waiting["queue_blocker_class"] == "host_capacity"
        assert waiting["queue_deadline_at"] == (start + timedelta(minutes=30)).isoformat()

        clock[0] = start + timedelta(minutes=6)
        service.process_queued_work_status_once(now=clock[0])
        service.process_queued_work_status_once(now=clock[0])

        callbacks = _callbacks_for_run(store, run["run_id"])
        assert [item["event_type"] for item in callbacks] == [
            "run.waiting_on_capacity",
            "run.queue_status",
        ]
        assert store.list_run_attempts(run["run_id"]) == []
        assert [item["attempt_number"] for item in callbacks] == [0, 0]
        assert [
            json.loads(item["payload_json"])["attempt_number"] for item in callbacks
        ] == [None, None]
        refresh = json.loads(callbacks[-1]["payload_json"])
        assert refresh["queueAgeSeconds"] == 360
        assert refresh["blocker"] == {"class": "host_capacity"}
        assert refresh["nextRetryAt"]
        assert refresh["timeoutAt"] == (start + timedelta(minutes=30)).isoformat()
        detail = store.work_trace_detail(
            run_id=run["run_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        assert detail is not None
        assert [item["attemptNumber"] for item in detail["callbackDeliveries"]] == [
            None,
            None,
        ]
        assert detail["attemptHistory"] == []
    finally:
        service.shutdown()


def test_capacity_wait_uses_one_persisted_episode_and_backed_off_retry_clock(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "5")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_MAX_DELAY_S", "60")

    db_path = tmp_path / "capacity-clock.sqlite3"
    store = Store(str(db_path))
    record, worker = _reserve_callback_delegation(store, suffix="capacity_clock")
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._now_datetime = lambda: clock[0]
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    observed_delays: list[float] = []
    try:
        run = store.get_run(record["current_run_id"])
        for retry_number in range(1, 9):
            updated = service._requeue_retryable_run(
                worker,
                run,
                _capacity_error(),
                failure_fields=_capacity_failure_fields(),
            )
            assert updated is not None
            retry_at = datetime.fromisoformat(str(updated["retry_after"]))
            observed_delays.append((retry_at - clock[0]).total_seconds())
            assert updated["capacity_retry_count"] == retry_number
            assert updated["retry_attempts"] == 0
            assert updated["queue_wait_generation"] == 1
            assert updated["queue_wait_open"] == 1
            assert store.list_run_attempts(run["run_id"]) == []
            assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
            assert store.get_worker(worker["worker_id"])["state"] == "ready"
            run = Store(str(db_path)).get_run(run["run_id"])
            assert run is not None
            clock[0] = retry_at

        detail = store.work_trace_detail(
            run_id=run["run_id"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert detail is not None
        assert len(detail["capacityAttempts"]) == 1
        assert detail["capacityAttemptOverflowCount"] == 0
        assert observed_delays[0] >= 5
        assert observed_delays[0] <= 5.5
        assert observed_delays[1] >= 10
        assert observed_delays[1] <= 11
        assert observed_delays[2] >= 20
        assert observed_delays[2] <= 22
        assert observed_delays[3] >= 40
        assert observed_delays[3] <= 44
        assert observed_delays[4:] == [60, 60, 60, 60]
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("probe_state", "transient"),
    [
        ("refresh_in_progress", True),
        ("proof_lapsed", True),
        ("snapshot_invalidated", True),
        ("native_network_membership_unverified", False),
        ("filesystem_acl_application_unavailable", False),
    ],
)
def test_probe_timing_wait_keeps_worker_compute_and_recorded_failures_release_it(
    tmp_path, monkeypatch, probe_state, transient
):
    import time
    from types import SimpleNamespace

    from workers_projects_runtime.service import HostResourceUsage
    from workers_projects_runtime.workspace_resources import WorkspaceResources

    monkeypatch.setenv("XPERFECT_CONTROLLER_ID", "c" * 64)
    monkeypatch.setenv("XPERFECT_SHARED_NETWORK", "workers")
    resources = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    proof = {"image": "", "volume_name": "", "volume_root": "", "control_root": "",
             "controller": "c" * 64, "network": "workers"}
    resources._substrate_proof = (time.monotonic(), proof)
    resources.cached = (time.monotonic(), {
        "child_processes": 0, "threads": 0,
        "available_memory_bytes": 16 * 1024**3, "available_disk_bytes": 64 * 1024**3,
        "running_worker_containers": 0, "running_worker_ids": [],
        "process_probe_ok": True, "memory_probe_ok": True, "disk_probe_ok": True,
    })
    if probe_state == "proof_lapsed":
        resources._substrate_proof = (time.monotonic() - 31.0, proof)
    elif probe_state == "snapshot_invalidated":
        resources.invalidate_capacity_snapshot()
    elif not transient:
        # A completed controlled refresh failed closed and recorded its typed reason.
        resources._substrate_proof = None
        resources.last_refresh_error_code = probe_state

    class PackagedRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            return resources.usage(self, cached_only=cached_only)

    monkeypatch.setattr(service_module, "host_resource_usage", lambda _leases: HostResourceUsage(
        child_processes=0, threads=0,
        available_memory_bytes=16 * 1024**3, available_disk_bytes=64 * 1024**3,
    ))
    store = Store(str(tmp_path / f"probe-{probe_state}.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix=probe_state)
    service = WorkersProjectsService(store, PackagedRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    released: list[str] = []
    release = service._release_capacity_wait_compute
    monkeypatch.setattr(service, "_release_capacity_wait_compute",
                        lambda held, run: released.append(run["run_id"]) or release(held, run))
    try:
        if probe_state == "refresh_in_progress":
            resources._probe_lock.acquire()
        try:
            # Admission reads only the background snapshot, exactly as dispatch does.
            error = service._host_resource_capacity_error(worker, docker_cached_only=True)
        finally:
            if probe_state == "refresh_in_progress":
                resources._probe_lock.release()
        assert isinstance(error, HostCapacityError)
        assert error.capacity_class == "resource_probe_unavailable"
        if not transient:
            assert error.probe_error_code == probe_state

        run = store.get_run(record["current_run_id"])
        updated = service._requeue_retryable_run(
            worker, run, error, failure_fields=_capacity_failure_fields()
        )
        # Admission is refused the same way in every state: one open capacity wait
        # with its retry clock. Only a recorded failure stops the waiting worker.
        assert updated is not None
        assert updated["state"] == "queued"
        assert updated["queue_wait_open"] == 1
        assert updated["capacity_retry_count"] == 1
        assert updated["retry_after"]
        assert released == ([] if transient else [run["run_id"]])
        assert error.probe_transient is transient
    finally:
        service.shutdown()


def test_queue_refresh_identity_survives_restart_without_duplicate(tmp_path, monkeypatch):
    start = datetime(2026, 8, 22, 13, 0, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "1800")
    monkeypatch.setenv("GLASSHIVE_QUEUE_STATUS_REFRESH_INTERVAL_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    db_path = tmp_path / "refresh-restart.sqlite3"
    store = Store(str(db_path))
    record, worker = _reserve_callback_delegation(store, suffix="refresh_restart")
    first = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    first._now_datetime = lambda: clock[0]
    first._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        first._requeue_retryable_run(
            worker,
            store.get_run(record["current_run_id"]),
            _capacity_error(),
            failure_fields=_capacity_failure_fields(),
        )
        clock[0] = start + timedelta(minutes=2)
        due = store.list_due_queue_status(now=clock[0].isoformat(), limit=1)[0]
        # Simulate process exit after durable outbox insertion but before the
        # queue refresh cursor advances.
        first._emit_callback(
            worker,
            "run.queue_status",
            run=due,
            message="This work is still queued.",
            callback_id=first._queue_callback_id(
                "refresh",
                due,
                sequence=int(due["queue_status_sequence"] or 0) + 1,
            ),
            insert_once=True,
        )
    finally:
        first.shutdown()

    reopened = Store(str(db_path))
    second = WorkersProjectsService(reopened, StubRuntime(), reconcile_on_startup=False)
    second._now_datetime = lambda: clock[0]
    second._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        second.process_queued_work_status_once(now=clock[0])
        callbacks = _callbacks_for_run(reopened, record["current_run_id"])
    finally:
        second.shutdown()

    callback_ids = [item["callback_id"] for item in callbacks]
    assert len(callback_ids) == 2
    assert len(set(callback_ids)) == 2


def test_queue_deadline_terminalizes_once_survives_restart_and_late_probe_cannot_resurrect(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 14, 0, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setenv("GLASSHIVE_QUEUE_STATUS_REFRESH_INTERVAL_S", "20")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    db_path = tmp_path / "queue-timeout.sqlite3"
    store = Store(str(db_path))
    record, worker = _reserve_callback_delegation(store, suffix="timeout_restart")
    stale_run = store.get_run(record["current_run_id"])
    first = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    first._now_datetime = lambda: clock[0]
    first._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        first._requeue_retryable_run(
            worker,
            stale_run,
            _capacity_error(),
            failure_fields=_capacity_failure_fields(),
        )
        clock[0] = start + timedelta(seconds=61)
        result = first.process_queued_work_status_once(now=clock[0])
        expired = store.get_run(record["current_run_id"])
        assert result["timedOut"] == 1
        assert expired["queue_callback_state"] == "enqueued"
    finally:
        first.shutdown()

    reopened = Store(str(db_path))
    second = WorkersProjectsService(reopened, StubRuntime(), reconcile_on_startup=False)
    second._now_datetime = lambda: clock[0]
    second._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        second.process_queued_work_status_once(now=clock[0])
        late = second._requeue_retryable_run(
            worker,
            stale_run,
            _capacity_error(),
            failure_fields=_capacity_failure_fields(),
        )
        durable = reopened.get_run(record["current_run_id"])
        callbacks = _callbacks_for_run(reopened, record["current_run_id"])
    finally:
        second.shutdown()

    assert late is None
    assert durable["state"] == "failed"
    assert durable["failure_class"] == "queue_wait_timeout"
    assert durable["failure_retryable"] == 1
    failed_callbacks = [
        item for item in callbacks if item["event_type"] == "run.failed"
    ]
    assert len(failed_callbacks) == 1
    assert store.list_run_attempts(stale_run["run_id"]) == []
    assert failed_callbacks[0]["attempt_number"] == 0
    assert json.loads(failed_callbacks[0]["payload_json"])["attempt_number"] is None
    detail = reopened.work_trace_detail(
        run_id=stale_run["run_id"],
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    assert detail is not None
    assert detail["attemptHistory"] == []
    assert detail["callbackDeliveries"][-1]["attemptNumber"] is None


@pytest.mark.parametrize("stalled_state", ["claimed", "admitted"])
def test_queue_deadline_remains_active_for_stalled_claimed_and_admitted_runs(
    tmp_path, monkeypatch, stalled_state
):
    start = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: start.isoformat())
    store = Store(str(tmp_path / f"deadline-{stalled_state}.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix=f"stalled_{stalled_state}"
    )
    run = store.get_run(record["current_run_id"])
    claimed = store.claim_next_queued_run(worker["worker_id"])
    assert claimed is not None
    if stalled_state == "admitted":
        claimed, _lease = _admit_test_run(
            store,
            worker,
            claimed,
            executor_id="stalled-executor",
        )

    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        service.process_queued_work_status_once(now=start + timedelta(seconds=61))
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"])
    assert claimed["state"] == stalled_state
    assert durable["state"] == "failed"
    assert durable["failure_class"] == "queue_wait_timeout"
    assert durable["queue_wait_open"] == 0
    callbacks = _callbacks_for_run(store, run["run_id"])
    assert [item["event_type"] for item in callbacks] == ["run.failed"]


def test_exact_runtime_invocation_closes_wait_and_later_execution_opens_new_generation(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 15, 30, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    store = Store(str(tmp_path / "wait-generation.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="wait_generation")
    run_id = str(record["current_run_id"])

    first_claim = store.claim_next_queued_run(worker["worker_id"])
    first_wait = store.requeue_run_for_retry(
        run_id,
        retry_after=start.isoformat(),
        **(store.get_run_retry_generation(run_id) or {}),
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )
    second_claim = store.claim_next_queued_run(worker["worker_id"])
    changed_blocker = store.requeue_run_for_retry(
        run_id,
        retry_after=start.isoformat(),
        **(store.get_run_retry_generation(run_id) or {}),
        last_retry_class="provider_rate_limited",
        failure_class="provider_rate_limited",
        failure_retryable=1,
        failure_structured=1,
    )
    third_claim = store.claim_next_queued_run(worker["worker_id"])
    admitted, lease = _admit_test_run(
        store,
        worker,
        third_claim,
        executor_id="generation-one",
    )
    invoked = store.mark_run_runtime_invoked(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id="generation-one",
    )

    assert first_claim is not None and second_claim is not None
    assert first_wait["queue_wait_generation"] == 1
    assert changed_blocker["queue_wait_generation"] == 1
    assert invoked is not None and invoked["queue_wait_open"] == 0
    assert invoked["queue_wait_closed_at"] == start.isoformat()

    clock[0] = start + timedelta(minutes=5)
    reopened = store.requeue_run_for_retry(
        run_id,
        retry_after=clock[0].isoformat(),
        **(store.get_run_retry_generation(run_id) or {}),
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )
    assert admitted["state"] == "admitted"
    assert reopened is not None
    assert reopened["queue_wait_open"] == 1
    assert reopened["queue_wait_generation"] == 2
    assert reopened["queue_wait_started_at"] == clock[0].isoformat()
    assert reopened["queue_deadline_at"] == (
        clock[0] + timedelta(seconds=60)
    ).isoformat()
    assert store.get_host_run_lease(lease["lease_id"])["status"] == "released"


def test_stale_processor_requeue_cannot_mutate_or_release_newer_attempt(tmp_path):
    db_path = tmp_path / "stale-attempt-requeue.sqlite3"
    first = Store(str(db_path))
    record, worker = _reserve_callback_delegation(first, suffix="stale_attempt")
    run_id = str(record["current_run_id"])
    attempt_one = first.claim_next_queued_run(worker["worker_id"])
    assert attempt_one is not None
    lease_one = _acquire_test_lease(
        first, worker, attempt_one, executor_id="processor-one"
    )
    stale_fence = {
        "expected_attempt_id": str(attempt_one["active_attempt_id"]),
        "expected_lease_id": str(lease_one["lease_id"]),
        "expected_executor_id": str(lease_one["executor_id"]),
        "expected_startup_token": str(lease_one["startup_token"]),
    }
    first_requeue = first.requeue_run_for_retry(
        run_id,
        retry_after="2000-01-01T00:00:00+00:00",
        last_retry_class="host_capacity",
        **stale_fence,
    )
    assert first_requeue is not None

    second = Store(str(db_path))
    attempt_two = second.claim_next_queued_run(worker["worker_id"])
    assert attempt_two is not None
    lease_two = _acquire_test_lease(
        second, worker, attempt_two, executor_id="processor-two"
    )
    assert attempt_two["active_attempt_id"] != attempt_one["active_attempt_id"]
    assert lease_two["startup_token"] != lease_one["startup_token"]

    stale_result = first.requeue_run_for_retry(
        run_id,
        retry_after="2000-01-01T00:00:01+00:00",
        last_retry_class="provider_rate_limited",
        **stale_fence,
    )

    durable = second.get_run(run_id)
    durable_lease = second.get_active_host_run_lease_for_run(run_id)
    attempts = second.list_run_attempts(run_id)
    assert stale_result is None
    assert durable["state"] == "claimed"
    assert durable["active_attempt_id"] == attempt_two["active_attempt_id"]
    assert durable_lease is not None
    assert durable_lease["attempt_id"] == attempt_two["active_attempt_id"]
    assert durable_lease["startup_token"] == lease_two["startup_token"]
    assert attempts[1]["state"] == "claimed"
    assert attempts[1]["ended_at"] is None


def test_timeout_intent_is_atomic_across_crash_restart_and_two_processes(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    db_path = tmp_path / "timeout-two-process.sqlite3"
    first_store = Store(str(db_path))
    record, _worker = _reserve_callback_delegation(
        first_store, suffix="timeout_two_process"
    )
    run_id = str(record["current_run_id"])
    clock[0] = start + timedelta(seconds=61)
    first = WorkersProjectsService(
        first_store, StubRuntime(), reconcile_on_startup=False
    )
    first._deliver_callback_record = lambda *_args, **_kwargs: None

    def crash_after_timeout(*_args, **_kwargs):
        raise RuntimeError("synthetic process death after timeout transaction")

    first_store.finalize_schedule_for_run = crash_after_timeout
    with pytest.raises(RuntimeError, match="synthetic process death"):
        first.process_queued_work_status_once(now=clock[0])
    first.shutdown()

    crashed_run = first_store.get_run(run_id)
    crashed_callbacks = _callbacks_for_run(first_store, run_id)
    assert crashed_run["state"] == "failed"
    assert crashed_run["queue_callback_state"] == "enqueued"
    assert [item["event_type"] for item in crashed_callbacks] == ["run.failed"]

    barrier = Barrier(2)

    def recover_once() -> dict[str, int]:
        reopened_store = Store(str(db_path))
        service = WorkersProjectsService(
            reopened_store, StubRuntime(), reconcile_on_startup=False
        )
        service._deliver_callback_record = lambda *_args, **_kwargs: None
        try:
            barrier.wait(timeout=5)
            return service.process_queued_work_status_once(now=clock[0])
        finally:
            service.shutdown()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: recover_once(), range(2)))

    reopened = Store(str(db_path))
    callbacks = _callbacks_for_run(reopened, run_id)
    assert sum(item["timedOut"] for item in results) == 0
    assert len([item for item in callbacks if item["event_type"] == "run.failed"]) == 1


def test_exact_timeout_and_terminal_callback_are_atomic_across_os_processes(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 16, 15, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    db_path = tmp_path / "timeout-os-processes.sqlite3"
    store = Store(str(db_path))
    record, worker = _reserve_callback_delegation(store, suffix="timeout_os_process")
    run = store.get_run(record["current_run_id"])
    callback_id = str(run["queue_terminal_callback_id"])
    clock[0] = start + timedelta(seconds=61)
    terminal_message = (
        "This work left the queue after its bounded admission wait expired. "
        "The workspace is preserved and the work can be retried."
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        callback_intent = service._emit_callback(
            worker,
            "run.failed",
            run={
                **run,
                "state": "failed",
                "failure_class": "queue_wait_timeout",
                "failure_retryable": 1,
                "failure_user_message": terminal_message,
            },
            message=terminal_message,
            callback_id=callback_id,
            insert_once=True,
            submit_delivery=False,
            persist_callback=False,
        )
    finally:
        service.shutdown()
    assert callback_intent is not None

    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    arguments = (
        str(db_path),
        str(run["run_id"]),
        str(run["queue_deadline_at"]),
        int(run["queue_wait_generation"]),
        callback_id,
        callback_intent,
        clock[0].isoformat(),
        start_event,
        result_queue,
    )
    processes = [
        context.Process(target=_expire_wait_in_process, args=arguments)
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=15)
    try:
        assert [process.exitcode for process in processes] == [0, 0]
        assert sorted(result_queue.get(timeout=2) for _index in range(2)) == [
            False,
            True,
        ]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    durable = Store(str(db_path))
    assert durable.get_run(run["run_id"])["failure_class"] == "queue_wait_timeout"
    assert [
        item["event_type"] for item in _callbacks_for_run(durable, run["run_id"])
    ] == ["run.failed"]


def test_stale_refresh_snapshot_cannot_insert_after_timeout(tmp_path, monkeypatch):
    start = datetime(2026, 8, 22, 16, 30, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "120")
    monkeypatch.setenv("GLASSHIVE_QUEUE_STATUS_REFRESH_INTERVAL_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    store = Store(str(tmp_path / "stale-refresh.sqlite3"))
    record, _worker = _reserve_callback_delegation(store, suffix="stale_refresh")
    run_id = str(record["current_run_id"])
    clock[0] = start + timedelta(seconds=61)
    stale = store.list_due_queue_status(now=clock[0].isoformat(), limit=1)[0]

    clock[0] = start + timedelta(seconds=121)
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        service.process_queued_work_status_once(now=clock[0])
        monkeypatch.setattr(
            store,
            "list_due_queue_status",
            lambda **_kwargs: [stale],
        )
        service.process_queued_work_status_once(now=clock[0])
    finally:
        service.shutdown()

    callbacks = _callbacks_for_run(store, run_id)
    assert [item["event_type"] for item in callbacks] == ["run.failed"]


def test_callback_delivery_owner_cas_blocks_stale_sender_and_terminal_revert(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 17, 0, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    db_path = tmp_path / "callback-owner.sqlite3"
    first_store = Store(str(db_path))
    record, worker = _reserve_callback_delegation(first_store, suffix="callback_owner")
    callback = first_store.insert_callback_outbox_once(
        callback_id="cb_sender_owner",
        project_id=record["project_id"],
        worker_id=worker["worker_id"],
        run_id=record["current_run_id"],
        event_type="run.queue_status",
        url="https://callback.example.invalid/events",
        payload_json=json.dumps({"callback_ts": start.timestamp()}),
    )
    first_claim = first_store.claim_pending_callback(callback["callback_id"])
    assert first_claim is not None

    clock[0] = start + timedelta(minutes=10)
    second_store = Store(str(db_path))
    assert second_store.reclaim_stale_delivering_callbacks(
        stale_before=clock[0].isoformat(), limit=1
    ) == 1
    second_claim = second_store.claim_pending_callback(callback["callback_id"])
    assert second_claim is not None
    assert second_claim["delivery_generation"] == first_claim["delivery_generation"] + 1

    stale_accept = first_store.mark_callback_http_accepted(
        callback["callback_id"],
        lease_token=first_claim["delivery_lease_token"],
        delivery_generation=first_claim["delivery_generation"],
        attempts=1,
        payload_json="{}",
    )
    terminal = second_store.mark_callback_dead_lettered(
        callback["callback_id"],
        lease_token=second_claim["delivery_lease_token"],
        delivery_generation=second_claim["delivery_generation"],
        attempts=1,
        payload_json="{}",
        last_error="synthetic terminal rejection",
    )
    stale_revert = first_store.mark_callback_pending(
        callback["callback_id"],
        lease_token=first_claim["delivery_lease_token"],
        delivery_generation=first_claim["delivery_generation"],
        attempts=1,
        payload_json="{}",
        last_error="stale sender",
    )

    assert stale_accept is None
    assert terminal is not None and terminal["status"] == "dead_lettered"
    assert stale_revert is None
    assert first_store.get_callback_outbox(callback["callback_id"])["status"] == "dead_lettered"


def test_timeout_releases_exact_active_host_lease(tmp_path, monkeypatch):
    start = datetime(2026, 8, 22, 17, 30, tzinfo=timezone.utc)
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: start.isoformat())
    store = Store(str(tmp_path / "timeout-lease.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="timeout_lease")
    claimed = store.claim_next_queued_run(worker["worker_id"])
    _admitted, lease = _admit_test_run(
        store,
        worker,
        claimed,
        executor_id="timeout-lease-owner",
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        service.process_queued_work_status_once(now=start + timedelta(seconds=61))
    finally:
        service.shutdown()

    assert store.get_run(record["current_run_id"])["state"] == "failed"
    released = store.get_host_run_lease(lease["lease_id"])
    assert released["status"] == "released"
    assert released["release_reason"] == "run_terminal:queue_wait_timeout"


def test_late_runtime_invocation_cannot_beat_expired_wait_deadline(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 17, 45, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: clock[0].isoformat())
    store = Store(str(tmp_path / "deadline-invocation-race.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="deadline_invocation_race"
    )
    claimed = store.claim_next_queued_run(worker["worker_id"])
    _admitted, lease = _admit_test_run(
        store,
        worker,
        claimed,
        executor_id="late-runtime-owner",
    )

    clock[0] = start + timedelta(seconds=61)
    late_invocation = store.mark_run_runtime_invoked(
        str(record["current_run_id"]),
        lease_id=str(lease["lease_id"]),
        executor_id="late-runtime-owner",
    )
    assert late_invocation is None

    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    try:
        result = service.process_queued_work_status_once(now=clock[0])
    finally:
        service.shutdown()

    assert result["timedOut"] == 1
    assert store.get_run(record["current_run_id"])["failure_class"] == "queue_wait_timeout"
    assert [
        item["event_type"]
        for item in _callbacks_for_run(store, record["current_run_id"])
    ] == ["run.failed"]


def test_heal_terminal_fence_releases_old_lease_before_replacement_admission(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store_module, "utc_now", lambda: start.isoformat())
    db_path = tmp_path / "heal-replacement.sqlite3"
    store = Store(str(db_path))
    project = store.create_project("owner-a", "Heal", "Heal", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Heal worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    first = store.create_run(worker["worker_id"], project["project_id"], "First")
    first = store.claim_next_queued_run(worker["worker_id"])
    first, old_lease = _admit_test_run(
        store,
        worker,
        first,
        executor_id="old-heal-owner",
    )
    first = store.mark_run_runtime_invoked(
        first["run_id"],
        lease_id=old_lease["lease_id"],
        executor_id="old-heal-owner",
    )
    replacement = store.create_run(
        worker["worker_id"], project["project_id"], "Replacement"
    )
    competitor = Store(str(db_path))
    admitted_replacement: dict[str, dict] = {}
    original_finalize = store.finalize_run_if_state

    def finalize_then_compete(*args, **kwargs):
        finalized = original_finalize(*args, **kwargs)
        claimed = competitor.claim_next_queued_run(worker["worker_id"])
        assert claimed is not None and claimed["run_id"] == replacement["run_id"]
        admitted_replacement["lease"] = _acquire_test_lease(
            competitor,
            competitor.get_worker(worker["worker_id"]),
            claimed,
            executor_id="replacement-owner",
            mission_limit=1,
        )
        return finalized

    monkeypatch.setattr(store, "finalize_run_if_state", finalize_then_compete)
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(
        service,
        "_collect_completed_run",
        lambda *_args, **_kwargs: {"state": "completed", "output_text": "done"},
    )
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    monkeypatch.setattr(service, "_emit_callback", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_refresh_runtime_info", lambda *_args, **_kwargs: worker)
    try:
        service.heal_worker(worker["worker_id"])
    finally:
        service.shutdown()

    assert store.get_host_run_lease(old_lease["lease_id"])["status"] == "released"
    assert admitted_replacement["lease"]["status"] == "active"


def test_callback_unavailable_and_dead_letter_are_truthful(tmp_path, monkeypatch):
    start = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("GLASSHIVE_QUEUE_WAIT_TIMEOUT_S", "60")
    monkeypatch.setattr(store_module, "utc_now", lambda: start.isoformat())
    store = Store(str(tmp_path / "callback-truth.sqlite3"))
    project = store.create_project(
        "owner-a", "No callback", "Truth", "codex-cli", tenant_id="tenant-a"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="No callback worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        tenant_id="tenant-a",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Wait")
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._now_datetime = lambda: start + timedelta(seconds=61)
    try:
        service.process_queued_work_status_once(now=service._now_datetime())
        durable = store.get_run(run["run_id"])
        assert durable["queue_callback_state"] == "unavailable"
        assert _callbacks_for_run(store, run["run_id"]) == []

        # A durable outbox row exposes terminal transport failure without
        # claiming user-surface delivery.
        callback_id = "cb_dead_letter_test"
        store.insert_callback_outbox_once(
            callback_id=callback_id,
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            event_type="run.failed",
            url="https://callback.example.invalid/events",
            payload_json=json.dumps({"callback_ts": start.timestamp()}),
        )
        claimed_callback = store.claim_pending_callback(callback_id)
        assert claimed_callback is not None
        store.mark_callback_dead_lettered(
            callback_id,
            lease_token=claimed_callback["delivery_lease_token"],
            delivery_generation=claimed_callback["delivery_generation"],
            attempts=1,
            payload_json="{}",
            last_error="synthetic delivery rejection",
        )
        rows = _callbacks_for_run(store, run["run_id"])
        assert rows[-1]["status"] == "dead_lettered"
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("used", "total", "threshold", "state", "ready"),
    [
        (70, 100, "90", "healthy", True),
        (85, 100, "90", "warning", True),
        (95, 100, "90", "critical", False),
    ],
)
def test_storage_pressure_v1_is_typed_and_threshold_injected(
    tmp_path, monkeypatch, used, total, threshold, state, ready
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    monkeypatch.setenv("GLASSHIVE_STORAGE_PRESSURE_CRITICAL_PERCENT", threshold)
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: service_module.shutil._ntuple_diskusage(total, used, total - used),
    )
    store = Store(str(tmp_path / "storage.sqlite3"))

    class ReadyRuntime(StubRuntime):
        def isolated_parallel_readiness(self, **_kwargs):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(store, ReadyRuntime(), reconcile_on_startup=False)
    try:
        result = service.orchestration_capabilities()
    finally:
        service.shutdown()

    pressure = result["storagePressure"]
    assert pressure == {
        "version": 1,
        "state": state,
        "healthy": state != "critical",
        "usedPercent": float(used),
        "availableBytes": total - used,
        "thresholdPercent": float(threshold),
    }
    assert result["isolatedParallelReady"] is ready
    assert "path" not in json.dumps(pressure).lower()


def test_invalid_storage_probe_fails_closed_without_machine_details(tmp_path, monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("/private/host/path unavailable")),
    )
    store = Store(str(tmp_path / "storage-invalid.sqlite3"))

    class ReadyRuntime(StubRuntime):
        def isolated_parallel_readiness(self, **_kwargs):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(store, ReadyRuntime(), reconcile_on_startup=False)
    try:
        result = service.orchestration_capabilities()
    finally:
        service.shutdown()

    assert result["isolatedParallelReady"] is False
    assert result["isolatedParallelReason"] == "storage_pressure_unavailable"
    assert result["storagePressure"] == {
        "version": 1,
        "state": "critical",
        "healthy": False,
        "usedPercent": None,
        "availableBytes": None,
        "thresholdPercent": 90.0,
        "errorCode": "storage_probe_unavailable",
    }
    assert "/private" not in json.dumps(result)


def test_prompt_layer_inventory_is_derived_from_actual_registered_producers():
    bindings = service_module.worker_prompt_layer_producer_bindings()

    assert set(bindings) == {
        "workers_projects_runtime.bootstrap.canonicalize_viventium_feeling_projection",
        "workers_projects_runtime.bootstrap.glasshive_project_agents_md",
        "workers_projects_runtime.bootstrap.glasshive_project_claude_md",
        "workers_projects_runtime.bootstrap.glasshive_project_codex_md",
        "workers_projects_runtime.bootstrap.merge_glasshive_worker_instructions",
        "workers_projects_runtime.coordinator.prompt_manifest",
        "workers_projects_runtime.mcp_server.create_mcp_server",
        "workers_projects_runtime.mcp_server.glasshive_workers_server_instructions",
        "workers_projects_runtime.profile_runtime.HostNativeCliMixin._host_harness_prompt",
        "workers_projects_runtime.profile_runtime.HostNativeCliMixin._host_project_definition",
        "workers_projects_runtime.profile_runtime._apply_codex_developer_instructions",
        "workers_projects_runtime.profile_runtime._instruction_with_completion_contract",
    }
    assert service_module.worker_prompt_layer_producer_names() == (
        "agents_md",
        "claude_md",
        "codex_md",
        "developer_instructions",
        "glasshive_worker_project_contract",
        "harness_prompt",
        "mcp_server_instructions",
        "project_definition",
        "run_instruction",
        "system_instructions",
        "tool_schemas",
        "viventium_feeling_state",
    )


def test_actual_prompt_output_carries_each_nested_producer_identity():
    emitted = bootstrap_module.glasshive_project_agents_md({})

    assert isinstance(emitted, str)
    assert bootstrap_module.worker_prompt_layer_emissions(emitted) == (
        (
            "workers_projects_runtime.bootstrap.merge_glasshive_worker_instructions",
            ("glasshive_worker_project_contract", "system_instructions"),
        ),
        (
            "workers_projects_runtime.bootstrap.glasshive_project_agents_md",
            ("agents_md",),
        ),
    )


def test_non_text_prompt_output_carries_producer_identity():
    emitted = mcp_server_module.create_mcp_server(
        base_url="http://127.0.0.1:1"
    )

    assert bootstrap_module.worker_prompt_layer_emissions(emitted) == (
        (
            "workers_projects_runtime.mcp_server.create_mcp_server",
            ("tool_schemas",),
        ),
    )


def test_removing_registration_from_real_producer_invalidates_actual_output(
    monkeypatch,
):
    producer = bootstrap_module.glasshive_project_agents_md
    producer_ref = producer.__glasshive_worker_prompt_producer_ref__
    monkeypatch.delitem(
        bootstrap_module.WORKER_PROMPT_LAYER_PRODUCER_BINDINGS,
        producer_ref,
    )

    with pytest.raises(RuntimeError, match="Unregistered worker prompt producer"):
        producer({})
    assert service_module.worker_prompt_layer_registration_errors() == (
        "unregistered_prompt_producer",
    )


def test_mismatched_registration_invalidates_actual_producer_boundary(monkeypatch):
    producer = bootstrap_module.glasshive_project_agents_md
    producer_ref = producer.__glasshive_worker_prompt_producer_ref__
    monkeypatch.setitem(
        bootstrap_module.WORKER_PROMPT_LAYER_PRODUCER_BINDINGS,
        producer_ref,
        ("system_instructions",),
    )

    with pytest.raises(RuntimeError, match="Unregistered worker prompt producer"):
        producer({})
    assert service_module.worker_prompt_layer_registration_errors() == (
        "unregistered_prompt_producer",
    )


def test_actual_prompt_boundary_rejects_new_producer_without_registration(
    monkeypatch,
):
    actual = service_module.worker_prompt_layer_actual_producers()

    def unregistered_prompt_producer():
        return "synthetic prompt"

    monkeypatch.setattr(
        service_module,
        "worker_prompt_layer_actual_producers",
        lambda: (*actual, unregistered_prompt_producer),
    )

    snapshot = service_module.worker_prompt_layer_integrity_snapshot(
        include_producer_scope=True
    )
    assert snapshot == {
        "contractVersion": 1,
        "producerScope": "glasshive.worker_prompt_registry",
        "unknownLayerNames": ["unregistered_prompt_producer"],
    }
    assert service_module.valid_worker_prompt_layer_capability(snapshot) is False


def test_unknown_actual_worker_prompt_producer_fails_readiness_and_trace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: service_module.shutil._ntuple_diskusage(100, 50, 50),
    )
    monkeypatch.setitem(
        bootstrap_module.WORKER_PROMPT_LAYER_PRODUCER_BINDINGS,
        "synthetic.unregistered_prompt_producer",
        ("unexpected_worker_layer",),
    )
    store = Store(str(tmp_path / "prompt-layer-unknown.sqlite3"))

    class ReadyRuntime(StubRuntime):
        def isolated_parallel_readiness(self, **_kwargs):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(store, ReadyRuntime(), reconcile_on_startup=False)
    try:
        result = service.orchestration_capabilities()
        trace = service.worker_prompt_layer_trace()
    finally:
        service.shutdown()

    assert result["isolatedParallelReady"] is False
    assert result["isolatedParallelReason"] == "prompt_layers_unknown"
    assert result["promptLayers"] == {
        "contractVersion": 1,
        "producerScope": "glasshive.worker_prompt_registry",
        "unknownLayerNames": ["unexpected_worker_layer"],
    }
    assert trace == {
        "contractVersion": 1,
        "producerScope": "glasshive.worker_prompt_registry",
        "layerNames": list(service_module.worker_prompt_layer_producer_names()),
        "unknownLayerNames": ["unexpected_worker_layer"],
    }


def test_work_trace_persists_event_time_origin_prompt_preflight_and_runtime_invocation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "0")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "0")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "1")
    monkeypatch.setenv("WPR_DOCKER_DISK_RESERVATION_MB", "1")
    store = Store(str(tmp_path / "immutable-work-trace.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None
    origin_ref = "origin_immutable_trace_0001"
    source_event_id = "source-event-private-identity"
    recorded_layer_names = list(service_module.worker_prompt_layer_producer_names())
    try:
        record = service.reserve_delegation(
            tenant_id="tenant-a",
            owner_id="owner-a",
            idempotency_key="immutable-trace-idempotency",
            request_digest="immutable-trace-digest",
            origin_ref=origin_ref,
            title="Immutable trace",
            goal="Persist event-time trace evidence",
            instruction="Create one synthetic trace artifact.",
            origin_surface="telegram",
            worker_name="Trace worker",
            worker_role="worker",
            profile="codex-cli",
            execution_mode="docker",
            bootstrap_bundle={
                "viventium_delegation_identity": {
                    "version": 1,
                    "source_event_id": source_event_id,
                },
                "viventium_delegation_context": {
                    "version": 1,
                    "source_event_id": source_event_id,
                    "source_revision": 7,
                    "triggering_source_segments": [],
                },
            },
        )
        worker = store.get_worker(str(record["worker_id"]))
        run = store.get_run(str(record["initial_run_id"]))
        assert worker is not None and run is not None
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id=service._executor_id
        )
        assert claimed is not None
        admitted, lease = _admit_test_run(
            store, worker, claimed, executor_id=service._executor_id
        )
        preflight = store.record_provider_authorization_preflight(
            admitted["run_id"],
            provider="openai",
            status="authorized",
            failure_class="",
        )
        assert preflight is not None
        replayed_preflight = store.record_provider_authorization_preflight(
            admitted["run_id"],
            provider="openai",
            status="authorized",
            failure_class="",
        )
        assert replayed_preflight is not None
        assert replayed_preflight["trace_event_id"] == preflight["trace_event_id"]
        with pytest.raises(
            RuntimeError,
            match="Provider authorization preflight conflicts with immutable evidence",
        ):
            store.record_provider_authorization_preflight(
                admitted["run_id"],
                provider="openai",
                status="rejected",
                failure_class="provider_unauthorized",
            )
        running = store.mark_run_runtime_invoked(
            admitted["run_id"],
            lease_id=lease["lease_id"],
            executor_id=service._executor_id,
        )
        assert running is not None

        before = store.work_trace_detail(
            run_id=running["run_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        monkeypatch.setitem(
            bootstrap_module.WORKER_PROMPT_LAYER_PRODUCER_BINDINGS,
            "synthetic.changed_after_event",
            ("unexpected_current_layer",),
        )
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE run_attempts SET state = 'corrupted_after_event' WHERE run_id = ?",
                (running["run_id"],),
            )
        after = Store(store.db_path).work_trace_detail(
            run_id=running["run_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
    finally:
        service.shutdown()

    expected_origin = {
        "originRef": "origin_sha256:"
        + hashlib.sha256(origin_ref.encode()).hexdigest(),
        "surface": "telegram",
        "sourceEventRef": "source_sha256:"
        + hashlib.sha256(source_event_id.encode()).hexdigest(),
        "sourceRevision": 7,
    }
    assert before is not None and after is not None
    assert before["traceability"]["origin"] == expected_origin
    assert before["traceability"]["promptLayers"] == {
        "contractVersion": 1,
        "producerScope": "glasshive.worker_prompt_registry",
        "layerNames": recorded_layer_names,
        "unknownLayerNames": [],
    }
    assert before["traceability"]["contractVersion"] == 2
    runtime_invocations = before["traceability"]["runtimeInvocations"]
    assert len(runtime_invocations) == 1
    assert runtime_invocations[0]["runtimeInvocationRef"].startswith(
        "runtime_invocation_sha256:"
    )
    assert runtime_invocations[0]["attemptNumber"] == 1
    assert runtime_invocations[0]["profile"] == "codex-cli"
    assert runtime_invocations[0]["runtime"] == "codex-cli"
    assert runtime_invocations[0]["model"] == worker["model"]
    assert runtime_invocations[0]["runtimeInvokedAt"] == running["runtime_invoked_at"]
    provider_preflights = before["traceability"]["providerAuthorizationPreflights"]
    assert len(provider_preflights) == 1
    assert provider_preflights[0]["attemptNumber"] == 1
    assert provider_preflights[0]["provider"] == "openai"
    assert provider_preflights[0]["status"] == "authorized"
    assert provider_preflights[0]["failureClass"] is None
    assert before["traceability"]["integrity"]["algorithm"] == "sha256-chain-v1"
    assert before["traceability"]["integrity"]["eventCount"] == 7
    assert before["traceability"]["integrity"]["headSha256"].startswith("sha256:")
    assert after["traceability"]["origin"] == before["traceability"]["origin"]
    assert after["traceability"]["promptLayers"] == before["traceability"]["promptLayers"]
    assert after["traceability"]["runtimeInvocations"] == before["traceability"]["runtimeInvocations"]
    assert after["traceability"]["providerAuthorizationPreflights"] == (
        before["traceability"]["providerAuthorizationPreflights"]
    )
    assert after["traceability"]["integrity"]["eventCount"] == (
        before["traceability"]["integrity"]["eventCount"] + 1
    )
    assert after["traceability"]["integrity"]["headSha256"] != (
        before["traceability"]["integrity"]["headSha256"]
    )
    assert before["attemptHistory"][-1]["state"] == "running"
    assert after["attemptHistory"][-1]["state"] == "retry_queued"
    assert after["attemptHistory"][-1]["terminalReason"] == (
        "running_invariant_reconciled"
    )
    with sqlite3.connect(store.db_path) as conn:
        lifecycle_states = [
            json.loads(row[0])["state"]
            for row in conn.execute(
                "SELECT payload_json FROM work_trace_events "
                "WHERE run_id = ? AND event_type = 'lifecycle.attempt' "
                "ORDER BY sequence",
                (running["run_id"],),
            ).fetchall()
        ]
    assert lifecycle_states[-2:] == ["running", "retry_queued"]
    assert store.work_trace_detail(
        run_id=running["run_id"],
        tenant_id="tenant-a",
        owner_id="owner-b",
    ) is None
    serialized = json.dumps(before["traceability"])
    assert source_event_id not in serialized
    assert origin_ref not in serialized
    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE work_trace_events SET payload_json = '{}' WHERE run_id = ?",
                (running["run_id"],),
            )


def test_work_trace_detail_uses_one_sqlite_snapshot_during_concurrent_append(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "work-trace-snapshot.sqlite3"))
    writer_store = Store(store.db_path)
    record, worker = _reserve_callback_delegation(store, suffix="trace_snapshot")
    run_id = str(record["current_run_id"])
    reader_at_trace = Barrier(2)
    writer_committed = Barrier(2)
    original_connect = store._connect

    @contextmanager
    def traced_connect():
        with original_connect() as conn:
            paused = False

            def pause_before_trace_rows(statement: str) -> None:
                nonlocal paused
                normalized = " ".join(str(statement).split())
                if paused or not normalized.startswith(
                    "SELECT * FROM work_trace_events"
                ):
                    return
                paused = True
                reader_at_trace.wait(timeout=10)
                writer_committed.wait(timeout=10)

            conn.set_trace_callback(pause_before_trace_rows)
            yield conn

    monkeypatch.setattr(store, "_connect", traced_connect)

    def append_attempt() -> dict:
        reader_at_trace.wait(timeout=10)
        claimed = writer_store.claim_next_queued_run(
            worker["worker_id"], executor_id="executor-trace-snapshot"
        )
        assert claimed is not None
        writer_committed.wait(timeout=10)
        return claimed

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(append_attempt)
        detail = store.work_trace_detail(
            run_id=run_id,
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        claimed = writer.result(timeout=10)

    assert detail is not None
    assert detail["attemptHistory"] == []
    assert claimed["run_id"] == run_id
    fresh = writer_store.work_trace_detail(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    assert fresh is not None
    assert len(fresh["attemptHistory"]) == 1


def test_running_retry_reconciliation_appends_exact_attempt_snapshot_and_chain_head(
    tmp_path,
):
    store = Store(str(tmp_path / "reconciled-attempt-trace.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="reconciled_trace")
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id="executor-reconciled-trace"
    )
    assert claimed is not None
    admitted, lease = _admit_test_run(
        store,
        worker,
        claimed,
        executor_id="executor-reconciled-trace",
    )
    running = store.mark_run_runtime_invoked(
        admitted["run_id"],
        lease_id=lease["lease_id"],
        executor_id="executor-reconciled-trace",
    )
    assert running is not None
    before = store.work_trace_detail(
        run_id=running["run_id"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert before is not None
    assert before["attemptHistory"][-1]["state"] == "running"

    repaired = store.reconcile_dead_host_run_lease(
        lease_id=lease["lease_id"],
        run_id=running["run_id"],
        expected_attempt_id=running["active_attempt_id"],
        expected_executor_id="executor-reconciled-trace",
        reason="synthetic_dead_executor",
    )

    assert repaired is not None
    assert Store(store.db_path).get_run(running["run_id"])["state"] == "queued"
    durable_attempt = Store(store.db_path).get_run_attempt(
        running["active_attempt_id"]
    )
    assert durable_attempt is not None
    assert durable_attempt["state"] == "retry_queued"
    after = Store(store.db_path).work_trace_detail(
        run_id=running["run_id"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert after is not None
    assert after["attemptHistory"][-1]["state"] == "retry_queued"
    assert after["attemptHistory"][-1]["terminalReason"] == "synthetic_dead_executor"
    assert after["traceability"]["integrity"]["eventCount"] == (
        before["traceability"]["integrity"]["eventCount"] + 1
    )
    assert after["traceability"]["integrity"]["headSha256"] != (
        before["traceability"]["integrity"]["headSha256"]
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            "SELECT sequence, event_type, payload_json, event_sha256 "
            "FROM work_trace_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (running["run_id"],),
        ).fetchone()
    assert latest is not None
    assert latest["event_type"] == "lifecycle.attempt"
    assert json.loads(latest["payload_json"])["state"] == "retry_queued"
    assert after["traceability"]["integrity"]["headSha256"] == (
        f"sha256:{latest['event_sha256']}"
    )


def test_follow_up_run_appends_immutable_origin_prompt_and_runtime_trace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "0")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "0")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "1")
    monkeypatch.setenv("WPR_DOCKER_DISK_RESERVATION_MB", "1")
    store = Store(str(tmp_path / "follow-up-work-trace.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None
    origin_ref = "origin_follow_up_trace_0001"
    source_event_id = "source-follow-up-trace"
    follow_up_origin_ref = "origin_follow_up_trace_0002"
    follow_up_source_event_id = "source-follow-up-trace-second"
    follow_up_prompt_layers = {
        "contractVersion": 1,
        "producerScope": "synthetic.follow_up_host",
        "layerNames": ["follow-up-event-layer"],
    }
    try:
        record = service.reserve_delegation(
            tenant_id="tenant-a",
            owner_id="owner-a",
            idempotency_key="follow-up-trace-idempotency",
            request_digest="follow-up-trace-digest",
            origin_ref=origin_ref,
            title="Follow-up trace",
            goal="Trace every run",
            instruction="Create the initial result.",
            origin_surface="telegram",
            worker_name="Follow-up trace worker",
            worker_role="worker",
            profile="codex-cli",
            execution_mode="docker",
            bootstrap_bundle={
                "viventium_delegation_context": {
                    "version": 1,
                    "source_event_id": source_event_id,
                    "source_revision": 9,
                    "triggering_source_segments": [],
                }
            },
        )
        worker = store.get_worker(str(record["worker_id"]))
        initial = store.get_run(str(record["initial_run_id"]))
        assert worker is not None and initial is not None
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id=service.executor_id
        )
        assert claimed is not None
        admitted, lease = _admit_test_run(
            store, worker, claimed, executor_id=service.executor_id
        )
        initial_running = store.mark_run_runtime_invoked(
            admitted["run_id"],
            lease_id=lease["lease_id"],
            executor_id=service.executor_id,
        )
        assert initial_running is not None
        assert store.finalize_run_if_state(
            initial_running["run_id"],
            "running",
            "completed",
            **_terminal_generation(initial_running, lease),
        ) is not None
        store.release_host_run_lease(
            lease["lease_id"], executor_id=None, reason="initial_completed"
        )

        follow_up = store.create_run(
            worker["worker_id"],
            worker["project_id"],
            "Create the follow-up result.",
            origin_trace={
                "origin_ref": follow_up_origin_ref,
                "source_event_id": follow_up_source_event_id,
                "source_revision": 10,
                "surface": "desktop",
                "prompt_layers": follow_up_prompt_layers,
            },
        )
        store.update_delegation_current_run(
            record["work_ref"],
            tenant_id="tenant-a",
            owner_id="owner-a",
            run_id=follow_up["run_id"],
        )
        claimed_follow_up = store.claim_next_queued_run(
            worker["worker_id"], executor_id=service.executor_id
        )
        assert claimed_follow_up is not None
        admitted_follow_up, follow_up_lease = _admit_test_run(
            store,
            worker,
            claimed_follow_up,
            executor_id=service.executor_id,
        )
        preflight_follow_up = store.record_provider_authorization_preflight(
            admitted_follow_up["run_id"],
            provider="openai",
            status="authorized",
            failure_class="",
        )
        assert preflight_follow_up is not None
        running_follow_up = store.mark_run_runtime_invoked(
            admitted_follow_up["run_id"],
            lease_id=follow_up_lease["lease_id"],
            executor_id=service.executor_id,
        )
        assert running_follow_up is not None
        detail = store.work_trace_detail(
            run_id=running_follow_up["run_id"],
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
    finally:
        service.shutdown()

    assert detail is not None
    assert detail["traceability"]["origin"] == {
        "originRef": "origin_sha256:"
        + hashlib.sha256(follow_up_origin_ref.encode()).hexdigest(),
        "surface": "desktop",
        "sourceEventRef": "source_sha256:"
        + hashlib.sha256(follow_up_source_event_id.encode()).hexdigest(),
        "sourceRevision": 10,
    }
    assert detail["traceability"]["promptLayers"] == follow_up_prompt_layers
    assert detail["traceability"]["contractVersion"] == 2
    assert detail["traceability"]["runtimeInvocations"][0]["attemptNumber"] == 1
    assert detail["traceability"]["runtimeInvocations"][0]["runtimeInvokedAt"] == (
        running_follow_up["runtime_invoked_at"]
    )
    assert detail["traceability"]["providerAuthorizationPreflights"][0]["status"] == (
        "authorized"
    )
    assert detail["traceability"]["integrity"]["eventCount"] == 6


def test_attempt_capacity_and_callback_detail_is_bounded_owner_scoped_and_redacted(
    tmp_path, monkeypatch
):
    start = datetime(2026, 8, 22, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store_module, "utc_now", lambda: start.isoformat())
    store = Store(str(tmp_path / "detail.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="detail")
    run_id = record["current_run_id"]
    for index in range(40):
        current = store.claim_next_queued_run(worker["worker_id"])
        assert current is not None
        store.requeue_run_for_retry(
            run_id,
            retry_after=start.isoformat(),
            **(store.get_run_retry_generation(run_id) or {}),
            error_text="secret /private/path token=abc",
            last_retry_class="host_capacity",
            consume_retry_budget=False,
            capacity_class="resource_pressure",
            capacity_available={"memoryBytes": 4 * 1024**3},
            capacity_required={"memoryBytes": 5 * 1024**3},
            capacity_shortage={"memoryBytes": 1024**3},
            capacity_next_retry_at=(start + timedelta(seconds=index + 1)).isoformat(),
            failure_class="host_capacity",
            failure_retryable=1,
            failure_structured=1,
        )
    store.insert_callback_outbox_once(
        callback_id="cb_detail",
        project_id=record["project_id"],
        worker_id=worker["worker_id"],
        run_id=run_id,
        attempt_number=40,
        event_type="run.queue_status",
        url="https://callback.example.invalid/events",
        payload_json=json.dumps(
                {
                    "callback_ts": start.timestamp(),
                    "attempt_number": 40,
                    "secret": "must-not-project",
                }
        ),
    )
    replay = store.insert_callback_outbox_once(
        callback_id="cb_detail",
        project_id=record["project_id"],
        worker_id=worker["worker_id"],
        run_id=run_id,
        attempt_number=40,
        event_type="run.queue_status",
        url="https://callback.example.invalid/events",
        payload_json=json.dumps(
            {
                "callback_ts": start.timestamp(),
                "attempt_number": 40,
                "secret": "must-not-project",
            }
        ),
    )
    assert replay["_inserted"] is False

    detail = store.work_trace_detail(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-a",
        attempt_limit=16,
        capacity_limit=16,
        callback_limit=16,
    )

    assert detail is not None
    assert len(detail["attemptHistory"]) == 16
    assert detail["attemptHistoryOverflowCount"] == 24
    assert len(detail["capacityAttempts"]) == 1
    assert detail["capacityAttemptOverflowCount"] == 0
    assert len(detail["callbackDeliveries"]) == 1
    callback_delivery = detail["callbackDeliveries"][0]
    assert {
        key: callback_delivery[key]
        for key in (
            "callbackRef",
            "callbackRevision",
            "ledgerSequence",
            "previousEventSha256",
            "attemptNumber",
            "event",
            "status",
            "attempts",
            "createdAt",
            "updatedAt",
            "acceptedAt",
            "resultRevision",
            "resultDigest",
            "deliveryGeneration",
        )
    } == {
        "callbackRef": "callback_sha256:"
        + hashlib.sha256(b"cb_detail").hexdigest(),
        "callbackRevision": 1,
        "ledgerSequence": 1,
        "previousEventSha256": None,
        "attemptNumber": 40,
        "event": "run.queue_status",
        "status": "pending",
        "attempts": 0,
        "createdAt": start.isoformat(),
        "updatedAt": start.isoformat(),
        "acceptedAt": None,
        "resultRevision": 0,
        "resultDigest": None,
        "deliveryGeneration": 0,
    }
    for key in ("eventSha256", "payloadSha256", "authoritySha256"):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", callback_delivery[key])
    with sqlite3.connect(store.db_path) as conn:
        callback_trace_count = conn.execute(
            "SELECT COUNT(*) FROM work_trace_events "
            "WHERE run_id = ? AND event_type = 'callback.delivery'",
            (run_id,),
        ).fetchone()[0]
    assert callback_trace_count == 1
    assert store.work_trace_detail(
        run_id=run_id,
        tenant_id="tenant-a",
        owner_id="owner-b",
    ) is None
    encoded = json.dumps(detail)
    assert "/private" not in encoded
    assert "must-not-project" not in encoded
    assert "lease_id" not in encoded


def test_callback_outbox_rejects_missing_or_wrong_attempt_identity(tmp_path):
    store = Store(str(tmp_path / "callback-attempt-validation.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="callback_attempt_validation"
    )
    run_id = str(record["current_run_id"])
    base_payload = {
        "callback_id": "cb_attempt_validation",
        "callback_ts": 1_700_000_001,
        "event": "run.queued",
        "origin_ref": record["origin_ref"],
        "work_ref": record["work_ref"],
        "worker_id": worker["worker_id"],
        "run_id": run_id,
    }

    with pytest.raises(ValueError, match="attempt"):
        store.insert_callback_outbox_once(
            callback_id="cb_attempt_validation",
            project_id=record["project_id"],
            worker_id=worker["worker_id"],
            run_id=run_id,
            attempt_number=1,
            event_type="run.queued",
            url="https://callback.example.invalid/events",
            payload_json=json.dumps(base_payload),
        )
    with pytest.raises(ValueError, match="attempt"):
        store.insert_callback_outbox_once(
            callback_id="cb_attempt_validation_wrong",
            project_id=record["project_id"],
            worker_id=worker["worker_id"],
            run_id=run_id,
            attempt_number=1,
            event_type="run.queued",
            url="https://callback.example.invalid/events",
            payload_json=json.dumps(
                {
                    **base_payload,
                    "callback_id": "cb_attempt_validation_wrong",
                    "attempt_number": 2,
                }
            ),
        )


def test_callback_transport_retry_refreshes_timestamp_and_preserves_attempt_identity(
    tmp_path, monkeypatch
):
    payloads: list[dict] = []
    signatures: list[str] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = service_module.httpx.Request(
                    "POST", "https://callback.example.invalid/events"
                )
                response = service_module.httpx.Response(
                    self.status_code, request=request
                )
                raise service_module.httpx.HTTPStatusError(
                    "synthetic retry", request=request, response=response
                )

    def post(_url, *, content, headers, timeout):
        _ = timeout
        payloads.append(json.loads(content.decode("utf-8")))
        signatures.append(str(headers.get("X-GlassHive-Signature") or ""))
        return Response(500 if len(payloads) == 1 else 200)

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("GLASSHIVE_EVENTS_HMAC_SECRET", "synthetic-callback-secret")
    monkeypatch.setattr(service_module.httpx, "post", post)
    monkeypatch.setattr(service_module.time, "time", lambda: 1_700_000_001)
    store = Store(str(tmp_path / "callback-stable-retry.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="stable_retry")
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        callback = service._emit_callback(
            worker,
            "run.queued",
            run=store.get_run(record["current_run_id"]),
            message="Queued",
            callback_id="cb_stable_retry",
            insert_once=True,
            submit_delivery=False,
        )
        assert callback is not None
        retry_clock = iter(range(1_700_000_123, 1_700_000_200))
        monkeypatch.setattr(service_module.time, "time", lambda: next(retry_clock))
        service._deliver_callback_record(
            worker,
            callback,
            service._callback_config_for(worker),
        )
    finally:
        service.shutdown()

    assert len(payloads) == 2
    assert [{**payload, "callback_ts": None} for payload in payloads] == [
        {**payloads[0], "callback_ts": None},
        {**payloads[0], "callback_ts": None},
    ]
    assert [payload["callback_id"] for payload in payloads] == [
        "cb_stable_retry",
        "cb_stable_retry",
    ]
    assert [payload["attempt_number"] for payload in payloads] == [None, None]
    assert payloads[0]["callback_ts"] > 1_700_000_001
    assert payloads[1]["callback_ts"] > payloads[0]["callback_ts"]
    assert all(signatures)
    assert signatures[0] != signatures[1]


def test_late_terminal_attempt_cannot_finalize_or_replace_newer_output(tmp_path):
    store = Store(str(tmp_path / "terminal-attempt-fence.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="terminal_attempt_fence")
    run_id = str(record["current_run_id"])

    attempt_one = store.claim_next_queued_run(
        worker["worker_id"], executor_id="terminal-owner-one"
    )
    assert attempt_one is not None
    admitted_one, lease_one = _admit_test_run(
        store, worker, attempt_one, executor_id="terminal-owner-one"
    )
    running_one = store.mark_run_runtime_invoked(
        run_id,
        lease_id=lease_one["lease_id"],
        executor_id="terminal-owner-one",
    )
    assert running_one is not None
    stale_generation = _terminal_generation(running_one, lease_one)
    requeued = store.requeue_run_for_retry(
        run_id,
        retry_after="2000-01-01T00:00:00+00:00",
        **{
            key: value
            for key, value in stale_generation.items()
            if key != "expected_runtime_invoked_at"
        },
        last_retry_class="provider_temporarily_unavailable",
    )
    assert admitted_one["state"] == "admitted"
    assert requeued is not None

    attempt_two = store.claim_next_queued_run(
        worker["worker_id"], executor_id="terminal-owner-two"
    )
    assert attempt_two is not None
    _admitted_two, lease_two = _admit_test_run(
        store, worker, attempt_two, executor_id="terminal-owner-two"
    )
    running_two = store.mark_run_runtime_invoked(
        run_id,
        lease_id=lease_two["lease_id"],
        executor_id="terminal-owner-two",
    )
    assert running_two is not None
    current_generation = _terminal_generation(running_two, lease_two)

    late = store.finalize_run_if_state(
        run_id,
        "running",
        "completed",
        output_text="attempt-one stale output",
        **stale_generation,
    )

    durable = store.get_run(run_id)
    durable_attempt_two = store.get_run_attempt(running_two["active_attempt_id"])
    assert late is None
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["active_attempt_id"] == running_two["active_attempt_id"]
    assert durable["output_text"] == ""
    assert durable_attempt_two is not None
    assert durable_attempt_two["state"] == "running"
    assert durable_attempt_two["ended_at"] is None

    exact = store.finalize_run_if_state(
        run_id,
        "running",
        "completed",
        output_text="attempt-two durable output",
        **current_generation,
    )
    assert exact is not None
    assert exact["state"] == "completed"
    assert exact["output_text"] == "attempt-two durable output"


def test_late_native_settlement_cannot_move_or_overwrite_newer_attempt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_NATIVE_CHILD_RECONCILE_SECONDS", "0.01")
    store = Store(str(tmp_path / "late-native-settlement.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="late_native_settlement"
    )
    run_id = str(record["current_run_id"])

    attempt_one = store.claim_next_queued_run(
        worker["worker_id"], executor_id="native-owner-one"
    )
    assert attempt_one is not None
    _admitted_one, lease_one = _admit_test_run(
        store, worker, attempt_one, executor_id="native-owner-one"
    )
    running_one = store.mark_run_runtime_invoked(
        run_id,
        lease_id=lease_one["lease_id"],
        executor_id="native-owner-one",
    )
    assert running_one is not None
    stale_generation = _terminal_generation(running_one, lease_one)
    assert store.requeue_run_for_retry(
        run_id,
        retry_after="2000-01-01T00:00:00+00:00",
        **{
            key: value
            for key, value in stale_generation.items()
            if key != "expected_runtime_invoked_at"
        },
        last_retry_class="provider_temporarily_unavailable",
    ) is not None

    attempt_two = store.claim_next_queued_run(
        worker["worker_id"], executor_id="native-owner-two"
    )
    assert attempt_two is not None
    _admitted_two, lease_two = _admit_test_run(
        store, worker, attempt_two, executor_id="native-owner-two"
    )
    running_two = store.mark_run_runtime_invoked(
        run_id,
        lease_id=lease_two["lease_id"],
        executor_id="native-owner-two",
    )
    assert running_two is not None
    projection = NativeTeamProjection(provider="codex", observable=True)
    projection.apply(
        {
            "event_type": "provider.child.started",
            "payload": {"childRef": "attempt-two-child", "state": "running"},
        }
    )
    store.update_run(
        run_id,
        native_capabilities_json=json.dumps(
            {"provider": "codex", "providerStream": True, "childProjection": True}
        ),
        native_child_summary_json=json.dumps(projection.summary()),
    )
    service = WorkersProjectsService(
        store, StubRuntime(), max_workers=1, reconcile_on_startup=False
    )
    try:
        settlement = service._settle_native_children(
            worker,
            running_one,
            "attempt-one stale native output",
        )
    finally:
        service.shutdown()

    durable = store.get_run(run_id)
    current_attempt = store.get_run_attempt(running_two["active_attempt_id"])
    durable_work = store.get_delegation(
        str(record["work_ref"]), tenant_id="tenant-a", owner_id="owner-a"
    )
    assert settlement is None
    assert durable is not None
    assert durable["state"] == "running"
    assert durable["active_attempt_id"] == running_two["active_attempt_id"]
    assert durable["runtime_invoked_at"] == running_two["runtime_invoked_at"]
    assert durable["output_text"] == ""
    assert current_attempt is not None
    assert current_attempt["state"] == "running"
    assert current_attempt["ended_at"] is None
    assert durable_work is not None
    assert durable_work["current_run_id"] == run_id


def test_terminal_callback_rejects_stale_content_and_inserts_exact_result_once(
    tmp_path,
):
    store = Store(str(tmp_path / "terminal-callback-fence.sqlite3"))
    record, worker = _reserve_callback_delegation(store, suffix="terminal_callback_fence")
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    run_id = str(record["current_run_id"])
    try:
        attempt_one = store.claim_next_queued_run(
            worker["worker_id"], executor_id="callback-owner-one"
        )
        assert attempt_one is not None
        _admitted_one, lease_one = _admit_test_run(
            store, worker, attempt_one, executor_id="callback-owner-one"
        )
        running_one = store.mark_run_runtime_invoked(
            run_id,
            lease_id=lease_one["lease_id"],
            executor_id="callback-owner-one",
        )
        assert running_one is not None
        stale_generation = _terminal_generation(running_one, lease_one)
        assert store.requeue_run_for_retry(
            run_id,
            retry_after="2000-01-01T00:00:00+00:00",
            **{
                key: value
                for key, value in stale_generation.items()
                if key != "expected_runtime_invoked_at"
            },
            last_retry_class="provider_temporarily_unavailable",
        ) is not None

        attempt_two = store.claim_next_queued_run(
            worker["worker_id"], executor_id="callback-owner-two"
        )
        assert attempt_two is not None
        _admitted_two, lease_two = _admit_test_run(
            store, worker, attempt_two, executor_id="callback-owner-two"
        )
        running_two = store.mark_run_runtime_invoked(
            run_id,
            lease_id=lease_two["lease_id"],
            executor_id="callback-owner-two",
        )
        assert running_two is not None
        exact = store.finalize_run_if_state(
            run_id,
            "running",
            "completed",
            output_text="FINAL REPORT:\nAttempt two durable output.",
            **_terminal_generation(running_two, lease_two),
        )
        assert exact is not None

        stale = service._emit_callback(
            worker,
            "run.completed",
            run={
                **running_one,
                "state": "completed",
                "output_text": "FINAL REPORT:\nAttempt one stale output.",
            },
            message="Attempt one stale callback content.",
            submit_delivery=False,
        )
        assert stale is None
        assert _callbacks_for_run(store, run_id) == []

        current = service._emit_callback(
            worker,
            "run.completed",
            run=exact,
            message="caller text must not replace durable result",
            submit_delivery=False,
        )
        duplicate = service._emit_callback(
            worker,
            "run.completed",
            run=exact,
            message="different caller text must not replace immutable result",
            submit_delivery=False,
        )
    finally:
        service.shutdown()

    callbacks = _callbacks_for_run(store, run_id)
    assert current is not None and current["_inserted"] is True
    assert duplicate is not None and duplicate["_inserted"] is False
    assert len(callbacks) == 1
    payload = json.loads(callbacks[0]["payload_json"])
    assert payload["attempt_number"] == 2
    assert "Attempt two durable output" in payload["message"]
    assert "caller text" not in payload["message"]
    assert "Attempt one" not in payload["message"]


def test_terminal_callback_cas_rejects_payload_when_durable_result_changes(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "terminal-callback-content-race.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="terminal_callback_content_race"
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    run_id = str(record["current_run_id"])
    try:
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id="callback-content-owner"
        )
        assert claimed is not None
        _admitted, lease = _admit_test_run(
            store, worker, claimed, executor_id="callback-content-owner"
        )
        running = store.mark_run_runtime_invoked(
            run_id,
            lease_id=lease["lease_id"],
            executor_id="callback-content-owner",
        )
        assert running is not None
        terminal = store.finalize_run_if_state(
            run_id,
            "running",
            "completed",
            output_text="FINAL REPORT:\nResult A from the captured snapshot.",
            **_terminal_generation(running, lease),
        )
        assert terminal is not None

        original_insert = store.insert_terminal_callback_outbox_if_current

        def mutate_result_then_insert(**kwargs):
            updated = store.update_run(
                run_id,
                output_text="FINAL REPORT:\nResult B from durable storage.",
            )
            assert updated is not None
            return original_insert(**kwargs)

        monkeypatch.setattr(
            store,
            "insert_terminal_callback_outbox_if_current",
            mutate_result_then_insert,
        )
        raced = service._emit_callback(
            worker,
            "run.completed",
            run=terminal,
            message="Result A",
            submit_delivery=False,
        )
        assert raced is None
        assert _callbacks_for_run(store, run_id) == []

        monkeypatch.setattr(
            store,
            "insert_terminal_callback_outbox_if_current",
            original_insert,
        )
        assert service._reconcile_terminal_callback_intents() == 1
    finally:
        service.shutdown()

    callbacks = _callbacks_for_run(store, run_id)
    assert len(callbacks) == 1
    payload = json.loads(callbacks[0]["payload_json"])
    assert "Result B from durable storage" in payload["message"]
    assert "Result A from the captured snapshot" not in payload["message"]


def test_recurring_callback_tick_retries_terminal_reconciliation_once(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "recurring-terminal-reconciliation.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="recurring_terminal_reconciliation"
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._deliver_callback_record = lambda *_args, **_kwargs: None
    service._replay_pending_callbacks = lambda **_kwargs: None
    service._startup_recovery_thread.join(timeout=2)
    run_id = str(record["current_run_id"])
    try:
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id="reconcile-owner"
        )
        assert claimed is not None
        _admitted, lease = _admit_test_run(
            store, worker, claimed, executor_id="reconcile-owner"
        )
        running = store.mark_run_runtime_invoked(
            run_id,
            lease_id=lease["lease_id"],
            executor_id="reconcile-owner",
        )
        assert running is not None
        terminal = store.finalize_run_if_state(
            run_id,
            "running",
            "completed",
            output_text="FINAL REPORT:\nRecurring recovery output.",
            **_terminal_generation(running, lease),
        )
        assert terminal is not None

        original_scan = store.list_terminal_runs_missing_callback_intent
        scan_calls = 0

        def transient_scan(*, created_before=None, limit=50):
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                raise sqlite3.OperationalError("synthetic transient scan failure")
            return original_scan(created_before=created_before, limit=limit)

        monkeypatch.setattr(
            store, "list_terminal_runs_missing_callback_intent", transient_scan
        )

        service._callback_retry_tick()
        assert _callbacks_for_run(store, run_id) == []
        service._callback_retry_tick()
        service._callback_retry_tick()
    finally:
        service.shutdown()

    callbacks = _callbacks_for_run(store, run_id)
    assert scan_calls == 3
    assert len(callbacks) == 1
    assert callbacks[0]["event_type"] == "run.completed"
    assert callbacks[0]["attempt_number"] == 1


def test_terminal_callback_reconciliation_skips_ineligible_rows_and_survives_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_pending_callbacks",
        lambda _self, **_kwargs: None,
    )
    db_path = tmp_path / "terminal-reconciliation-fairness.sqlite3"
    store = Store(str(db_path))
    first_service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    first_service._startup_recovery_thread.join(timeout=2)
    try:
        callbackless_project = store.create_project(
            "owner-a", "Callbackless", "No callback transport", "codex-cli"
        )
        callbackless_worker = store.create_worker(
            callbackless_project["project_id"],
            "owner-a",
            "Callbackless worker",
            "worker",
            "codex-cli",
            "codex-cli",
            "codex-cli",
            "test-model",
        )
        eligible_project = store.create_project(
            "owner-a", "Eligible", "Durable callback transport", "codex-cli"
        )
        eligible_worker = store.create_worker(
            eligible_project["project_id"],
            "owner-a",
            "Eligible worker",
            "worker",
            "codex-cli",
            "codex-cli",
            "codex-cli",
            "test-model",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "https://callback.example.invalid/events"
                }
            },
        )
        base = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        for index in range(60):
            run = store.create_run(
                callbackless_worker["worker_id"],
                callbackless_project["project_id"],
                f"Callbackless terminal run {index}",
            )
            store.update_run(
                run["run_id"],
                state="completed",
                ended_at=(base + timedelta(seconds=index)).isoformat(),
                output_text=f"Callbackless result {index}",
            )

        eligible_run_ids: list[str] = []
        for index in range(51):
            run = store.create_run(
                eligible_worker["worker_id"],
                eligible_project["project_id"],
                f"Eligible terminal run {index}",
            )
            store.update_run(
                run["run_id"],
                state="completed",
                ended_at=(base + timedelta(minutes=10, seconds=index)).isoformat(),
                output_text=f"Eligible result {index}",
            )
            eligible_run_ids.append(str(run["run_id"]))

        assert first_service._reconcile_terminal_callback_intents(limit=50) == 50
    finally:
        first_service.shutdown()

    restarted_store = Store(str(db_path))
    restarted_service = WorkersProjectsService(
        restarted_store, StubRuntime(), reconcile_on_startup=False
    )
    try:
        restarted_service._startup_recovery_thread.join(timeout=5)
        assert not restarted_service._startup_recovery_thread.is_alive()
        restarted_service._callback_retry_tick()
        restarted_service._callback_retry_tick()
    finally:
        restarted_service.shutdown()

    with sqlite3.connect(db_path) as conn:
        eligible_callbacks = conn.execute(
            "SELECT run_id, COUNT(*) FROM callback_outbox "
            "WHERE run_id IN ("
            + ",".join("?" for _ in eligible_run_ids)
            + ") GROUP BY run_id",
            eligible_run_ids,
        ).fetchall()
        callbackless_callbacks = conn.execute(
            "SELECT COUNT(*) FROM callback_outbox "
            "WHERE worker_id = ?",
            (callbackless_worker["worker_id"],),
        ).fetchone()[0]
    assert len(eligible_callbacks) == 51
    assert all(count == 1 for _run_id, count in eligible_callbacks)
    assert callbackless_callbacks == 0


@pytest.mark.parametrize("receipt_status", ["accepted", "idempotent"])
def test_terminal_callback_result_change_supersedes_old_identity_before_delivery(
    tmp_path, monkeypatch, receipt_status
):
    delivered_payloads: list[dict] = []

    class AcceptedResponse:
        status_code = 200

        def __init__(self, payload: dict):
            self.payload = payload

        def json(self):
            return {
                "callback_status": receipt_status,
                "callback_id": self.payload["callback_id"],
                "run_id": self.payload["run_id"],
                "result_revision": self.payload["result_revision"],
                "result_digest": self.payload["result_digest"],
                "current_callback_id": self.payload["callback_id"],
                "current_result_revision": self.payload["result_revision"],
                "current_result_digest": self.payload["result_digest"],
            }

        def raise_for_status(self):
            return None

    def post(_url, *, content, headers, timeout):
        _ = headers, timeout
        payload = json.loads(content.decode("utf-8"))
        delivered_payloads.append(payload)
        return AcceptedResponse(payload)

    monkeypatch.setattr(service_module.httpx, "post", post)
    store = Store(str(tmp_path / "terminal-result-identity.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="terminal_result_identity"
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    run_id = str(record["current_run_id"])
    try:
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id="result-identity-owner"
        )
        assert claimed is not None
        _admitted, lease = _admit_test_run(
            store, worker, claimed, executor_id="result-identity-owner"
        )
        running = store.mark_run_runtime_invoked(
            run_id,
            lease_id=lease["lease_id"],
            executor_id="result-identity-owner",
        )
        assert running is not None
        terminal_a = store.finalize_run_if_state(
            run_id,
            "running",
            "completed",
            output_text="FINAL REPORT:\nDurable result A.",
            **_terminal_generation(running, lease),
        )
        assert terminal_a is not None
        callback_a = service._emit_callback(
            worker,
            "run.completed",
            run=terminal_a,
            submit_delivery=False,
        )
        assert callback_a is not None

        terminal_b = store.update_run(
            run_id,
            output_text="FINAL REPORT:\nDurable result B.",
        )
        assert terminal_b is not None
        service._deliver_callback_record(
            worker,
            callback_a,
            service._callback_config_for(worker),
        )

        stale = store.get_callback_outbox(str(callback_a["callback_id"]))
        assert delivered_payloads == []
        assert stale is not None
        assert stale["status"] == "superseded"
        assert stale["http_accepted_at"] is None

        assert service._reconcile_terminal_callback_intents() == 1
        callbacks = _callbacks_for_run(store, run_id)
        callback_b = next(
            item
            for item in callbacks
            if str(item["callback_id"]) != str(callback_a["callback_id"])
        )
        payload_b = json.loads(callback_b["payload_json"])
        assert callback_b["result_digest"] == Store.terminal_result_digest(
            terminal_b
        )
        assert payload_b["result_digest"] == callback_b["result_digest"]
        assert "Durable result B" in payload_b["message"]
        assert "Durable result A" not in payload_b["message"]
        service._deliver_callback_record(
            worker,
            callback_b,
            service._callback_config_for(worker),
        )
        assert service._reconcile_terminal_callback_intents() == 0
        assert len(_callbacks_for_run(store, run_id)) == 2
    finally:
        service.shutdown()

    assert len(delivered_payloads) == 1
    assert delivered_payloads[0]["callback_id"] != callback_a["callback_id"]
    assert "Durable result B" in delivered_payloads[0]["message"]
    accepted_b = store.get_callback_outbox(delivered_payloads[0]["callback_id"])
    assert accepted_b is not None
    assert accepted_b["status"] == "http_accepted"
    assert accepted_b["http_accepted_at"]


def test_terminal_callback_receipt_accepts_exact_idempotent_and_rejects_legacy_duplicate():
    payload = {
        "callback_id": f"cb_terminal_{'a' * 64}",
        "run_id": "run_receipt_contract",
        "result_revision": 3,
        "result_digest": f"sha256:{'b' * 64}",
    }

    class Response:
        def __init__(
            self,
            status: str,
            *,
            status_code: int = 200,
            current_callback_id: str | None = None,
            current_result_digest: str | None = None,
        ):
            self.status = status
            self.status_code = status_code
            self.current_callback_id = current_callback_id or payload["callback_id"]
            self.current_result_digest = current_result_digest or payload["result_digest"]

        def json(self):
            return {
                "callback_status": self.status,
                "callback_id": payload["callback_id"],
                "run_id": payload["run_id"],
                "result_revision": payload["result_revision"],
                "result_digest": payload["result_digest"],
                "current_callback_id": self.current_callback_id,
                "current_result_revision": payload["result_revision"],
                "current_result_digest": self.current_result_digest,
            }

    assert WorkersProjectsService._terminal_callback_response_decision(
        Response("accepted"), payload
    ) == ("accepted", 3)
    assert WorkersProjectsService._terminal_callback_response_decision(
        Response("idempotent"), payload
    ) == ("accepted", 3)
    assert WorkersProjectsService._terminal_callback_response_decision(
        Response("duplicate"), payload
    ) == ("invalid", 0)
    assert WorkersProjectsService._terminal_callback_response_decision(
        Response(
            "conflict",
            status_code=409,
            current_callback_id=f"cb_terminal_{'c' * 64}",
            current_result_digest=f"sha256:{'d' * 64}",
        ),
        payload,
    ) == ("conflict", 3)


def test_interrupted_run_uses_cancelled_terminal_result_wire_identity(tmp_path, monkeypatch):
    delivered_payloads: list[dict] = []

    class AcceptedResponse:
        status_code = 200

        def __init__(self, payload: dict):
            self.payload = payload

        def json(self):
            return {
                "callback_status": "accepted",
                "callback_id": self.payload["callback_id"],
                "run_id": self.payload["run_id"],
                "result_revision": self.payload["result_revision"],
                "result_digest": self.payload["result_digest"],
                "current_callback_id": self.payload["callback_id"],
                "current_result_revision": self.payload["result_revision"],
                "current_result_digest": self.payload["result_digest"],
            }

        def raise_for_status(self):
            return None

    def post(_url, *, content, headers, timeout):
        _ = headers, timeout
        delivered = json.loads(content.decode("utf-8"))
        delivered_payloads.append(delivered)
        return AcceptedResponse(delivered)

    monkeypatch.setattr(service_module.httpx, "post", post)
    store = Store(str(tmp_path / "interrupted-terminal-wire.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="interrupted_terminal_wire"
    )
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    run_id = str(record["current_run_id"])
    try:
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id="interrupted-wire-owner"
        )
        assert claimed is not None
        _admitted, lease = _admit_test_run(
            store, worker, claimed, executor_id="interrupted-wire-owner"
        )
        running = store.mark_run_runtime_invoked(
            run_id,
            lease_id=lease["lease_id"],
            executor_id="interrupted-wire-owner",
        )
        assert running is not None
        interrupted = store.finalize_run_if_state(
            run_id,
            "running",
            "interrupted",
            error_text="Synthetic provider interruption.",
            **_terminal_generation(running, lease),
        )
        assert interrupted is not None

        callback = service._emit_callback(
            worker,
            "run.interrupted",
            run=interrupted,
            submit_delivery=False,
        )
        assert callback is not None
        payload = json.loads(callback["payload_json"])
        assert payload["event"] == "run.interrupted"
        assert payload["work_state"] == "cancelled"
        assert payload["work_terminal"] is True
        assert payload["result_state"] == "cancelled"
        assert payload["result_revision"] == interrupted["terminal_result_revision"]
        assert payload["callback_id"].startswith("cb_terminal_")
        assert payload["callback_id"] == Store.terminal_callback_id(
            run_id=run_id,
            state="cancelled",
            ended_at=interrupted["ended_at"],
            attempt_number=payload["attempt_number"],
            result_revision=payload["result_revision"],
            result_digest=payload["result_digest"],
        )
        claimed_callback = store.claim_pending_callback(payload["callback_id"])
        assert claimed_callback is not None
        assert claimed_callback["status"] == "delivering"
        assert claimed_callback["event_type"] == "run.interrupted"
        claimed_payload = json.loads(claimed_callback["payload_json"])
        assert claimed_payload["work_state"] == "cancelled"
        assert claimed_payload["result_state"] == "cancelled"
        pending_again = store.mark_callback_pending(
            payload["callback_id"],
            lease_token=str(claimed_callback["delivery_lease_token"]),
            delivery_generation=int(claimed_callback["delivery_generation"]),
            attempts=0,
            payload_json=str(claimed_callback["payload_json"]),
            last_error="synthetic interrupted delivery retry",
        )
        assert pending_again is not None
        assert pending_again["status"] == "pending"
        service._deliver_callback_record(
            worker,
            pending_again,
            service._callback_config_for(worker),
        )
        accepted = store.get_callback_outbox(payload["callback_id"])
        assert accepted is not None
        assert accepted["status"] == "http_accepted"
        assert accepted["http_accepted_at"]
        accepted_payload = json.loads(accepted["payload_json"])
        assert accepted_payload["event"] == "run.interrupted"
        assert accepted_payload["work_state"] == "cancelled"
        assert accepted_payload["result_state"] == "cancelled"
        assert len(delivered_payloads) == 1
        assert delivered_payloads[0]["work_state"] == "cancelled"
        assert delivered_payloads[0]["result_state"] == "cancelled"
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("current_callback_id", "current_result_digest", "expected"),
    [
        (f"cb_terminal_{'c' * 64}", f"sha256:{'d' * 64}", ("superseded", 4)),
        (f"cb_terminal_{'c' * 63}", f"sha256:{'d' * 64}", ("invalid", 0)),
        (f"cb_terminal_{'c' * 64}suffix", f"sha256:{'d' * 64}", ("invalid", 0)),
        (f"cb_terminal_{'c' * 64}", f"sha256:{'d' * 63}", ("invalid", 0)),
        (f"cb_terminal_{'c' * 64}", f"sha256:{'d' * 64}suffix", ("invalid", 0)),
    ],
)
def test_terminal_callback_superseded_receipt_requires_full_canonical_identity(
    current_callback_id, current_result_digest, expected
):
    payload = {
        "callback_id": f"cb_terminal_{'a' * 64}",
        "run_id": "run_receipt_contract",
        "result_revision": 3,
        "result_digest": f"sha256:{'b' * 64}",
    }

    class Response:
        status_code = 409

        def json(self):
            return {
                "callback_status": "superseded",
                "callback_id": payload["callback_id"],
                "run_id": payload["run_id"],
                "result_revision": payload["result_revision"],
                "result_digest": payload["result_digest"],
                "current_callback_id": current_callback_id,
                "current_result_revision": 4,
                "current_result_digest": current_result_digest,
            }

    assert WorkersProjectsService._terminal_callback_response_decision(
        Response(), payload
    ) == expected


def test_terminal_callback_result_change_during_http_cannot_accept_stale_identity(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "terminal-http-result-race.sqlite3"))
    record, worker = _reserve_callback_delegation(
        store, suffix="terminal_http_result_race"
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    run_id = str(record["current_run_id"])

    class AcceptedResponse:
        status_code = 200

        def raise_for_status(self):
            return None

    def post(_url, *, content, headers, timeout):
        _ = content, headers, timeout
        updated = store.update_run(
            run_id,
            output_text="FINAL REPORT:\nResult B won during HTTP delivery.",
        )
        assert updated is not None
        return AcceptedResponse()

    monkeypatch.setattr(service_module.httpx, "post", post)
    try:
        claimed = store.claim_next_queued_run(
            worker["worker_id"], executor_id="http-race-owner"
        )
        assert claimed is not None
        _admitted, lease = _admit_test_run(
            store, worker, claimed, executor_id="http-race-owner"
        )
        running = store.mark_run_runtime_invoked(
            run_id,
            lease_id=lease["lease_id"],
            executor_id="http-race-owner",
        )
        assert running is not None
        terminal_a = store.finalize_run_if_state(
            run_id,
            "running",
            "completed",
            output_text="FINAL REPORT:\nResult A entered delivery.",
            **_terminal_generation(running, lease),
        )
        assert terminal_a is not None
        callback_a = service._emit_callback(
            worker,
            "run.completed",
            run=terminal_a,
            submit_delivery=False,
        )
        assert callback_a is not None

        service._deliver_callback_record(
            worker,
            callback_a,
            service._callback_config_for(worker),
        )
        stale = store.get_callback_outbox(str(callback_a["callback_id"]))
        assert stale is not None
        assert stale["status"] == "superseded"
        assert stale["http_accepted_at"] is None
        assert service._reconcile_terminal_callback_intents() == 1
        assert service._reconcile_terminal_callback_intents() == 0
    finally:
        service.shutdown()

    callbacks = _callbacks_for_run(store, run_id)
    assert len(callbacks) == 2
    assert len({str(item["callback_id"]) for item in callbacks}) == 2
    current = next(item for item in callbacks if item["status"] == "pending")
    current_payload = json.loads(current["payload_json"])
    assert "Result B won during HTTP delivery" in current_payload["message"]


def test_receiver_cas_rejects_result_a_when_b_wins_inside_a_http_call(
    tmp_path, monkeypatch
):
    sender = Store(str(tmp_path / "terminal-revision-sender.sqlite3"))
    receiver = Store(str(tmp_path / "terminal-revision-receiver.sqlite3"))
    record, worker = _reserve_callback_delegation(
        sender, suffix="terminal_receiver_cas"
    )
    service = WorkersProjectsService(
        sender, StubRuntime(), reconcile_on_startup=False
    )
    run_id = str(record["current_run_id"])
    receiver_scope = "synthetic-viventium-receiver"
    observed_responses: list[str] = []
    mutation_started = False

    class ReceiverResponse:
        def __init__(self, decision: dict):
            self.status_code = int(decision["http_status"])
            self._body = {
                key: value
                for key, value in decision.items()
                if key != "http_status"
            }

        def json(self):
            return dict(self._body)

        def raise_for_status(self):
            if self.status_code >= 400:
                request = service_module.httpx.Request(
                    "POST", "https://callback.example.invalid/events"
                )
                response = service_module.httpx.Response(
                    self.status_code,
                    request=request,
                    json=self._body,
                )
                raise service_module.httpx.HTTPStatusError(
                    "receiver rejected stale terminal result",
                    request=request,
                    response=response,
                )

    def post(_url, *, content, headers, timeout):
        nonlocal mutation_started
        _ = headers, timeout
        payload = json.loads(content.decode("utf-8"))
        revision = payload.get("result_revision")
        assert isinstance(revision, int) and not isinstance(revision, bool)
        assert revision > 0

        if revision == 1 and not mutation_started:
            mutation_started = True
            terminal_b = sender.update_run(
                run_id,
                output_text=(
                    "FINAL REPORT:\nResult B became canonical inside A HTTP."
                ),
            )
            assert terminal_b is not None
            assert terminal_b["terminal_result_revision"] == 2
            assert service._reconcile_terminal_callback_intents() == 1
            callback_b = next(
                item
                for item in _callbacks_for_run(sender, run_id)
                if int(item["result_revision"] or 0) == 2
            )
            service._deliver_callback_record(
                worker,
                callback_b,
                service._callback_config_for(worker),
            )

        decision = receiver.accept_terminal_callback_result(
            receiver_scope=receiver_scope,
            payload=payload,
        )
        observed_responses.append(str(decision["callback_status"]))
        return ReceiverResponse(decision)

    monkeypatch.setattr(service_module.httpx, "post", post)
    try:
        claimed = sender.claim_next_queued_run(
            worker["worker_id"], executor_id="receiver-cas-owner"
        )
        assert claimed is not None
        _admitted, lease = _admit_test_run(
            sender, worker, claimed, executor_id="receiver-cas-owner"
        )
        running = sender.mark_run_runtime_invoked(
            run_id,
            lease_id=lease["lease_id"],
            executor_id="receiver-cas-owner",
        )
        assert running is not None
        terminal_a = sender.finalize_run_if_state(
            run_id,
            "running",
            "completed",
            output_text="FINAL REPORT:\nResult A entered HTTP.",
            **_terminal_generation(running, lease),
        )
        assert terminal_a is not None
        assert terminal_a["terminal_result_revision"] == 1
        callback_a = service._emit_callback(
            worker,
            "run.completed",
            run=terminal_a,
            submit_delivery=False,
        )
        assert callback_a is not None

        service._deliver_callback_record(
            worker,
            callback_a,
            service._callback_config_for(worker),
        )
    finally:
        service.shutdown()

    receiver_current = receiver.get_terminal_callback_result(
        receiver_scope=receiver_scope,
        run_id=run_id,
    )
    assert receiver_current is not None
    assert receiver_current["result_revision"] == 2
    assert "Result B became canonical" in receiver_current["payload_json"]
    receiver_attempts = receiver.list_terminal_callback_result_attempts(
        receiver_scope=receiver_scope,
        run_id=run_id,
    )
    assert [item["status"] for item in receiver_attempts] == [
        "accepted",
        "superseded",
    ]
    assert observed_responses == ["accepted", "superseded"]

    sender_callbacks = _callbacks_for_run(sender, run_id)
    sender_a = next(
        item for item in sender_callbacks if int(item["result_revision"] or 0) == 1
    )
    sender_b = next(
        item for item in sender_callbacks if int(item["result_revision"] or 0) == 2
    )
    assert sender_a["status"] == "superseded"
    assert sender_a["last_error"] == "receiver_result_superseded"
    assert sender_b["status"] == "http_accepted"


def test_unusable_viventium_callback_contracts_do_not_starve_valid_row_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_pending_callbacks",
        lambda _self, **_kwargs: None,
    )
    db_path = tmp_path / "unusable-viventium-reconciliation.sqlite3"
    store = Store(str(db_path))
    first_service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    first_service._startup_recovery_thread.join(timeout=2)
    try:
        unusable_project = store.create_project(
            "owner-a", "Unusable callbacks", "Classify unusable callbacks", "codex-cli"
        )
        unusable_worker = store.create_worker(
            unusable_project["project_id"],
            "owner-a",
            "Unusable callback worker",
            "worker",
            "codex-cli",
            "codex-cli",
            "codex-cli",
            "test-model",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": (
                        "https://callback.example.invalid"
                        "/api/viventium/glasshive/callback"
                    )
                }
            },
        )
        valid_project = store.create_project(
            "owner-a", "Valid callback", "Reach the valid callback", "codex-cli"
        )
        valid_worker = store.create_worker(
            valid_project["project_id"],
            "owner-a",
            "Valid callback worker",
            "worker",
            "codex-cli",
            "codex-cli",
            "codex-cli",
            "test-model",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "https://callback.example.invalid/events"
                }
            },
        )
        base = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        unusable_run_ids: list[str] = []
        for index in range(60):
            run = store.create_run(
                unusable_worker["worker_id"],
                unusable_project["project_id"],
                f"Unusable terminal callback {index}",
            )
            store.update_run(
                run["run_id"],
                state="completed",
                ended_at=(base + timedelta(seconds=index)).isoformat(),
                output_text=f"Unusable result {index}",
            )
            unusable_run_ids.append(str(run["run_id"]))

        valid_run = store.create_run(
            valid_worker["worker_id"],
            valid_project["project_id"],
            "Valid terminal callback",
        )
        store.update_run(
            valid_run["run_id"],
            state="completed",
            ended_at=(base + timedelta(minutes=10)).isoformat(),
            output_text="Valid terminal result",
        )

        assert first_service._reconcile_terminal_callback_intents(limit=50) == 0
    finally:
        first_service.shutdown()

    restarted_store = Store(str(db_path))
    restarted_service = WorkersProjectsService(
        restarted_store, StubRuntime(), reconcile_on_startup=False
    )
    try:
        restarted_service._startup_recovery_thread.join(timeout=5)
        assert not restarted_service._startup_recovery_thread.is_alive()
        restarted_service._callback_retry_tick()
        restarted_service._callback_retry_tick()
    finally:
        restarted_service.shutdown()

    valid_callbacks = restarted_store.list_callback_outbox_for_run(
        str(valid_run["run_id"]),
        tenant_id="local",
        owner_id="owner-a",
        limit=10,
    )
    assert len(valid_callbacks) == 1
    with sqlite3.connect(db_path) as conn:
        unavailable = conn.execute(
            "SELECT run_id, status, reason_code "
            "FROM terminal_callback_reconciliations "
            "WHERE run_id IN ("
            + ",".join("?" for _ in unusable_run_ids)
            + ")",
            unusable_run_ids,
        ).fetchall()
        unusable_outbox = conn.execute(
            "SELECT COUNT(*) FROM callback_outbox WHERE worker_id = ?",
            (unusable_worker["worker_id"],),
        ).fetchone()[0]
    assert len(unavailable) == 60
    assert all(status == "unavailable" for _run_id, status, _reason in unavailable)
    assert all(
        reason == "callback_context_incomplete"
        for _run_id, _status, reason in unavailable
    )
    assert unusable_outbox == 0
