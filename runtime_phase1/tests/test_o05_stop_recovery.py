"""O05 correction: Stop keeps its fence until the exact generation is proven gone,
recovers as the same cancellation, and commits one cancellation with terminal truth."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import workers_projects_runtime.service as service_module
from workers_projects_runtime.store import Store

from test_o05_prelaunch_stop import (
    _ExactHostRuntime,
    _confirm,
    _host_service,
    _invoked_host_run,
)
from test_host_run_leases import _admit_and_invoke, _running_host_run


@pytest.fixture(autouse=True)
def _synthetic_healthy_storage_probe(monkeypatch):
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: service_module.shutil._ntuple_diskusage(100, 50, 50),
    )


PUBLISHED = {
    "pid": 4242,
    "process_group": 4242,
    "process_start_identity": "ps-lstart:Tue Sep 22 21:00:00 2026",
    "startup_identity_kind": "host_process",
    "startup_session_id": "session-4242",
}


def _publish(store, lease, executor_id, **identity):
    store.heartbeat_host_run_lease(
        lease["lease_id"], executor_id=executor_id, lease_ttl_s=30,
        **{**PUBLISHED, **identity},
    )


class _CleanupRuntime(_ExactHostRuntime):
    """Exact pre-confirmation cleanup with scripted results; never an unscoped signal."""

    def __init__(self, results=()):
        super().__init__()
        self.results = list(results)
        self.cleanups = []

    def cleanup_unconfirmed_run_start(self, worker, run_id, identity):
        self.cleanups.append((run_id, dict(identity)))
        result = self.results.pop(0) if self.results else False
        if isinstance(result, BaseException):
            raise result
        return result


class _InterruptFailsOnce(_ExactHostRuntime):
    def __init__(self):
        super().__init__()
        self.fail = True

    def interrupt_worker(self, worker, run_id=None):
        if self.fail:
            self.fail = False
            raise RuntimeError("synthetic temporary interrupt failure")
        return super().interrupt_worker(worker, run_id=run_id)


def _events(store, worker):
    return [event["event_type"] for event in store.list_events(worker["worker_id"])]


def _effects(store, worker):
    with sqlite3.connect(store.db_path) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT effect_kind FROM lifecycle_operation_effects WHERE worker_id = ?"
                " ORDER BY created_at",
                (worker["worker_id"],),
            )
        ]


def _expire_claim(store, worker):
    store.update_worker(worker["worker_id"], compute_release_expires_at="2000-01-01T00:00:00+00:00")


def _assert_pending(store, worker, run, lease):
    assert store.get_run(run["run_id"])["state"] == "running"
    held = store.get_host_run_lease(lease["lease_id"])
    assert held["status"] == "active" and held["startup_state"] == "reserved"
    fenced = store.get_worker(worker["worker_id"])
    assert fenced["compute_release_kind"] == "cancel_run"
    assert fenced["compute_release_target_run_id"] == run["run_id"]
    assert "run.cancelled" not in _events(store, worker)


def _assert_cancelled_once(store, worker, run, lease):
    assert store.get_run(run["run_id"])["state"] == "cancelled"
    assert store.get_host_run_lease(lease["lease_id"])["status"] == "released"
    events = _events(store, worker)
    assert events.count("run.cancelled") == 1, events
    assert not {"run.interrupted", "worker.interrupted", "control.terminal_won"} & set(events), events
    assert _effects(store, worker) == ["callback.run_cancelled"]
    settled = store.get_worker(worker["worker_id"])
    assert settled["state"] == "ready" and not settled.get("compute_release_token")


# Independent reviewer probe retained as a regression.
def test_stop_does_not_release_reserved_but_published_process_without_death_proof(tmp_path):
    store = Store(str(tmp_path / "reserved-published.db"))
    runtime = _ExactHostRuntime()
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "published")
    _publish(store, lease, service.executor_id, process_start_identity="start-4242")
    try:
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    _assert_pending(store, worker, run, lease)
    assert runtime.interrupted == []
    assert _events(store, worker).count("run.stopping") == 1


def test_published_startup_is_cancelled_only_after_exact_cleanup_proof(tmp_path):
    store = Store(str(tmp_path / "published-cleanup.db"))
    runtime = _CleanupRuntime([True])
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "published-cleanup")
    _publish(store, lease, service.executor_id)
    try:
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    # Cleanup targeted only the identity the lease durably published; no signal.
    assert runtime.cleanups == [(run["run_id"], {
        "identity_kind": "host_process", "pid": 4242, "process_group": 4242,
        "process_start_identity": PUBLISHED["process_start_identity"],
        "container_id": "", "session_id": "session-4242",
    })]
    assert runtime.interrupted == []
    _assert_cancelled_once(store, worker, run, lease)


def test_failed_cleanup_keeps_the_fence_until_a_repeated_stop_proves_it(tmp_path):
    store = Store(str(tmp_path / "failed-cleanup.db"))
    runtime = _CleanupRuntime([RuntimeError("cleanup could not reach the process"), False, True])
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "failed-cleanup")
    _publish(store, lease, service.executor_id)
    try:
        service.cancel_run(worker["worker_id"], run["run_id"])
        _assert_pending(store, worker, run, lease)
        # Delayed confirmation: a start that reports in after Stop cannot claim the run.
        assert _confirm(store, worker, run, lease, service.executor_id) is None
        assert store.get_host_run_lease(lease["lease_id"])["startup_state"] == "reserved"
        service.cancel_run(worker["worker_id"], run["run_id"])
        _assert_pending(store, worker, run, lease)
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    assert len(runtime.cleanups) == 3 and runtime.interrupted == []
    assert _events(store, worker).count("run.stopping") == 1
    _assert_cancelled_once(store, worker, run, lease)


def test_stop_that_waited_for_a_publishing_launch_needs_proof(tmp_path):
    """Delayed publication: Stop waits on the launch flock; the launch publishes
    its process identity and exits without confirming; Stop must not release it."""

    store = Store(str(tmp_path / "delayed-publication.db"))
    runtime = _CleanupRuntime()
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "delayed-publication")
    launch = service._acquire_worker_lifecycle_guard(worker["worker_id"])
    failures = []

    def stop():
        try:
            service.cancel_run(worker["worker_id"], run["run_id"])
        except BaseException as exc:  # surfaced below
            failures.append(exc)

    stopper = threading.Thread(target=stop)
    try:
        stopper.start()
        time.sleep(0.2)
        assert stopper.is_alive()
        _publish(store, lease, service.executor_id)
        launch.release()
        stopper.join(timeout=10)
    finally:
        service.shutdown()
    assert not stopper.is_alive() and failures == []
    _assert_pending(store, worker, run, lease)
    assert len(runtime.cleanups) == 1 and runtime.interrupted == []


def test_another_executors_unpublished_reservation_is_not_assumed_unstarted(tmp_path):
    store = Store(str(tmp_path / "foreign-reservation.db"))
    runtime = _CleanupRuntime()
    service = _host_service(store, tmp_path, runtime)
    worker, run = _running_host_run(store, "foreign-reservation")
    lease = store.acquire_host_run_lease(
        runtime_family="codex", lane="mission", tenant_id="tenant-a", owner_id="owner-a",
        worker_id=worker["worker_id"], run_id=run["run_id"], executor_id="executor-before-crash",
        conversation_limit=2, mission_limit=3, account_mission_limit=4,
        tenant_mission_limit=12, lease_ttl_s=30,
    )
    run = _admit_and_invoke(store, run, lease)
    store.update_worker_state(worker["worker_id"], "running")
    try:
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    _assert_pending(store, worker, run, lease)
    assert runtime.interrupted == []


def test_expired_pending_cancellation_recovers_as_cancellation_once_proven(tmp_path):
    store = Store(str(tmp_path / "pending-recovery.db"))
    runtime = _CleanupRuntime([False, True])
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "pending-recovery")
    _publish(store, lease, service.executor_id)
    try:
        service.cancel_run(worker["worker_id"], run["run_id"])
        _assert_pending(store, worker, run, lease)
        _expire_claim(store, worker)
        recovered = service.recover_expired_compute_release_claims_once()
    finally:
        service.shutdown()
    assert [(item["kind"], item["target_transitioned"]) for item in recovered] == [("cancel_run", True)]
    _assert_cancelled_once(store, worker, run, lease)


# Reviewer probe P2a, extended to the durable callback effect.
def test_failed_stop_recovery_keeps_cancel_semantics(tmp_path):
    store = Store(str(tmp_path / "stop-recovery.db"))
    runtime = _InterruptFailsOnce()
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "recovery", confirmed=True)
    try:
        with pytest.raises(RuntimeError, match="synthetic temporary"):
            service.cancel_run(worker["worker_id"], run["run_id"])
        assert store.get_run(run["run_id"])["state"] == "running"
        assert store.get_active_host_run_lease_for_run(run["run_id"]) is not None
        assert store.get_worker(worker["worker_id"])["compute_release_kind"] == "cancel_run"
        _expire_claim(store, worker)
        recovered = service.recover_expired_compute_release_claims_once()
    finally:
        service.shutdown()
    assert [item["kind"] for item in recovered] == ["cancel_run"]
    assert [signal[0] for signal in runtime.interrupted] == [run["run_id"]]
    _assert_cancelled_once(store, worker, run, lease)


def test_a_repeated_stop_retries_a_failed_exact_interrupt_at_once(tmp_path):
    store = Store(str(tmp_path / "stop-retry.db"))
    runtime = _InterruptFailsOnce()
    service = _host_service(store, tmp_path, runtime)
    worker, run, lease = _invoked_host_run(store, service, "retry", confirmed=True)
    try:
        with pytest.raises(RuntimeError, match="synthetic temporary"):
            service.cancel_run(worker["worker_id"], run["run_id"])
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    _assert_cancelled_once(store, worker, run, lease)


# Reviewer probe P2b, extended to prove the effect was durable before settlement.
def test_stop_settlement_crash_recovers_one_cancel_event(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "stop-settlement.db"))
    service = _host_service(store, tmp_path, _ExactHostRuntime())
    worker, run, lease = _invoked_host_run(store, service, "settlement", confirmed=True)
    original = store.finalize_worker_run_control_claim

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash after terminal CAS")

    monkeypatch.setattr(store, "finalize_worker_run_control_claim", crash)
    try:
        with pytest.raises(RuntimeError, match="synthetic crash"):
            service.cancel_run(worker["worker_id"], run["run_id"])
        # Terminal truth, its one event and its callback effect committed together.
        assert store.get_run(run["run_id"])["state"] == "cancelled"
        assert _events(store, worker).count("run.cancelled") == 1
        assert _effects(store, worker) == ["callback.run_cancelled"]
        monkeypatch.setattr(store, "finalize_worker_run_control_claim", original)
        _expire_claim(store, worker)
        recovered = service.recover_expired_compute_release_claims_once()
        service.cancel_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()
    assert [(item["kind"], item["terminal_won"]) for item in recovered] == [("cancel_run", True)]
    _assert_cancelled_once(store, worker, run, lease)
