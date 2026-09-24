from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

import workers_projects_runtime.service as service_module
from workers_projects_runtime.openclaw_runtime import HostCapacityError, StubRuntime
from workers_projects_runtime.service import HostResourceUsage, WorkersProjectsService
from workers_projects_runtime.store import Store


def _delegation_kwargs(suffix: str) -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "idempotency_key": f"capacity-{suffix}",
        "request_digest": f"digest-{suffix}",
        "origin_ref": f"telegram:{suffix}",
        "title": f"Capacity {suffix}",
        "goal": "Create one synthetic artifact.",
        "instruction": "Create one synthetic artifact.",
        "origin_surface": "telegram",
        "worker_name": f"Worker {suffix}",
        "worker_role": "General intelligent worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }


def _row_counts(store: Store) -> dict[str, int]:
    with store._connect() as conn:  # Exact persistence is part of this regression contract.
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("projects", "workers", "runs", "delegations", "host_run_leases")
        }


@pytest.fixture(autouse=True)
def _capacity_policy(monkeypatch):
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "2048")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "3072")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "1024")
    monkeypatch.setenv("WPR_DOCKER_DISK_RESERVATION_MB", "1024")
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )


def test_delegation_is_queued_when_4_3_gib_cannot_cover_5_gib(tmp_path):
    available_memory = int(4.3 * 1024**3)

    class MeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": available_memory,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 0,
                "running_worker_ids": [],
                "worker_process_counts": {},
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    store = Store(str(tmp_path / "blocked.sqlite3"))
    service = WorkersProjectsService(store, MeasuredRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        accepted = service.reserve_delegation(**_delegation_kwargs("blocked"))
        replay = service.reserve_delegation(**_delegation_kwargs("blocked"))
    finally:
        service.shutdown()

    assert replay["initial_run_id"] == accepted["initial_run_id"]
    assert replay["idempotent_replay"] is True
    assert store.get_run(accepted["initial_run_id"])["state"] == "queued"
    assert _row_counts(store) == {
        "projects": 1,
        "workers": 1,
        "runs": 1,
        "delegations": 1,
        "host_run_leases": 0,
    }


def test_no_cli_preflight_runs_without_durable_provisional_reservation(
    tmp_path, monkeypatch
):
    class CliPreflightRuntime(StubRuntime):
        preflight_uses_cli_subprocess = True

        def __init__(self):
            super().__init__()
            self.preflight_calls = 0

        def preflight_worker_profile(self, *_args, **_kwargs):
            self.preflight_calls += 1

    runtime = CliPreflightRuntime()
    store = Store(str(tmp_path / "no-preflight-reservation.sqlite3"))
    monkeypatch.setattr(
        store,
        "acquire_preflight_capacity_reservation",
        lambda **_kwargs: None,
        raising=False,
    )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        with pytest.raises(HostCapacityError):
            service.reserve_delegation(**_delegation_kwargs("no-preflight-reservation"))
    finally:
        service.shutdown()

    assert runtime.preflight_calls == 0
    assert _row_counts(store) == {
        "projects": 0,
        "workers": 0,
        "runs": 0,
        "delegations": 0,
        "host_run_leases": 0,
    }


def test_cli_preflight_observes_live_reservation_and_failure_releases_it(tmp_path):
    store = Store(str(tmp_path / "fenced-preflight.sqlite3"))
    observed_active_counts: list[int] = []

    class FailingCliPreflightRuntime(StubRuntime):
        preflight_uses_cli_subprocess = True

        def preflight_worker_profile(self, *_args, **_kwargs):
            with store._connect() as conn:
                observed_active_counts.append(
                    int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM preflight_capacity_reservations
                            WHERE status = 'active' AND expires_at > ?
                            """,
                            (datetime.now(timezone.utc).isoformat(),),
                        ).fetchone()[0]
                    )
                )
            raise RuntimeError("synthetic preflight failure")

    service = WorkersProjectsService(
        store, FailingCliPreflightRuntime(), reconcile_on_startup=False
    )
    try:
        with pytest.raises(RuntimeError, match="synthetic preflight failure"):
            service.reserve_delegation(**_delegation_kwargs("fenced-failure"))
    finally:
        service.shutdown()

    assert observed_active_counts == [1]
    with store._connect() as conn:
        reservations = conn.execute(
            "SELECT status, release_reason FROM preflight_capacity_reservations"
        ).fetchall()
    assert [tuple(row) for row in reservations] == [
        ("released", "preflight_failed")
    ]
    assert _row_counts(store) == {
        "projects": 0,
        "workers": 0,
        "runs": 0,
        "delegations": 0,
        "host_run_leases": 0,
    }
    assert store.list_provider_route_health(
        tenant_id="tenant-a", owner_id="owner-a"
    ) == []


def test_expired_preflight_reservation_blocks_cli_and_acceptance(tmp_path, monkeypatch):
    class CountingRuntime(StubRuntime):
        preflight_uses_cli_subprocess = True

        def __init__(self):
            super().__init__()
            self.preflight_calls = 0

        def preflight_worker_profile(self, *_args, **_kwargs):
            self.preflight_calls += 1

    runtime = CountingRuntime()
    store = Store(str(tmp_path / "expired-preflight.sqlite3"))
    original_acquire = store.acquire_preflight_capacity_reservation

    def acquire_expired(**kwargs):
        reservation = original_acquire(**kwargs)
        with store._connect() as conn:
            conn.execute(
                """
                UPDATE preflight_capacity_reservations
                SET expires_at = ? WHERE reservation_id = ?
                """,
                (
                    (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    reservation["reservation_id"],
                ),
            )
        return reservation

    monkeypatch.setattr(store, "acquire_preflight_capacity_reservation", acquire_expired)
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        with pytest.raises(HostCapacityError, match="expired before adapter"):
            service.reserve_delegation(**_delegation_kwargs("expired-preflight"))
    finally:
        service.shutdown()

    assert runtime.preflight_calls == 0
    assert _row_counts(store) == {
        "projects": 0,
        "workers": 0,
        "runs": 0,
        "delegations": 0,
        "host_run_leases": 0,
    }


def test_two_processes_accept_both_objectives_but_only_one_capacity_reservation(tmp_path):
    database = str(tmp_path / "concurrent.sqlite3")
    probe_barrier = Barrier(2)
    available_memory = int(7.5 * 1024**3)

    class RacedRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            probe_barrier.wait(timeout=3)
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": available_memory,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 0,
                "running_worker_ids": [],
                "worker_process_counts": {},
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    services = [
        WorkersProjectsService(Store(database), RacedRuntime(), reconcile_on_startup=False)
        for _ in range(2)
    ]
    for service in services:
        service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]

    def submit(index: int):
        try:
            return services[index].reserve_delegation(
                **_delegation_kwargs(f"race-{index}")
            )
        except HostCapacityError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
    finally:
        for service in services:
            service.shutdown()

    accepted = [result for result in results if isinstance(result, dict)]
    blocked = [result for result in results if isinstance(result, HostCapacityError)]
    assert len(accepted) == 2
    assert blocked == []
    assert _row_counts(Store(database)) == {
        "projects": 2,
        "workers": 2,
        "runs": 2,
        "delegations": 2,
        "host_run_leases": 1,
    }


def test_exact_committed_replay_stays_accepted_after_capacity_changes(tmp_path):
    class MutableRuntime(StubRuntime):
        available_memory_bytes = 16 * 1024**3

        def isolated_resource_usage(self, *, cached_only=False):
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": self.available_memory_bytes,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 0,
                "running_worker_ids": [],
                "worker_process_counts": {},
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    runtime = MutableRuntime()
    store = Store(str(tmp_path / "replay.sqlite3"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        accepted = service.reserve_delegation(**_delegation_kwargs("replay"))
        runtime.available_memory_bytes = 1024**3
        replay = service.reserve_delegation(**_delegation_kwargs("replay"))
    finally:
        service.shutdown()

    assert replay["idempotent_replay"] is True
    assert replay["work_ref"] == accepted["work_ref"]
    assert replay["initial_run_id"] == accepted["initial_run_id"]
    assert _row_counts(store) == {
        "projects": 1,
        "workers": 1,
        "runs": 1,
        "delegations": 1,
        "host_run_leases": 1,
    }


def test_preaccept_lease_binds_to_first_attempt_before_admission(tmp_path):
    store = Store(str(tmp_path / "handoff.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        accepted = service.reserve_delegation(**_delegation_kwargs("handoff"))
        run_id = str(accepted["initial_run_id"])
        worker_id = str(accepted["worker_id"])
        reserved = store.get_active_host_run_lease_for_run(run_id)
        assert reserved is not None
        assert reserved["attempt_id"] == ""

        claimed = store.claim_next_queued_run(
            worker_id,
            executor_id=service._executor_id,
            lease_ttl_s=service._host_lease_ttl_s(),
        )
        assert claimed is not None
        bound = store.get_active_host_run_lease_for_run(run_id)
        assert bound is not None
        assert bound["attempt_id"] == claimed["active_attempt_id"]

        replayed_lease = service._acquire_host_run_lease(
            store.get_worker(worker_id), claimed
        )
        assert replayed_lease is not None
        assert replayed_lease["lease_id"] == reserved["lease_id"]
        admitted = store.admit_claimed_run(
            run_id,
            lease_id=str(bound["lease_id"]),
            executor_id=service._executor_id,
        )
        assert admitted is not None
        assert admitted["state"] == "admitted"
        assert admitted["runtime_invoked_at"] is None

        running = store.mark_run_runtime_invoked(
            run_id,
            lease_id=str(bound["lease_id"]),
            executor_id=service._executor_id,
        )
        assert running is not None
        assert running["state"] == "running"
        assert running["runtime_invoked_at"]
    finally:
        service.shutdown()


def test_expired_preaccept_lease_can_transfer_to_restart_executor(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        WorkersProjectsService,
        "start_assigned_run",
        lambda self, _worker_id: None,
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_ensure_worker_processor",
        lambda self, _worker_id: None,
    )
    database = str(tmp_path / "restart.sqlite3")
    first_store = Store(database)
    first = WorkersProjectsService(
        first_store, StubRuntime(), reconcile_on_startup=False
    )
    accepted = first.reserve_delegation(**_delegation_kwargs("restart"))
    run_id = str(accepted["initial_run_id"])
    worker_id = str(accepted["worker_id"])
    first.shutdown()

    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with first_store._connect() as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ?, expires_at = ? WHERE run_id = ?",
            (expired_at, expired_at, run_id),
        )
    expired_lease = first_store.get_active_host_run_lease_for_run(run_id)
    assert expired_lease is not None
    restart_executor = f"{expired_lease['executor_id']}:restart"

    restarted_store = Store(database)
    restarted = WorkersProjectsService(
        restarted_store, StubRuntime(), reconcile_on_startup=False
    )
    restarted._executor_id = restart_executor
    try:
        worker = restarted_store.get_worker(worker_id)
        queued = restarted_store.get_run(run_id)
        assert worker is not None and queued is not None
        recovered = restarted._acquire_host_run_lease(worker, queued)
        assert recovered is not None
        assert recovered["recovery_takeover"] is True

        claimed = restarted_store.claim_next_queued_run(
            worker_id,
            executor_id=restart_executor,
            lease_ttl_s=30,
        )
        assert claimed is not None
        transferred = restarted_store.get_active_host_run_lease_for_run(run_id)
        assert transferred is not None
        assert transferred["executor_id"] == restart_executor
        assert transferred["attempt_id"] == claimed["active_attempt_id"]
        assert datetime.fromisoformat(transferred["expires_at"]) > datetime.now(
            timezone.utc
        )
        replayed = restarted_store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id="tenant-a",
            owner_id="owner-a",
            worker_id=worker_id,
            run_id=run_id,
            executor_id=restart_executor,
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            lease_ttl_s=30,
        )
        assert replayed["idempotent_replay"] is True
        assert replayed["lease_id"] == transferred["lease_id"]
    finally:
        restarted.shutdown()


def test_restart_reconciliation_releases_only_expired_never_launched_reservation(
    tmp_path, monkeypatch
):
    database = str(tmp_path / "restart-reconcile.sqlite3")
    first_store = Store(database)
    first = WorkersProjectsService(
        first_store, StubRuntime(), reconcile_on_startup=False
    )
    first.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    accepted = first.reserve_delegation(**_delegation_kwargs("restart-reconcile"))
    run_id = str(accepted["initial_run_id"])
    first.shutdown()

    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with first_store._connect() as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ?, expires_at = ? WHERE run_id = ?",
            (expired_at, expired_at, run_id),
        )

    monkeypatch.setattr(
        WorkersProjectsService,
        "_ensure_worker_processor",
        lambda self, _worker_id: None,
    )
    restarted_store = Store(database)
    restarted = WorkersProjectsService(
        restarted_store, StubRuntime(), reconcile_on_startup=True
    )
    try:
        with restarted_store._connect() as conn:
            durable_lease = conn.execute(
                "SELECT * FROM host_run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert durable_lease is not None
        assert durable_lease["status"] == "released"
        assert durable_lease["release_reason"] == "expired_preaccept_no_runtime"
        run = restarted_store.get_run(run_id)
        assert run is not None
        assert run["state"] == "queued"
        assert run["runtime_invoked_at"] is None
    finally:
        restarted.shutdown()


def test_stale_reconciler_cannot_release_reservation_after_first_attempt_claim(
    tmp_path,
):
    store = Store(str(tmp_path / "reconcile-claim-race.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        accepted = service.reserve_delegation(
            **_delegation_kwargs("reconcile-claim-race")
        )
        run_id = str(accepted["initial_run_id"])
        lease = store.get_active_host_run_lease_for_run(run_id)
        assert lease is not None
        claimed = store.claim_next_queued_run(
            str(accepted["worker_id"]),
            executor_id=service._executor_id,
            lease_ttl_s=service._host_lease_ttl_s(),
        )
        assert claimed is not None

        released = store.release_expired_preaccept_host_run_lease(
            lease_id=str(lease["lease_id"]),
            run_id=run_id,
            reason="expired_preaccept_no_runtime",
        )

        assert released is None
        durable = store.get_active_host_run_lease_for_run(run_id)
        assert durable is not None
        assert durable["attempt_id"] == claimed["active_attempt_id"]
    finally:
        service.shutdown()


def test_preflight_reservation_renews_and_ownership_loss_kills_exact_probe(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "renewed-preflight.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(service, "_host_lease_ttl_s", lambda: 0.3)
    release_calls: list[tuple[str, str]] = []
    original_release = store.release_preflight_capacity_reservation

    def record_release(reservation_id: str, *, reason: str) -> bool:
        release_calls.append((reservation_id, reason))
        return original_release(reservation_id, reason=reason)

    monkeypatch.setattr(store, "release_preflight_capacity_reservation", record_release)
    probe_started = Event()
    finish_probe = Event()

    def blocked_probe(_probe_lease):
        probe_started.set()
        assert finish_probe.wait(timeout=5)
        return {"status": "ready"}

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                service.run_reserved_host_subprocess_probe,
                "codex-cli",
                blocked_probe,
                tenant_id="tenant-a",
                owner_id="owner-a",
            )
            assert probe_started.wait(timeout=2)
            with store._connect() as conn:
                initial = conn.execute(
                    "SELECT * FROM preflight_capacity_reservations WHERE status = 'active'"
                ).fetchone()
            assert initial is not None
            initial_expiry = datetime.fromisoformat(str(initial["expires_at"]))
            while datetime.now(timezone.utc) <= initial_expiry + timedelta(seconds=0.15):
                Event().wait(0.03)
            assert store.preflight_capacity_reservation_is_live(
                str(initial["reservation_id"]),
                executor_id=service.executor_id,
                profile="codex-cli",
                execution_mode="host",
            )
            finish_probe.set()
            assert result.result(timeout=2) == {"status": "ready"}

        pid_path = tmp_path / "probe.pid"
        command = [
            sys.executable,
            "-c",
            (
                "import os, pathlib, signal; "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                "signal.pause()"
            ),
        ]

        def child_probe(probe_lease):
            return probe_lease.run_subprocess(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            lost = pool.submit(
                service.run_reserved_host_subprocess_probe,
                "codex-cli",
                child_probe,
                tenant_id="tenant-a",
                owner_id="owner-a",
            )
            deadline = datetime.now(timezone.utc) + timedelta(seconds=3)
            while not pid_path.exists() and datetime.now(timezone.utc) < deadline:
                Event().wait(0.03)
            assert pid_path.exists()
            child_pid = int(Path(pid_path).read_text())
            with store._connect() as conn:
                active = conn.execute(
                    """
                    SELECT reservation_id FROM preflight_capacity_reservations
                    WHERE status = 'active' ORDER BY acquired_at DESC LIMIT 1
                    """
                ).fetchone()
                assert active is not None
                lost_reservation_id = str(active["reservation_id"])
                conn.execute(
                    """
                    UPDATE preflight_capacity_reservations
                    SET executor_id = 'replacement-executor'
                    WHERE reservation_id = ? AND status = 'active'
                    """,
                    (lost_reservation_id,),
                )
            with pytest.raises(HostCapacityError, match="reservation ownership"):
                lost.result(timeout=3)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)

        durable = store.get_preflight_capacity_reservation(lost_reservation_id)
        assert durable is not None
        assert durable["status"] == "released"
        assert durable["release_reason"] == "preflight_lease_lost"
        assert [item for item in release_calls if item[0] == lost_reservation_id] == [
            (lost_reservation_id, "preflight_lease_lost")
        ]
    finally:
        finish_probe.set()
        service.shutdown()


def test_capacity_queued_objective_runs_after_restart_without_resubmission(tmp_path):
    class RecoveringRuntime(StubRuntime):
        preflight_uses_cli_subprocess = True
        memory = 1024**3
        preflight_calls = 0

        def preflight_worker_profile(self, *_args, **_kwargs):
            self.preflight_calls += 1

        def isolated_resource_usage(self, *, cached_only=False):
            return {**super().isolated_resource_usage(), "available_memory_bytes": self.memory}

    runtime = RecoveringRuntime()
    store = Store(str(tmp_path / "queued-restart.sqlite3"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None
    try:
        accepted = service.reserve_delegation(**_delegation_kwargs("restart-queued"))
        assert runtime.preflight_calls == 0
        assert store.get_active_host_run_lease_for_run(accepted["initial_run_id"]) is None
    finally:
        service.shutdown()

    runtime.memory = 16 * 1024**3
    restarted = WorkersProjectsService(store, runtime, reconcile_on_startup=True)
    try:
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = store.get_run(accepted["initial_run_id"])
            if run["state"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert run["state"] == "completed", run
        assert run["output_text"] == "STUB_OK: Create one synthetic artifact."
        assert _row_counts(store)["runs"] == 1
        assert _row_counts(store)["delegations"] == 1
    finally:
        restarted.shutdown()
