from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

import pytest

import workers_projects_runtime.service as service_module
import workers_projects_runtime.store as store_module
from workers_projects_runtime.failure_classification import classify_runtime_error
from workers_projects_runtime.openclaw_runtime import (
    HostCapacityError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeInfo,
    RunStartupRejectedError,
    StubRuntime,
)
from workers_projects_runtime.profile_runtime import (
    HostCodexCliRuntime,
    ProfiledWorkerRuntime,
)
from workers_projects_runtime.service import HostResourceUsage, WorkersProjectsService
from workers_projects_runtime.service import ParallelExecutionIsolationError
from workers_projects_runtime.service import valid_worker_prompt_layer_capability
from workers_projects_runtime.store import (
    HostRunLeaseCapacityError,
    RunRestorationState,
    Store,
)


REAL_HOST_RESOURCE_USAGE = service_module.host_resource_usage


@pytest.fixture(autouse=True)
def _synthetic_healthy_storage_probe(monkeypatch):
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: service_module.shutil._ntuple_diskusage(100, 50, 50),
    )


def _healthy_capability_producers() -> dict[str, object]:
    return {
        "readinessScope": {
            "contractVersion": 1,
            "scope": "deployment",
            "ownerCredentialRole": "transport_auth",
        },
        "storagePressure": {
            "version": 1,
            "state": "healthy",
            "healthy": True,
            "usedPercent": 50.0,
            "availableBytes": 50,
            "thresholdPercent": 90.0,
        },
        "promptLayers": {
            "contractVersion": 1,
            "producerScope": "glasshive.worker_prompt_registry",
            "unknownLayerNames": [],
        },
        "workTraceContract": WorkersProjectsService.work_trace_contract_capability(),
    }


def test_worker_prompt_layer_capability_fails_closed_when_omitted():
    assert valid_worker_prompt_layer_capability(None) is False
    assert valid_worker_prompt_layer_capability({}) is False
    assert (
        valid_worker_prompt_layer_capability(
            {"contractVersion": 1, "unknownLayerNames": []}
        )
        is False
    )
    assert (
        valid_worker_prompt_layer_capability(
            {
                "version": 1,
                "available": False,
                "reason": "arbitrary_unavailable",
            }
        )
        is False
    )


def test_reconcile_does_not_race_a_locally_owned_active_run(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    _project, worker, _run = _active_worker_and_run(
        store,
        "local-processor",
        execution_mode="host",
        run_state="running",
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(service, "_local_processor_owns", lambda _worker_id: True)

    def forbidden_collect(*_args, **_kwargs):
        raise AssertionError("local processor completion must not be recovered concurrently")

    monkeypatch.setattr(service, "_collect_completed_run", forbidden_collect)

    service._reconcile_worker_row(worker)


def test_heal_worker_does_not_race_a_locally_owned_active_run(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    _project, worker, run = _active_worker_and_run(
        store,
        "local-heal",
        execution_mode="docker",
        run_state="running",
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(service, "_local_processor_owns", lambda _worker_id: True)

    def forbidden_collect(*_args, **_kwargs):
        raise AssertionError("a live queue processor owns its native evidence")

    monkeypatch.setattr(service, "_collect_completed_run", forbidden_collect)
    result = service.heal_worker(worker["worker_id"])

    assert result["worker_id"] == worker["worker_id"]
    assert store.get_run(run["run_id"])["state"] == "running"


def _active_worker_and_run(
    store: Store,
    suffix: str,
    *,
    execution_mode: str = "docker",
    run_state: str = "claimed",
    tenant_id: str = "local",
    owner_id: str = "owner-a",
):
    project = store.create_project(
        owner_id,
        f"Project {suffix}",
        f"Goal {suffix}",
        "codex-cli",
        tenant_id=tenant_id,
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id=owner_id,
        name=f"Worker {suffix}",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
        tenant_id=tenant_id,
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], f"Instruction {suffix}"
    )
    if run_state in {"claimed", "running"}:
        run = store.claim_next_queued_run(worker["worker_id"])
        assert run is not None
    if run_state == "running":
        run = _invoke_run_attempt(store, worker, run, suffix=suffix)
    elif run_state != "claimed":
        transitioned = store.transition_run_if_state(
            run["run_id"], "queued", run_state
        )
        assert transitioned is not None
        run = transitioned
    store.update_worker_state(worker["worker_id"], "running")
    return project, store.get_worker(worker["worker_id"]), run


def _exact_terminal_generation(store: Store, run_id: str) -> dict[str, str]:
    run = store.get_run(run_id)
    assert run is not None
    return {
        **(store.get_run_retry_generation(run_id) or {}),
        "expected_runtime_invoked_at": str(run.get("runtime_invoked_at") or ""),
    }


def test_closing_paused_worker_settles_exact_run_and_lease_after_compute_stop(tmp_path):
    store = Store(str(tmp_path / "paused-close.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "paused-close", run_state="running"
    )
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    assert lease is not None
    paused = store.transition_run_if_state(run["run_id"], "running", "paused")
    assert paused is not None
    store.update_worker_state(worker["worker_id"], "paused")
    released = []

    class ReleasingRuntime(StubRuntime):
        def release_idle_workspace_box(self, closed_worker):
            released.append(
                (
                    closed_worker["state"],
                    store.get_run(run["run_id"])["state"],
                    store.get_host_run_lease(lease["lease_id"])["status"],
                )
            )
            return True

    service = WorkersProjectsService(store, ReleasingRuntime(), reconcile_on_startup=False)
    try:
        closed = service.terminate_worker(worker["worker_id"])
    finally:
        service.shutdown()
    assert closed["state"] == "terminated"
    assert store.get_run(run["run_id"])["state"] == "cancelled"
    settled_lease = store.get_host_run_lease(lease["lease_id"])
    assert settled_lease["status"] == "released"
    assert settled_lease["release_reason"] in {
        "worker_compute_terminated", "run_terminal:cancelled"
    }
    assert released == [("terminated", "cancelled", "released")]


def test_unproven_worker_compute_keeps_paused_run_and_lease_fenced(tmp_path):
    store = Store(str(tmp_path / "paused-unproven.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "paused-unproven", run_state="running"
    )
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    assert lease is not None
    store.transition_run_if_state(run["run_id"], "running", "paused")
    store.update_worker_state(worker["worker_id"], "paused")

    class UnconfirmedRuntime(StubRuntime):
        def terminate_worker(self, _worker):
            raise RuntimeError("exact compute stop unconfirmed")

        def release_idle_workspace_box(self, _worker):
            raise AssertionError("unconfirmed compute cannot release the box")

    service = WorkersProjectsService(store, UnconfirmedRuntime(), reconcile_on_startup=False)
    try:
        with pytest.raises(RuntimeError, match="unconfirmed"):
            service.terminate_worker(worker["worker_id"])
    finally:
        service.shutdown()
    assert store.get_run(run["run_id"])["state"] == "paused"
    assert store.get_host_run_lease(lease["lease_id"])["status"] == "active"


def test_needs_input_closes_exact_attempt_without_terminalizing_resumable_run(tmp_path):
    store = Store(str(tmp_path / "needs-input-attempt.sqlite3"))
    _project, _worker, run = _active_worker_and_run(
        store,
        "needs-input-attempt",
        run_state="running",
    )
    attempt_id = str(run["active_attempt_id"])

    updated = store.mark_run_needs_input(
        str(run["run_id"]),
        error_text="Synthetic connected account is missing.",
        failure_class="provider_auth_missing",
        failure_user_message="Connect the model account, then resume this work.",
    )

    assert updated is not None
    assert updated["state"] == "needs_input"
    assert updated["ended_at"] is None
    assert updated["active_attempt_id"] == attempt_id
    attempt = store.get_run_attempt(attempt_id)
    assert attempt is not None
    assert attempt["state"] == "needs_input"
    assert attempt["ended_at"]
    assert attempt["terminal_reason"] == "needs_input"


def test_provider_liveness_retries_are_durable_deduplicated_and_reset_by_progress(
    tmp_path,
):
    store = Store(str(tmp_path / "provider-liveness.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-liveness",
        execution_mode="host",
        run_state="running",
    )
    attempt_id = str(run["active_attempt_id"])
    original_route = (
        run["provider_route_profile"],
        run["provider_route_runtime"],
        run["provider_route_model"],
    )
    observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    def observe(
        event_ref: str,
        *,
        kind: str,
        failure_class: str = "",
        offset_seconds: int,
        source_sequence: int,
    ) -> dict:
        result = store.observe_provider_liveness(
            run_id=str(run["run_id"]),
            expected_attempt_id=attempt_id,
            kind=kind,
            failure_class=failure_class,
            runtime="codex-cli",
            model="test",
            source_sequence=source_sequence,
            source_digest=hashlib.sha256(event_ref.encode()).hexdigest(),
            observed_at=(observed_at + timedelta(seconds=offset_seconds)).isoformat(),
            retry_limit=3,
        )
        assert result is not None
        return result

    first = observe(
        "liveness-retry-1",
        kind="internal_retry",
        failure_class="provider_internal_retry",
        offset_seconds=1,
        source_sequence=1,
    )
    duplicate = observe(
        "liveness-retry-1",
        kind="internal_retry",
        failure_class="provider_internal_retry",
        offset_seconds=2,
        source_sequence=1,
    )
    with pytest.raises(ValueError, match="source sequence conflicts"):
        store.observe_provider_liveness(
            run_id=str(run["run_id"]),
            expected_attempt_id=attempt_id,
            kind="meaningful_progress",
            failure_class="",
            runtime="codex-cli",
            model="test",
            source_sequence=1,
            source_digest=hashlib.sha256(b"changed-payload").hexdigest(),
            observed_at=(observed_at + timedelta(seconds=2)).isoformat(),
            retry_limit=3,
        )
    second = observe(
        "liveness-retry-2",
        kind="internal_retry",
        failure_class="provider_internal_retry",
        offset_seconds=3,
        source_sequence=2,
    )
    progress = observe(
        "liveness-progress-1",
        kind="meaningful_progress",
        offset_seconds=4,
        source_sequence=3,
    )
    after_progress_1 = observe(
        "liveness-retry-3",
        kind="internal_retry",
        failure_class="provider_internal_retry",
        offset_seconds=5,
        source_sequence=4,
    )
    after_progress_2 = observe(
        "liveness-retry-4",
        kind="internal_retry",
        failure_class="provider_internal_retry",
        offset_seconds=6,
        source_sequence=5,
    )
    threshold = observe(
        "liveness-retry-5",
        kind="internal_retry",
        failure_class="provider_internal_retry",
        offset_seconds=7,
        source_sequence=6,
    )

    assert first["internal_retry_count"] == 1
    assert duplicate["internal_retry_count"] == 1
    assert duplicate["_inserted"] is False
    assert second["internal_retry_count"] == 2
    assert progress["meaningful_progress_sequence"] == 1
    assert progress["internal_retry_count"] == 0
    assert after_progress_1["state"] == "running"
    assert after_progress_2["state"] == "running"
    assert threshold["state"] == "needs_input"
    assert threshold["_attention_transitioned"] is True
    assert threshold["failure_class"] == "provider_progress_stalled"
    assert threshold["ended_at"] is None
    assert (
        threshold["provider_route_profile"],
        threshold["provider_route_runtime"],
        threshold["provider_route_model"],
    ) == original_route
    assert store.get_active_host_run_lease_for_run(str(run["run_id"])) is None
    attempt = store.get_run_attempt(attempt_id)
    assert attempt is not None
    assert attempt["state"] == "needs_input"
    assert attempt["terminal_reason"] == "provider_progress_stalled"
    assert len(store.list_provider_liveness_events(str(run["run_id"]))) == 6


def test_provider_liveness_stop_claim_wins_before_attention_transition(tmp_path):
    store = Store(str(tmp_path / "provider-liveness-stop.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-liveness-stop",
        execution_mode="host",
        run_state="running",
    )
    attempt_id = str(run["active_attempt_id"])

    for sequence in (1, 2):
        assert store.observe_provider_liveness(
            run_id=str(run["run_id"]),
            expected_attempt_id=attempt_id,
            kind="internal_retry",
            failure_class="provider_internal_retry",
            runtime="codex-cli",
            model="test",
            source_sequence=sequence,
            source_digest=hashlib.sha256(f"retry-{sequence}".encode()).hexdigest(),
            retry_limit=3,
        ) is not None
    store.update_worker_state(str(worker["worker_id"]), "stopping")

    assert store.observe_provider_liveness(
        run_id=str(run["run_id"]),
        expected_attempt_id=attempt_id,
        kind="internal_retry",
        failure_class="provider_internal_retry",
        runtime="codex-cli",
        model="test",
        source_sequence=3,
        source_digest=hashlib.sha256(b"retry-3").hexdigest(),
        retry_limit=3,
    ) is None
    assert store.get_run(str(run["run_id"]))["state"] == "running"
    assert store.get_worker(str(worker["worker_id"]))["state"] == "stopping"
    assert store.get_active_host_run_lease_for_run(str(run["run_id"])) is not None


def test_provider_liveness_rejects_an_expired_attempt_lease(tmp_path):
    store = Store(str(tmp_path / "provider-liveness-expired-lease.sqlite3"))
    _project, _worker, run = _active_worker_and_run(
        store,
        "provider-liveness-expired-lease",
        execution_mode="host",
        run_state="running",
    )
    attempt_id = str(run["active_attempt_id"])
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE host_run_leases SET expires_at = ?
            WHERE run_id = ? AND attempt_id = ? AND status = 'active'
            """,
            ("2020-01-01T00:00:00+00:00", run["run_id"], attempt_id),
        )

    observed = store.observe_provider_liveness(
        run_id=str(run["run_id"]),
        expected_attempt_id=attempt_id,
        kind="internal_retry",
        failure_class="provider_internal_retry",
        runtime="codex-cli",
        model="test",
        source_sequence=1,
        source_digest=hashlib.sha256(b"expired-attempt-event").hexdigest(),
        retry_limit=3,
    )

    assert observed is None
    assert store.list_provider_liveness_events(str(run["run_id"])) == []
    assert (store.get_run(str(run["run_id"])) or {})["internal_retry_count"] == 0


def test_provider_liveness_identity_is_attempt_scoped_and_source_ordered(tmp_path):
    store = Store(str(tmp_path / "provider-liveness-attempt.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-liveness-attempt",
        execution_mode="host",
        run_state="running",
    )
    run_id = str(run["run_id"])
    first_attempt = str(run["active_attempt_id"])
    shared_digest = hashlib.sha256(b"same-native-line").hexdigest()
    first = store.observe_provider_liveness(
        run_id=run_id,
        expected_attempt_id=first_attempt,
        kind="internal_retry",
        failure_class="provider_internal_retry",
        runtime="codex-cli",
        model="test",
        source_sequence=2,
        source_digest=shared_digest,
        retry_limit=1,
    )
    assert first is not None and first["state"] == "needs_input"

    resumed = store.transition_run_if_state(
        run_id,
        "needs_input",
        "queued",
        ended_at=None,
        error_text="",
        retry_after=None,
    )
    assert resumed is not None
    resumed_at = datetime.now(timezone.utc)
    store.update_run(
        run_id,
        queue_wait_open=1,
        queue_wait_started_at=resumed_at.isoformat(),
        queue_wait_closed_at=None,
        queue_deadline_at=(resumed_at + timedelta(minutes=5)).isoformat(),
        queue_wait_generation=int(resumed["queue_wait_generation"] or 0) + 1,
    )
    store.update_worker_state(str(worker["worker_id"]), "starting", last_error="")
    claimed = store.claim_next_queued_run(str(worker["worker_id"]))
    assert claimed is not None
    second = _invoke_run_attempt(store, worker, claimed, suffix="second-attempt")
    second_attempt = str(second["active_attempt_id"])
    assert second_attempt != first_attempt

    replayed_line = store.observe_provider_liveness(
        run_id=run_id,
        expected_attempt_id=second_attempt,
        kind="internal_retry",
        failure_class="provider_internal_retry",
        runtime="codex-cli",
        model="test",
        source_sequence=2,
        source_digest=shared_digest,
        retry_limit=3,
    )
    assert replayed_line is not None
    assert replayed_line["_inserted"] is True
    assert replayed_line["internal_retry_count"] == 1
    late_old_line = store.observe_provider_liveness(
        run_id=run_id,
        expected_attempt_id=second_attempt,
        kind="meaningful_progress",
        failure_class="",
        runtime="codex-cli",
        model="test",
        source_sequence=1,
        source_digest=hashlib.sha256(b"late-old-line").hexdigest(),
        retry_limit=3,
    )
    assert late_old_line is not None
    assert late_old_line["_inserted"] is True
    assert late_old_line["_applied"] is False
    assert late_old_line["meaningful_progress_sequence"] == 0
    assert late_old_line["internal_retry_count"] == 1
    assert len(store.list_provider_liveness_events(run_id)) == 3


def test_service_rejects_late_provider_liveness_from_prior_attempt(tmp_path):
    store = Store(str(tmp_path / "provider-liveness-late-attempt.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-liveness-late-attempt",
        execution_mode="host",
        run_state="running",
    )
    run_id = str(run["run_id"])
    first_attempt = str(run["active_attempt_id"])
    transitioned = store.observe_provider_liveness(
        run_id=run_id,
        expected_attempt_id=first_attempt,
        kind="internal_retry",
        failure_class="provider_internal_retry",
        runtime="codex-cli",
        model="test",
        source_sequence=1,
        source_digest=hashlib.sha256(b"first-attempt-threshold").hexdigest(),
        retry_limit=1,
    )
    assert transitioned is not None and transitioned["state"] == "needs_input"

    resumed = store.transition_run_if_state(
        run_id,
        "needs_input",
        "queued",
        ended_at=None,
        error_text="",
        retry_after=None,
    )
    assert resumed is not None
    resumed_at = datetime.now(timezone.utc)
    store.update_run(
        run_id,
        queue_wait_open=1,
        queue_wait_started_at=resumed_at.isoformat(),
        queue_wait_closed_at=None,
        queue_deadline_at=(resumed_at + timedelta(minutes=5)).isoformat(),
        queue_wait_generation=int(resumed["queue_wait_generation"] or 0) + 1,
    )
    store.update_worker_state(str(worker["worker_id"]), "starting", last_error="")
    claimed = store.claim_next_queued_run(str(worker["worker_id"]))
    assert claimed is not None
    second = _invoke_run_attempt(store, worker, claimed, suffix="late-second-attempt")
    second_attempt = str(second["active_attempt_id"])
    assert second_attempt != first_attempt

    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    try:
        late = service._observe_provider_liveness(
            {
                "worker_id": worker["worker_id"],
                "run_id": run_id,
                "attempt_id": first_attempt,
                "kind": "internal_retry",
                "failure_class": "provider_internal_retry",
                "runtime": "codex-cli",
                "model": "test",
                "source_sequence": 2,
                "source_digest": hashlib.sha256(b"late-first-attempt-line").hexdigest(),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    finally:
        service.shutdown()

    assert late is None
    durable = store.get_run(run_id) or {}
    assert durable["active_attempt_id"] == second_attempt
    assert durable["internal_retry_count"] == 0
    assert len(store.list_provider_liveness_events(run_id)) == 1


def test_provider_liveness_inactivity_survives_store_restart_and_transitions_once(
    tmp_path,
):
    db_path = tmp_path / "provider-liveness-restart.sqlite3"
    store = Store(str(db_path))
    _project, _worker, run = _active_worker_and_run(
        store,
        "provider-liveness-restart",
        execution_mode="host",
        run_state="running",
    )
    assert run["meaningful_progress_at"] is None
    anchor = datetime.fromisoformat(str(run["liveness_started_at"]))

    assert store.transition_stalled_provider_liveness_runs(
        now=(anchor + timedelta(seconds=899)).isoformat(),
        inactivity_seconds=900,
    ) == []

    reopened = Store(str(db_path))
    transitioned = reopened.transition_stalled_provider_liveness_runs(
        now=(anchor + timedelta(seconds=900)).isoformat(),
        inactivity_seconds=900,
    )
    assert len(transitioned) == 1
    assert transitioned[0]["run_id"] == run["run_id"]
    assert transitioned[0]["state"] == "needs_input"
    assert transitioned[0]["failure_class"] == "provider_progress_stalled"
    assert transitioned[0]["_attention_transitioned"] is True
    assert reopened.transition_stalled_provider_liveness_runs(
        now=(anchor + timedelta(seconds=901)).isoformat(),
        inactivity_seconds=900,
    ) == []


def test_declared_long_liveness_is_pinned_to_the_exact_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_startup_recovery",
        lambda self: None,
    )
    store = Store(str(tmp_path / "declared-long-pin.sqlite3"))
    project = store.create_project(
        "owner-a", "Declared long pin", "Preserve exact run policy", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Declared long worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
        bootstrap_profile="clean-room",
        bootstrap_bundle={
            "execution_policy": "parallel-clean-room-v1",
            "viventium_run_liveness": {"version": 1, "long_mission": True},
        },
    )

    declared = store.create_run(
        worker["worker_id"], project["project_id"], "Run beyond ordinary wall clock"
    )
    store.update_worker(
        worker["worker_id"],
        bootstrap_bundle_json=json.dumps(
            {"execution_policy": "parallel-clean-room-v1"}
        ),
    )
    ordinary = store.create_run(
        worker["worker_id"], project["project_id"], "Use ordinary wall clock"
    )

    assert (store.get_run(declared["run_id"]) or {})["liveness_mode"] == "declared_long"
    assert (store.get_run(ordinary["run_id"]) or {})["liveness_mode"] == "standard"
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    try:
        current_worker = store.get_worker(worker["worker_id"]) or {}
        declared_worker = service._run_local_worker(current_worker, declared)
        ordinary_worker = service._run_local_worker(current_worker, ordinary)
    finally:
        service.shutdown()
    assert json.loads(declared_worker["bootstrap_bundle_json"])[
        "viventium_run_liveness"
    ] == {"version": 1, "long_mission": True}
    assert "viventium_run_liveness" not in json.loads(
        ordinary_worker["bootstrap_bundle_json"]
    )


def test_declared_long_run_extends_only_with_fresh_typed_progress(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_MAX_RUN_DURATION_S", "1")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_NO_PROGRESS_TIMEOUT_S", "900")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_startup_recovery",
        lambda self: None,
    )
    store = Store(str(tmp_path / "declared-long-freshness.sqlite3"))
    project = store.create_project(
        "owner-a", "Declared long freshness", "Require real progress", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Declared long freshness worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
        bootstrap_profile="clean-room",
        bootstrap_bundle={
            "execution_policy": "parallel-clean-room-v1",
            "viventium_run_liveness": {"version": 1, "long_mission": True},
        },
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Continue while progress is fresh"
    )
    claimed = store.claim_next_queued_run(worker["worker_id"])
    assert claimed is not None
    running = _invoke_run_attempt(store, worker, claimed, suffix="declared-long-fresh")
    store.update_worker_state(worker["worker_id"], "running")
    now = datetime.now(timezone.utc)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE runs SET started_at = ? WHERE run_id = ?",
            ("2020-01-01T00:00:00+00:00", run["run_id"]),
        )
    progress = store.observe_provider_liveness(
        run_id=run["run_id"],
        expected_attempt_id=str(running["active_attempt_id"]),
        kind="meaningful_progress",
        failure_class="",
        runtime="codex-cli",
        model="test",
        source_sequence=1,
        source_digest=hashlib.sha256(b"declared-long-progress").hexdigest(),
        observed_at=now.isoformat(),
    )
    assert progress is not None and progress["meaningful_progress_sequence"] == 1
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    max_duration_releases: list[tuple[str, str]] = []
    real_release = service._release_worker_compute

    def capture_max_release(current_worker, **kwargs):
        max_duration_releases.append(
            (str(current_worker["worker_id"]), str(kwargs.get("kind") or ""))
        )
        return {
            "worker_id": str(current_worker["worker_id"]),
            "target_transitioned": False,
        }

    monkeypatch.setattr(service, "_release_worker_compute", capture_max_release)
    try:
        assert service.reap_expired_runs_once() == []
        assert max_duration_releases == []
        assert (store.get_run(run["run_id"]) or {})["state"] == "running"

        monkeypatch.setattr(service, "_release_worker_compute", real_release)
        stale_at = now - timedelta(seconds=901)
        store.update_run(run["run_id"], meaningful_progress_at=stale_at.isoformat())
        transitioned = service.process_provider_liveness_once()
        assert [item["run_id"] for item in transitioned] == [run["run_id"]]
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"]) or {}
    assert durable["state"] == "needs_input"
    assert durable["failure_class"] == "provider_progress_stalled"
    assert durable["ended_at"] is None


def test_resumed_declared_long_without_progress_uses_current_attempt_grace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_MAX_RUN_DURATION_S", "60")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_startup_recovery",
        lambda self: None,
    )
    store = Store(str(tmp_path / "declared-long-no-progress.sqlite3"))
    project = store.create_project(
        "owner-a", "Declared long no progress", "Do not grant empty extension", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Declared long no-progress worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
        bootstrap_profile="clean-room",
        bootstrap_bundle={
            "execution_policy": "parallel-clean-room-v1",
            "viventium_run_liveness": {"version": 1, "long_mission": True},
        },
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Do not extend without progress"
    )
    claimed = store.claim_next_queued_run(worker["worker_id"])
    assert claimed is not None
    first = _invoke_run_attempt(
        store, worker, claimed, suffix="declared-long-no-progress-first"
    )
    store.update_worker_state(worker["worker_id"], "running")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE runs SET started_at = ? WHERE run_id = ?",
            ("2020-01-01T00:00:00+00:00", run["run_id"]),
        )
    attention = store.observe_provider_liveness(
        run_id=run["run_id"],
        expected_attempt_id=str(first["active_attempt_id"]),
        kind="internal_retry",
        failure_class="provider_internal_retry",
        runtime="codex-cli",
        model="test",
        source_sequence=1,
        source_digest=hashlib.sha256(b"resume-grace-first-attempt").hexdigest(),
        retry_limit=1,
    )
    assert attention is not None and attention["state"] == "needs_input"
    resumed = store.transition_run_if_state(
        run["run_id"],
        "needs_input",
        "queued",
        ended_at=None,
        error_text="",
        retry_after=None,
    )
    assert resumed is not None
    resumed_at = datetime.now(timezone.utc)
    store.update_run(
        run["run_id"],
        queue_wait_open=1,
        queue_wait_started_at=resumed_at.isoformat(),
        queue_wait_closed_at=None,
        queue_deadline_at=(resumed_at + timedelta(minutes=5)).isoformat(),
        queue_wait_generation=int(resumed["queue_wait_generation"] or 0) + 1,
    )
    store.update_worker_state(worker["worker_id"], "starting", last_error="")
    resumed_claim = store.claim_next_queued_run(worker["worker_id"])
    assert resumed_claim is not None
    second = _invoke_run_attempt(
        store, worker, resumed_claim, suffix="declared-long-no-progress-second"
    )
    store.update_worker_state(worker["worker_id"], "running")
    assert second["active_attempt_id"] != first["active_attempt_id"]
    assert second["started_at"] == "2020-01-01T00:00:00+00:00"
    assert second["meaningful_progress_sequence"] == 0
    assert datetime.fromisoformat(str(second["liveness_started_at"])) >= resumed_at
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    max_duration_releases: list[tuple[str, str]] = []

    def capture_max_release(current_worker, **kwargs):
        max_duration_releases.append(
            (str(current_worker["worker_id"]), str(kwargs.get("kind") or ""))
        )
        return {
            "worker_id": str(current_worker["worker_id"]),
            "target_transitioned": False,
        }

    monkeypatch.setattr(service, "_release_worker_compute", capture_max_release)
    try:
        assert service.reap_expired_runs_once() == []
        assert max_duration_releases == []
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                """
                UPDATE runs
                SET runtime_invoked_at = ?, liveness_started_at = ?
                WHERE run_id = ?
                """,
                (
                    "2020-01-01T00:00:00+00:00",
                    "2020-01-01T00:00:00+00:00",
                    run["run_id"],
                ),
            )
        reaped = service.reap_expired_runs_once()
    finally:
        service.shutdown()

    assert [item["run_id"] for item in reaped] == [run["run_id"]]
    assert max_duration_releases == [(worker["worker_id"], "max_duration")]


def test_service_provider_liveness_attention_releases_compute_and_emits_once(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "provider-liveness-service.sqlite3"))
    project, worker, run = _active_worker_and_run(
        store,
        "provider-liveness-service",
        execution_mode="host",
        run_state="running",
    )
    store.update_worker(
        str(worker["worker_id"]),
        bootstrap_bundle_json=json.dumps(
            {"callbacks": {"url": "https://example.invalid/callback"}}
        ),
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    released: list[str] = []
    callbacks: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service,
        "_release_needs_input_compute",
        lambda _worker, current_run: released.append(str(current_run["run_id"])),
    )
    def capture_callback(_worker, event_type, *, run, **_kwargs):
        callbacks.append((event_type, str(run["run_id"])))
        return {"callback_id": "synthetic-callback", "_inserted": True}

    monkeypatch.setattr(service, "_emit_callback", capture_callback)
    try:
        for sequence in range(1, 4):
            updated = service._observe_provider_liveness(
                {
                    "worker_id": worker["worker_id"],
                    "run_id": run["run_id"],
                    "attempt_id": run["active_attempt_id"],
                    "kind": "internal_retry",
                    "failure_class": "provider_internal_retry",
                    "runtime": "codex-cli",
                    "model": "test",
                    "source_sequence": sequence,
                    "source_digest": hashlib.sha256(
                        f"service-liveness-{sequence}".encode()
                    ).hexdigest(),
                    "observed_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=sequence)
                    ).isoformat(),
                }
            )
            assert updated is not None
        duplicate = service._observe_provider_liveness(
            {
                "worker_id": worker["worker_id"],
                "run_id": run["run_id"],
                "attempt_id": run["active_attempt_id"],
                "kind": "internal_retry",
                "failure_class": "provider_internal_retry",
                "runtime": "codex-cli",
                "model": "test",
                "source_sequence": 3,
                "source_digest": hashlib.sha256(b"service-liveness-3").hexdigest(),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    finally:
        service.shutdown()

    assert duplicate is None
    durable = store.get_run(str(run["run_id"]))
    assert durable is not None
    assert durable["state"] == "needs_input"
    assert durable["failure_class"] == "provider_progress_stalled"
    assert store.get_worker(str(worker["worker_id"]))["state"] == "needs_input"
    assert released == [str(run["run_id"])]
    assert callbacks == [("run.needs_input", str(run["run_id"]))]
    attention_events = [
        event
        for event in store.list_events(str(worker["worker_id"]))
        if event["event_type"] == "run.needs_input"
    ]
    assert len(attention_events) == 1
    assert attention_events[0]["project_id"] == project["project_id"]


def test_provider_liveness_attention_recovers_compute_and_callback_after_restart(
    tmp_path, monkeypatch
):
    class StoppingStubRuntime(StubRuntime):
        def terminate_worker(self, worker):
            info = self.ensure_worker_ready(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "provider-liveness-crash.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-liveness-crash",
        execution_mode="host",
        run_state="running",
    )
    store.update_worker(
        str(worker["worker_id"]),
        bootstrap_bundle_json=json.dumps(
            {"callbacks": {"url": "https://example.invalid/callback"}}
        ),
    )
    attempt_id = str(run["active_attempt_id"])
    for sequence in range(1, 4):
        transitioned = store.observe_provider_liveness(
            run_id=str(run["run_id"]),
            expected_attempt_id=attempt_id,
            kind="internal_retry",
            failure_class="provider_internal_retry",
            runtime="codex-cli",
            model="test",
            source_sequence=sequence,
            source_digest=hashlib.sha256(f"crash-{sequence}".encode()).hexdigest(),
            retry_limit=3,
        )
    assert transitioned is not None
    assert transitioned["state"] == "needs_input"
    assert store.get_worker(str(worker["worker_id"]))["compute_released_at"] is None
    pending = store.list_lifecycle_operation_effects(
        worker_id=str(worker["worker_id"]), status="pending"
    )
    assert [item["effect_kind"] for item in pending] == ["callback.run_needs_input"]

    monkeypatch.setattr(
        WorkersProjectsService,
        "_deliver_callback_record",
        lambda *_args, **_kwargs: None,
    )
    restarted = WorkersProjectsService(
        store,
        StoppingStubRuntime(),
        reconcile_on_startup=False,
    )
    try:
        restarted._startup_recovery_thread.join(timeout=3)
        assert not restarted._startup_recovery_thread.is_alive()
        restarted.reap_needs_input_workers_once()
        restarted._replay_pending_lifecycle_effects()
    finally:
        restarted.shutdown()

    durable_worker = store.get_worker(str(worker["worker_id"]))
    assert durable_worker is not None
    assert durable_worker["state"] == "needs_input"
    assert durable_worker["compute_released_at"]
    assert store.get_active_host_run_lease_for_run(str(run["run_id"])) is None
    events = [
        event
        for event in store.list_events(str(worker["worker_id"]))
        if event["event_type"] == "run.needs_input"
    ]
    assert len(events) == 1
    effects = store.list_lifecycle_operation_effects(
        worker_id=str(worker["worker_id"])
    )
    assert len(effects) == 1
    assert effects[0]["status"] == "applied"
    callbacks = store.list_callback_outbox_for_run(
        str(run["run_id"]), tenant_id="local", owner_id="owner-a"
    )
    assert len(callbacks) == 1
    assert callbacks[0]["event_type"] == "run.needs_input"


def test_managed_shutdown_release_keeps_run_out_of_provider_attention_and_requeues_same_run(
    tmp_path,
):
    store = Store(str(tmp_path / "managed-shutdown.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "managed-shutdown",
        execution_mode="host",
        run_state="running",
    )
    _other_project, _other_worker, other_run = _active_worker_and_run(
        store,
        "managed-shutdown-other",
        execution_mode="host",
        run_state="running",
    )
    run_id = str(run["run_id"])
    attempt_id = str(run["active_attempt_id"])
    lease = store.get_active_host_run_lease_for_run(run_id)
    assert lease is not None
    anchor = datetime.fromisoformat(str(run["liveness_started_at"]))
    runtime = StubRuntime()
    runtime.host_process_absence = lambda worker, run_id: True
    service = WorkersProjectsService(
        store,
        runtime,
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    # Own the lease exactly as the executor that dispatched this run.
    service._executor_id = str(lease["executor_id"])
    try:
        assert service.release_owned_host_run_leases() == 1
        # Idempotent: nothing this executor owns is still active.
        assert service.release_owned_host_run_leases() == 0

        released = store.get_host_run_lease(str(lease["lease_id"]))
        assert released is not None
        assert released["status"] == "released"
        assert released["release_reason"] == "managed_shutdown"
        foreign = store.get_active_host_run_lease_for_run(str(other_run["run_id"]))
        assert foreign is not None and foreign["status"] == "active"

        # The same monitor pass still classifies the genuinely silent foreign
        # lease, but a managed stop is not a stalled provider.
        transitioned = store.transition_stalled_provider_liveness_runs(
            now=(anchor + timedelta(seconds=3600)).isoformat(),
            inactivity_seconds=900,
        )
        assert [item["run_id"] for item in transitioned] == [str(other_run["run_id"])]
        still_running = store.get_run(run_id)
        assert still_running is not None
        assert still_running["state"] == "running"
        assert not str(still_running["failure_class"] or "")
        assert store.list_provider_liveness_events(run_id) == []
        assert (
            store.list_lifecycle_operation_effects(
                worker_id=str(worker["worker_id"]), status="pending"
            )
            == []
        )

        # Startup reconciliation re-queues the same run with restart wording.
        assert store.reconcile_invalid_running_runs() == 1
    finally:
        service.shutdown()

    requeued = store.get_run(run_id)
    assert requeued is not None
    assert requeued["run_id"] == run_id
    assert requeued["state"] == "queued"
    assert requeued["active_attempt_id"] == ""
    assert requeued["runtime_invoked_at"] is None
    assert requeued["failure_retryable"] == 1
    assert (
        requeued["failure_user_message"]
        == store_module.MANAGED_RESTART_FAILURE_USER_MESSAGE
    )
    assert (
        requeued["failure_recommended_recovery"]
        == store_module.MANAGED_RESTART_FAILURE_RECOMMENDED_RECOVERY
    )
    attempt = store.get_run_attempt(attempt_id)
    assert attempt is not None
    assert attempt["state"] == "retry_queued"
    assert store.get_worker(str(worker["worker_id"]))["state"] == "starting"


def test_service_shutdown_releases_owned_host_run_leases_as_managed_shutdown(tmp_path):
    store = Store(str(tmp_path / "managed-shutdown-wiring.sqlite3"))
    _project, _worker, run = _active_worker_and_run(
        store,
        "managed-shutdown-wiring",
        execution_mode="host",
        run_state="running",
    )
    lease = store.get_active_host_run_lease_for_run(str(run["run_id"]))
    assert lease is not None
    class GenerationStoppingRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.stopped: list[tuple[str, str | None]] = []

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            # A live native process of the old generation.
            return info.__class__(**{**info.__dict__, "pid": 4242})

        def host_process_absence(self, worker, run_id):
            return (str(worker["worker_id"]), run_id) in self.stopped

        def interrupt_worker(self, worker, run_id=None):
            self.stopped.append((str(worker["worker_id"]), run_id))
            return super().pause_worker(worker)

    runtime = GenerationStoppingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._executor_id = str(lease["executor_id"])

    service.shutdown()

    released = store.get_host_run_lease(str(lease["lease_id"]))
    assert released is not None
    assert released["status"] == "released"
    assert released["release_reason"] == "managed_shutdown"
    # The released lease fences the old generation's late result; the old generation's native
    # process is stopped before this process exits so it cannot keep making external effects.
    assert runtime.stopped == [(str(_worker["worker_id"]), str(run["run_id"]))]
    # Shutdown only hands back ownership; the run itself is untouched until
    # startup reconciliation re-queues it.
    assert store.get_run(str(run["run_id"]))["state"] == "running"
    assert service.release_owned_host_run_leases() == 0


def test_lifespan_shutdown_releases_owned_leases_before_service_shutdown(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from workers_projects_runtime.api import create_app

    runtime = StubRuntime()
    runtime.host_process_absence = lambda worker, run_id: True
    app = create_app(
        str(tmp_path / "lifespan-managed-shutdown.sqlite3"),
        runtime=runtime,
        reconcile_on_startup=False,
    )
    store = app.state.store
    service = app.state.service
    lease_ids: list[str] = []
    seen_at_shutdown: list[dict] = []
    original_shutdown = service.shutdown

    def spying_shutdown(*args, **kwargs):
        seen_at_shutdown.append(store.get_host_run_lease(lease_ids[0]) or {})
        return original_shutdown(*args, **kwargs)

    monkeypatch.setattr(service, "shutdown", spying_shutdown)
    with TestClient(app):
        _project, _worker, run = _active_worker_and_run(
            store,
            "lifespan-managed-shutdown",
            execution_mode="host",
            run_state="running",
        )
        lease = store.get_active_host_run_lease_for_run(str(run["run_id"]))
        assert lease is not None
        lease_ids.append(str(lease["lease_id"]))
        service._executor_id = str(lease["executor_id"])

    # The lifespan released the lease before service.shutdown() even started,
    # so a preempted shutdown still leaves a typed managed_shutdown release.
    assert len(seen_at_shutdown) == 1
    assert seen_at_shutdown[0]["status"] == "released"
    assert seen_at_shutdown[0]["release_reason"] == "managed_shutdown"


def test_needs_input_callback_payload_carries_failure_guidance(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "needs-input-guidance.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "needs-input-guidance",
        execution_mode="host",
        run_state="running",
    )
    store.update_worker(
        str(worker["worker_id"]),
        bootstrap_bundle_json=json.dumps(
            {"callbacks": {"url": "https://example.invalid/callback"}}
        ),
    )
    attempt_id = str(run["active_attempt_id"])
    transitioned = None
    for sequence in range(1, 4):
        transitioned = store.observe_provider_liveness(
            run_id=str(run["run_id"]),
            expected_attempt_id=attempt_id,
            kind="internal_retry",
            failure_class="provider_internal_retry",
            runtime="codex-cli",
            model="test",
            source_sequence=sequence,
            source_digest=hashlib.sha256(f"guidance-{sequence}".encode()).hexdigest(),
            retry_limit=3,
        )
    assert transitioned is not None
    assert transitioned["state"] == "needs_input"
    fields = Store._provider_liveness_failure_fields()

    monkeypatch.setattr(
        WorkersProjectsService,
        "_deliver_callback_record",
        lambda *_args, **_kwargs: None,
    )
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    try:
        service._replay_pending_lifecycle_effects()
    finally:
        service.shutdown()

    callbacks = store.list_callback_outbox_for_run(
        str(run["run_id"]), tenant_id="local", owner_id="owner-a"
    )
    assert len(callbacks) == 1
    assert callbacks[0]["event_type"] == "run.needs_input"
    payload = json.loads(str(callbacks[0]["payload_json"]))
    assert payload["event"] == "run.needs_input"
    assert payload["failure_class"] == "provider_progress_stalled"
    assert payload["failure_retryable"] is False
    assert payload["failure_user_message"] == fields["failure_user_message"]
    assert (
        payload["failure_recommended_recovery"]
        == fields["failure_recommended_recovery"]
    )
    assert payload["message"].startswith(str(fields["failure_user_message"]))


def test_provider_attention_resume_keeps_exact_route_during_cooldown(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "0")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "0")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "1")
    monkeypatch.setenv("WPR_DOCKER_DISK_RESERVATION_MB", "1")
    store = Store(str(tmp_path / "provider-attention-route-lock.sqlite3"))
    project = store.create_project(
        "owner-a", "Provider route lock", "Keep the exact resumed route", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Provider route lock worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
        bootstrap_profile="clean-room",
        bootstrap_bundle={
            "execution_policy": "parallel-clean-room-v1",
            "viventium_launch_authority": {
                "version": 1,
                "kind": "conversation_orchestrator",
                "execution_mode": "docker",
                "fallback_worker_profile": "claude-code",
            },
        },
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Resume on the exact provider route"
    )
    claimed = store.claim_next_queued_run(worker["worker_id"])
    assert claimed is not None
    running = _invoke_run_attempt(store, worker, claimed, suffix="route-lock-first")
    running = store.update_run(
        run["run_id"],
        provider_route_profile="codex-cli",
        provider_route_runtime="codex-cli",
        provider_route_model="test",
        provider_route_decision="primary_selected",
    ) or running
    store.update_worker_state(worker["worker_id"], "running")
    attention = store.observe_provider_liveness(
        run_id=run["run_id"],
        expected_attempt_id=str(running["active_attempt_id"]),
        kind="internal_retry",
        failure_class="provider_internal_retry",
        runtime="codex-cli",
        model="test",
        source_sequence=1,
        source_digest=hashlib.sha256(b"route-lock-attention").hexdigest(),
        retry_limit=1,
    )
    assert attention is not None and attention["state"] == "needs_input"
    assert attention["provider_liveness_route_locked"] == 1

    resumed = store.transition_run_if_state(
        run["run_id"],
        "needs_input",
        "queued",
        ended_at=None,
        error_text="",
        retry_after=None,
    )
    assert resumed is not None
    resumed_at = datetime.now(timezone.utc)
    store.update_run(
        run["run_id"],
        queue_wait_open=1,
        queue_wait_started_at=resumed_at.isoformat(),
        queue_wait_closed_at=None,
        queue_deadline_at=(resumed_at + timedelta(minutes=5)).isoformat(),
        queue_wait_generation=int(resumed["queue_wait_generation"] or 0) + 1,
    )
    store.update_worker_state(worker["worker_id"], "starting", last_error="")
    resumed_claim = store.claim_next_queued_run(worker["worker_id"])
    assert resumed_claim is not None

    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    pinned_route = {
        "tenant_id": str(worker["tenant_id"]),
        "owner_id": str(worker["owner_id"]),
        "profile": str(attention["provider_route_profile"]),
        "runtime": str(attention["provider_route_runtime"]),
        "model": str(attention["provider_route_model"]),
    }
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    store.record_provider_route_failure(
        **pinned_route,
        failure_class="provider_quota_exhausted",
        failure_structured=True,
        retry_at=retry_at.isoformat(),
        default_cooldown_s=300,
        run_id="prior-route-health",
    )
    try:
        handled = service._handle_unhealthy_provider_route(
            store.get_worker(worker["worker_id"]) or worker,
            store.get_run(run["run_id"]) or resumed_claim,
        )
    finally:
        service.shutdown()

    assert handled is True
    durable = store.get_run(run["run_id"]) or {}
    durable_worker = store.get_worker(worker["worker_id"]) or {}
    assert durable["state"] == "queued"
    assert durable["provider_liveness_route_locked"] == 1
    assert durable["provider_route_profile"] == "codex-cli"
    assert durable["provider_route_runtime"] == "codex-cli"
    assert durable["provider_route_model"] == "test"
    assert durable["provider_route_decision"] == "waiting_primary_health"
    assert durable_worker["profile"] == "codex-cli"
    assert not any(
        event["event_type"] == "run.provider_route_switched"
        for event in store.list_events(worker["worker_id"])
    )


def test_locked_resumed_route_waits_after_new_quota_failure(tmp_path):
    store = Store(str(tmp_path / "provider-locked-runtime-failure.sqlite3"))
    project, worker, run = _active_worker_and_run(
        store,
        "provider-locked-runtime-failure",
        execution_mode="docker",
        run_state="running",
    )
    run = store.update_run(
        run["run_id"],
        provider_route_profile="codex-cli",
        provider_route_runtime="codex-cli",
        provider_route_model="test",
        provider_route_decision="primary_selected",
        provider_liveness_route_locked=1,
    ) or run
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    store.record_provider_route_failure(
        tenant_id=str(worker["tenant_id"]),
        owner_id=str(worker["owner_id"]),
        profile="codex-cli",
        runtime="codex-cli",
        model="test",
        failure_class="provider_quota_exhausted",
        failure_structured=True,
        retry_at=retry_at.isoformat(),
        default_cooldown_s=300,
        run_id=str(run["run_id"]),
    )
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    failure = RuntimeErrorBase("Synthetic exact-route quota failure")
    failure_fields = {
        "failure_class": "provider_quota_exhausted",
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "The exact provider route is cooling down.",
        "failure_recommended_recovery": "Wait for the exact provider reset.",
        "failure_diagnostic_summary": "Synthetic route-lock regression.",
    }
    try:
        requeued = service._switch_quota_exhausted_run_to_fallback(
            worker, run, failure, failure_fields
        )
    finally:
        service.shutdown()

    assert requeued is not None
    durable = store.get_run(str(run["run_id"])) or {}
    assert durable["state"] == "queued"
    assert durable["retry_after"] == retry_at.isoformat()
    assert durable["retry_attempts"] == 0
    assert durable["provider_liveness_route_locked"] == 1
    assert durable["provider_route_profile"] == "codex-cli"
    assert durable["provider_route_runtime"] == "codex-cli"
    assert durable["provider_route_model"] == "test"
    assert durable["provider_route_decision"] == "waiting_primary_health"
    assert (store.get_worker(str(worker["worker_id"])) or {})["profile"] == "codex-cli"
    assert not any(
        event["event_type"] in {
            "run.provider_fallback",
            "run.provider_route_switched",
        }
        for event in store.list_events(str(worker["worker_id"]))
    )


def _invoke_run_attempt(
    store: Store,
    worker: dict,
    run: dict,
    *,
    suffix: str,
) -> dict:
    current = store.get_run(run["run_id"])
    assert current is not None
    if current["state"] == "queued":
        refreshed = store.transition_run_if_state(
            current["run_id"], "queued", "queued", retry_after=None
        )
        assert refreshed is not None
        current = store.claim_next_queued_run(worker["worker_id"])
        assert current is not None
    assert current["state"] == "claimed"
    executor_id = f"test-invocation-{suffix}"
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(current["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    admitted = store.admit_claimed_run(
        current["run_id"],
        lease_id=lease["lease_id"],
        executor_id=executor_id,
    )
    assert admitted is not None
    invoked = store.mark_run_runtime_invoked(
        current["run_id"],
        lease_id=lease["lease_id"],
        executor_id=executor_id,
    )
    assert invoked is not None
    session_id = f"session-{suffix}"
    if str(worker.get("execution_mode") or "docker") == "docker":
        container_id = f"container-{suffix}"
        identity_kind = "docker_session"
        process_start_identity = (
            f"docker:{container_id}:{session_id}:{current['run_id']}:synthetic"
        )
    else:
        container_id = ""
        identity_kind = "host_process"
        process_start_identity = f"process-start-{suffix}"
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(current["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=executor_id,
        identity_kind=identity_kind,
        pid=4242,
        process_group=4242,
        process_start_identity=process_start_identity,
        container_id=container_id,
        session_id=session_id,
    )
    assert confirmed is not None
    return confirmed["run"]


def _lease(
    store: Store,
    suffix: str,
    *,
    lane: str = "mission",
    family: str = "codex",
    tenant_id: str = "tenant-a",
    owner_id: str = "owner-a",
    conversation_limit: int = 2,
    mission_limit: int = 3,
    account_limit: int = 4,
    tenant_limit: int = 12,
    now: datetime | None = None,
):
    cache = getattr(store, "_test_lease_subjects", {})
    subject = cache.get(suffix)
    if subject is None:
        _project, worker, run = _active_worker_and_run(
            store,
            f"lease-{suffix}",
            run_state="claimed",
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        subject = (worker["worker_id"], run["run_id"])
        cache[suffix] = subject
        store._test_lease_subjects = cache
    worker_id, run_id = subject
    return store.acquire_host_run_lease(
        runtime_family=family,
        lane=lane,
        tenant_id=tenant_id,
        owner_id=owner_id,
        worker_id=worker_id,
        run_id=run_id,
        executor_id=f"executor-{suffix}",
        conversation_limit=conversation_limit,
        mission_limit=mission_limit,
        account_mission_limit=account_limit,
        tenant_mission_limit=tenant_limit,
        lease_ttl_s=30,
        now=now,
    )


def test_persisted_host_leases_enforce_independent_lane_and_account_caps(tmp_path):
    store = Store(str(tmp_path / "leases.sqlite3"))

    for index in range(3):
        _lease(store, f"mission-{index}", owner_id=f"owner-{index}")
    with pytest.raises(HostRunLeaseCapacityError) as family_blocked:
        _lease(store, "mission-overflow", owner_id="owner-overflow")
    assert family_blocked.value.capacity_class == "family_lane"
    assert family_blocked.value.code == "host_capacity"
    assert family_blocked.value.dimension == "codexMissionSlots"
    assert family_blocked.value.configured == {"codexMissionSlots": 3}
    assert family_blocked.value.used == {"codexMissionSlots": 3}
    assert family_blocked.value.required == {"codexMissionSlots": 1}
    assert family_blocked.value.shortage == {"codexMissionSlots": 1}

    # Conversation admission is an independent reserved lane for the same CLI.
    _lease(store, "conversation-1", lane="conversation")
    _lease(store, "conversation-2", lane="conversation")
    with pytest.raises(HostRunLeaseCapacityError) as conversation_blocked:
        _lease(store, "conversation-3", lane="conversation")
    assert conversation_blocked.value.capacity_class == "family_lane"

    # A single account cannot evade its top-level mission cap by switching CLI.
    isolated = Store(str(tmp_path / "account-cap.sqlite3"))
    for index, family in enumerate(("codex", "claude", "openclaw", "codex")):
        _lease(
            isolated,
            f"account-{index}",
            family=family,
            mission_limit=10,
            owner_id="same-owner",
        )
    with pytest.raises(HostRunLeaseCapacityError) as account_blocked:
        _lease(
            isolated,
            "account-overflow",
            family="claude",
            mission_limit=10,
            owner_id="same-owner",
        )
    assert account_blocked.value.capacity_class == "account"
    assert account_blocked.value.dimension == "accountMissionSlots"
    assert account_blocked.value.configured == {"accountMissionSlots": 4}
    assert account_blocked.value.used == {"accountMissionSlots": 4}


def test_model_bootstrap_cannot_forge_the_reserved_conversation_lane(tmp_path):
    store = Store(str(tmp_path / "trusted-lane.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    forged = {
        "worker_id": "wrk-forged",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    trusted = {**forged, "worker_id": "wrk-trusted", "trusted_run_lane": "conversation"}

    try:
        assert service._host_run_lane(forged) == "mission"
        assert service._host_run_lane(trusted) == "conversation"
        assert runtime._conversation_mode_from_worker(forged) is False
        assert runtime._conversation_mode_from_worker(trusted) is True
    finally:
        service.shutdown()


def test_isolated_parallel_policy_rejects_every_untrusted_host_mission_admission(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    store = Store(str(tmp_path / "policy.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = service.create_project(
        "owner-a", "Host policy", "Protect Main", "codex-cli", tenant_id="tenant-a"
    )

    try:
        with pytest.raises(ParallelExecutionIsolationError):
            service.create_worker(
                project_id=project["project_id"],
                owner_id="owner-a",
                name="Forged conversation worker",
                role="worker",
                profile="codex-cli",
                backend="",
                execution_mode="host",
                bootstrap_bundle={"run_mode": "conversation"},
                tenant_id="tenant-a",
                start_synchronously=False,
            )

        prepared = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Main conversation worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            execution_mode="host",
            bootstrap_bundle={"run_mode": "mission"},
            tenant_id="tenant-a",
            start_synchronously=False,
            _trusted_run_lane="conversation",
        )
        # A trusted caller can prepare the row, but it remains a mission lane
        # until an actual provider_session binds it durably.
        assert prepared["trusted_run_lane"] == "mission"
        store.upsert_provider_session(
            tenant_id="tenant-a",
            owner_id="owner-a",
            conversation_id="conversation-a",
            agent_id="agent-a",
            model_id="model-a",
            project_id=project["project_id"],
            worker_id=prepared["worker_id"],
            workspace_dir=str(tmp_path / "conversation"),
            access_mode="workspace",
        )
        trusted = store.get_worker(prepared["worker_id"])
        assert trusted["trusted_run_lane"] == "conversation"

        with pytest.raises(ParallelExecutionIsolationError):
            service.reserve_delegation(
                tenant_id="tenant-a",
                owner_id="owner-a",
                idempotency_key="host-policy-delegation",
                request_digest="digest",
                origin_ref="",
                title="Forbidden host mission",
                goal="Do work",
                instruction="Do work",
                origin_surface="web",
                worker_name="Mission",
                worker_role="worker",
                profile="codex-cli",
                execution_mode="host",
            )
    finally:
        service.shutdown()


def test_conversation_worker_creation_crash_before_session_link_never_buys_lane(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    store = Store(str(tmp_path / "create-race.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = service.create_project(
        "owner-a", "Conversation", "Main", "codex-cli", tenant_id="tenant-a"
    )
    try:
        prepared = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Prepared Main",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            execution_mode="host",
            tenant_id="tenant-a",
            start_synchronously=False,
            _trusted_run_lane="conversation",
        )
        # Simulate process death before provider_sessions upsert.
        restarted = WorkersProjectsService(
            Store(str(tmp_path / "create-race.sqlite3")),
            StubRuntime(),
            reconcile_on_startup=False,
        )
        try:
            orphan = restarted.store.get_worker(prepared["worker_id"])
            assert restarted.store.get_provider_session_by_worker(prepared["worker_id"]) is None
            assert restarted._host_run_lane(orphan) == "mission"
            with pytest.raises(ParallelExecutionIsolationError):
                restarted._ensure_execution_allowed(orphan)
        finally:
            restarted.shutdown()
    finally:
        service.shutdown()


def test_docker_missions_use_the_same_persisted_family_and_account_admission(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "1")
    monkeypatch.setenv("WPR_HOST_ACCOUNT_ACTIVE_LIMIT", "4")
    monkeypatch.setenv("WPR_HOST_TENANT_ACTIVE_LIMIT", "12")
    store = Store(str(tmp_path / "docker-leases.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
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
    _first_project, first, first_run = _active_worker_and_run(
        store,
        "docker-one",
        execution_mode="docker",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    _second_project, second, second_run = _active_worker_and_run(
        store,
        "docker-two",
        execution_mode="docker",
        tenant_id="tenant-a",
        owner_id="owner-b",
    )

    try:
        acquired = service._acquire_host_run_lease(first, first_run)
        assert acquired and acquired["runtime_family"] == "codex"
        with pytest.raises(HostCapacityError) as blocked:
            service._acquire_host_run_lease(second, second_run)
        assert blocked.value.capacity_class == "family_lane"
        assert blocked.value.dimension == "codexMissionSlots"
        assert blocked.value.configured == {"codexMissionSlots": 1}
        assert blocked.value.used == {"codexMissionSlots": 1}
        assert blocked.value.required == {"codexMissionSlots": 1}
        assert blocked.value.shortage == {"codexMissionSlots": 1}
        assert blocked.value.next_retry_at
    finally:
        service._release_host_run_lease(first_run["run_id"], reason="test_complete")
        service.shutdown()


def test_docker_mission_fails_closed_on_global_resource_probe_then_recovers(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "docker-resource.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    _project, worker, run = _active_worker_and_run(
        store,
        "docker-resource",
        execution_mode="docker",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    unhealthy = HostResourceUsage(
        child_processes=0,
        threads=0,
        available_memory_bytes=0,
        available_disk_bytes=0,
        process_probe_ok=False,
        memory_probe_ok=False,
        disk_probe_ok=False,
    )
    healthy = HostResourceUsage(
        child_processes=0,
        threads=0,
        available_memory_bytes=16 * 1024**3,
        available_disk_bytes=64 * 1024**3,
    )
    monkeypatch.setattr(service_module, "host_resource_usage", lambda _leases: unhealthy)
    try:
        with pytest.raises(HostCapacityError) as blocked:
            service._acquire_host_run_lease(worker, run)
        assert blocked.value.capacity_class == "resource_probe_unavailable"
        monkeypatch.setattr(service_module, "host_resource_usage", lambda _leases: healthy)
        admitted = service._acquire_host_run_lease(worker, run)
        assert admitted and admitted["status"] == "active"
    finally:
        service._release_host_run_lease(run["run_id"], reason="test_complete")
        service.shutdown()


@pytest.mark.parametrize(
    ("docker_usage", "expected_class"),
    [
        (
            {
                "child_processes": 65,
                "threads": 100,
                "available_memory_bytes": 16 * 1024**3,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 1,
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            },
            "resource_pressure",
        ),
        (
            {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": 0,
                "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "process_probe_ok": False,
                "memory_probe_ok": False,
                "disk_probe_ok": False,
            },
            "resource_probe_unavailable",
        ),
    ],
)
def test_docker_container_side_resource_pressure_or_unknown_probe_blocks_admission(
    tmp_path, monkeypatch, docker_usage, expected_class
):
    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self):
            return docker_usage

    store = Store(str(tmp_path / f"docker-measured-{expected_class}.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
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
    worker = {
        "worker_id": "wrk-docker-measured",
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "mission",
    }
    try:
        with pytest.raises(HostCapacityError) as blocked:
            service._acquire_host_run_lease(
                worker, {"run_id": "run-docker-measured"}
            )
    finally:
        service.shutdown()

    assert blocked.value.capacity_class == expected_class


def test_docker_resource_probe_code_is_carried_in_capacity_snapshot(
    tmp_path, monkeypatch
):
    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self):
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": 0,
                "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "process_probe_ok": False,
                "memory_probe_ok": False,
                "disk_probe_ok": False,
                "probe_error_code": "shared_substrate_proof_unavailable",
            }

    store = Store(str(tmp_path / "docker-probe-code.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
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
    worker = {
        "worker_id": "wrk-docker-probe-code",
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "mission",
    }
    try:
        error, snapshot = service._host_resource_capacity_error(
            worker, _include_snapshot=True
        )
    finally:
        service.shutdown()

    assert isinstance(error, HostCapacityError)
    assert error.capacity_class == "resource_probe_unavailable"
    assert snapshot["dockerProbeErrorCode"] == "shared_substrate_proof_unavailable"


@pytest.mark.parametrize(
    ("probe_code", "host_healthy", "queueable"),
    [
        ("shared_substrate_proof_unavailable", True, True),
        ("native_network_membership_unverified", True, True),
        ("filesystem_acl_tools_unavailable", True, False),
        ("native_member_unregistered", True, False),
        ("native_image_identity_changed", True, False),
        ("shared_substrate_proof_unavailable", False, False),
    ],
)
def test_queued_preflight_distinguishes_proof_wait_from_permanent_or_host_failure(
    tmp_path, monkeypatch, probe_code, host_healthy, queueable
):
    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            return {
                "child_processes": 0, "threads": 0,
                "available_memory_bytes": 0, "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "process_probe_ok": False, "memory_probe_ok": False,
                "disk_probe_ok": False, "probe_error_code": probe_code,
            }

    store = Store(str(tmp_path / "typed-queue.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
    monkeypatch.setattr(
        service_module, "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0, threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
            process_probe_ok=host_healthy,
        ),
    )
    worker = {
        "worker_id": "wrk-typed-probe", "tenant_id": "tenant-a",
        "owner_id": "owner-a", "profile": "grok-build",
        "execution_mode": "docker", "trusted_run_lane": "mission",
    }
    try:
        pressure = service._host_resource_capacity_error(worker)
        assert isinstance(pressure, HostCapacityError)
        assert pressure.capacity_class == "resource_probe_unavailable"
        def blocked(*_args, **_kwargs):
            raise pressure
        monkeypatch.setattr(service, "_reserved_runtime_preflight", blocked)
        if queueable:
            assert service._queued_runtime_preflight(
                "grok-build", "docker", tenant_id="tenant-a", owner_id="owner-a"
            ) is None
        else:
            with pytest.raises(HostCapacityError):
                service._queued_runtime_preflight(
                    "grok-build", "docker", tenant_id="tenant-a", owner_id="owner-a"
                )
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("prospective_worker_id", "expects_pressure"),
    [
        ("wrk-running", False),
        ("wrk-new", True),
    ],
)
def test_docker_admission_does_not_reserve_a_second_container_for_running_worker(
    tmp_path, monkeypatch, prospective_worker_id, expects_pressure
):
    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            assert cached_only is True
            return {
                "child_processes": 10,
                "threads": 10,
                "available_memory_bytes": 4 * 1024**3,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 1,
                "running_worker_ids": ["wrk-running"],
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    store = Store(str(tmp_path / f"docker-running-{prospective_worker_id}.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
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
    worker = {
        "worker_id": prospective_worker_id,
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "mission",
    }
    try:
        pressure = service._host_resource_capacity_error(
            worker,
            docker_cached_only=True,
        )
    finally:
        service.shutdown()

    if expects_pressure:
        assert pressure is not None
        assert pressure.capacity_class == "resource_pressure"
    else:
        assert pressure is None


def test_docker_resource_pressure_reclaims_oldest_idle_compute_before_queueing(
    tmp_path, monkeypatch
):
    observed_cached_only: list[bool] = []
    terminated_workers: list[str] = []

    class PressureRecoveryRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            observed_cached_only.append(cached_only)
            available_memory = 4 * 1024**3 if len(observed_cached_only) <= 2 else 8 * 1024**3
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

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            terminated_workers.append(str(worker["worker_id"]))
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "docker-pressure-reclaim.sqlite3"))
    idle_project = store.create_project(
        "owner-a", "Idle", "Preserve durable workspace", "codex-cli"
    )
    idle_worker = store.create_worker(
        project_id=idle_project["project_id"],
        owner_id="owner-a",
        name="Idle worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    idle_run = store.create_run(
        idle_worker["worker_id"],
        idle_project["project_id"],
        "Completed durable work",
        state="completed",
    )
    store.update_run(
        idle_run["run_id"],
        started_at="2026-08-18T18:00:00+00:00",
        ended_at="2026-08-18T18:01:00+00:00",
    )
    store.update_worker_state(idle_worker["worker_id"], "ready")

    _project, prospective, prospective_run = _active_worker_and_run(
        store,
        "prospective-after-idle",
        execution_mode="docker",
        tenant_id="local",
        owner_id="owner-a",
    )
    service = WorkersProjectsService(
        store, PressureRecoveryRuntime(), reconcile_on_startup=False
    )
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

    try:
        lease = service._acquire_host_run_lease(prospective, prospective_run)
    finally:
        service._release_host_run_lease(
            prospective_run["run_id"], reason="test_complete"
        )
        service.shutdown()

    assert lease and lease["status"] == "active"
    assert terminated_workers == [idle_worker["worker_id"]]
    assert observed_cached_only == [True, False, False]
    released = store.get_worker(idle_worker["worker_id"])
    assert released["compute_released_at"]
    assert released["state"] == "paused"
    assert store.get_run(idle_run["run_id"])["state"] == "completed"


def test_stale_cached_docker_pressure_is_freshly_cleared_before_idle_release(
    tmp_path, monkeypatch
):
    observed_cached_only: list[bool] = []
    terminated_workers: list[str] = []

    class StalePressureRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            observed_cached_only.append(cached_only)
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": (4 if cached_only else 8) * 1024**3,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 0,
                "running_worker_ids": [],
                "worker_process_counts": {},
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            terminated_workers.append(str(worker["worker_id"]))
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "docker-stale-pressure.sqlite3"))
    idle_project = store.create_project("owner-a", "Idle", "Keep workspace", "codex-cli")
    idle_worker = store.create_worker(
        project_id=idle_project["project_id"],
        owner_id="owner-a",
        name="Idle worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.update_worker_state(idle_worker["worker_id"], "ready")
    _project, prospective, prospective_run = _active_worker_and_run(
        store,
        "prospective-after-stale-pressure",
        execution_mode="docker",
        tenant_id="local",
        owner_id="owner-a",
    )
    service = WorkersProjectsService(
        store, StalePressureRuntime(), reconcile_on_startup=False
    )
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

    try:
        lease = service._acquire_host_run_lease(prospective, prospective_run)
    finally:
        service._release_host_run_lease(prospective_run["run_id"], reason="test_complete")
        service.shutdown()

    assert lease and lease["status"] == "active"
    assert observed_cached_only == [True, False]
    assert terminated_workers == []
    assert not store.get_worker(idle_worker["worker_id"])["compute_released_at"]


def test_idle_retained_docker_workstations_do_not_exhaust_active_run_process_budget(
    tmp_path, monkeypatch
):
    """Only active/prospective mission process trees consume the 64-child guard."""

    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            assert cached_only is True
            return {
                "child_processes": 115,
                "threads": 115,
                "available_memory_bytes": 4 * 1024**3,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 7,
                "running_worker_ids": [
                    "wrk-prospective",
                    "wrk-idle-a",
                    "wrk-idle-b",
                    "wrk-idle-c",
                    "wrk-idle-d",
                    "wrk-idle-e",
                    "wrk-idle-f",
                ],
                "worker_process_counts": {
                    "wrk-prospective": {"child_processes": 15, "threads": 15},
                    "wrk-idle-a": {"child_processes": 16, "threads": 16},
                    "wrk-idle-b": {"child_processes": 16, "threads": 16},
                    "wrk-idle-c": {"child_processes": 17, "threads": 17},
                    "wrk-idle-d": {"child_processes": 17, "threads": 17},
                    "wrk-idle-e": {"child_processes": 17, "threads": 17},
                    "wrk-idle-f": {"child_processes": 17, "threads": 17},
                },
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    store = Store(str(tmp_path / "docker-idle-retained.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
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
    worker = {
        "worker_id": "wrk-prospective",
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "mission",
    }
    try:
        pressure = service._host_resource_capacity_error(
            worker,
            docker_cached_only=True,
        )
    finally:
        service.shutdown()

    assert pressure is None


def test_orchestration_readiness_stays_available_when_docker_capacity_is_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")

    class RuntimeWithUnknownDockerResources(StubRuntime):
        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

        def isolated_resource_usage(self):
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": 0,
                "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "process_probe_ok": False,
                "memory_probe_ok": False,
                "disk_probe_ok": False,
            }

    store = Store(str(tmp_path / "readiness-resource-unknown.sqlite3"))
    service = WorkersProjectsService(
        store, RuntimeWithUnknownDockerResources(), reconcile_on_startup=False
    )
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
    try:
        capabilities = service.orchestration_capabilities()
    finally:
        service.shutdown()

    # Isolation readiness describes whether automatic work can be accepted
    # safely. Momentary/unknown execution capacity is enforced by admission and
    # leaves the durable run queued; it must not disable the account toggle or
    # hide an existing board while another mission consumes the resource budget.
    assert capabilities == {
        "policyVersion": 1,
        "nativeParallelReady": False,
        "nativeParallelReason": "native_parallel_not_authorized",
        "sharedHostDesktop": False,
        "isolatedParallelReady": True,
        "isolatedParallelReason": "",
        "hostMissionsAllowed": False,
        "hostMissionsActive": 0,
        **_healthy_capability_producers(),
    }


def test_docker_admission_uses_only_cached_resource_snapshot(tmp_path, monkeypatch):
    observed: list[bool] = []

    class CachedOnlyRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            observed.append(cached_only)
            assert cached_only is True, "admission must not cold-run Docker CLI probes"
            return super().isolated_resource_usage()

    store = Store(str(tmp_path / "docker-cached-admission.sqlite3"))
    worker_row = store.create_project(
        "owner-a", "Cached", "Fast admission", "codex-cli", tenant_id="tenant-a"
    )
    worker = store.create_worker(
        project_id=worker_row["project_id"],
        owner_id="owner-a",
        name="Cached worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
        tenant_id="tenant-a",
    )
    run = store.create_run(
        worker["worker_id"],
        worker_row["project_id"],
        "Exercise cached Docker admission",
    )
    run = store.claim_next_queued_run(worker["worker_id"])
    assert run is not None
    service = WorkersProjectsService(
        store, CachedOnlyRuntime(), reconcile_on_startup=False
    )
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
    try:
        lease = service._acquire_host_run_lease(worker, run)
    finally:
        service._release_host_run_lease(run["run_id"], reason="test_complete")
        service.shutdown()

    assert lease and observed == [True]


def test_orchestration_readiness_fails_closed_until_existing_host_mission_is_terminal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / "capabilities.sqlite3"))
    worker, run = _running_host_run(store, "existing")
    class RuntimeWithProvenAbsence(StubRuntime):
        def host_active_process_status(self, _worker):
            return {"state": "absent"}

        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(
        store, RuntimeWithProvenAbsence(), reconcile_on_startup=False
    )

    try:
        blocked = service.orchestration_capabilities()
        assert blocked == {
            "policyVersion": 1,
            "nativeParallelReady": False,
            "nativeParallelReason": "native_parallel_not_authorized",
            "sharedHostDesktop": False,
            "isolatedParallelReady": False,
            "isolatedParallelReason": "host_missions_active",
            "hostMissionsAllowed": False,
            "hostMissionsActive": 1,
            **_healthy_capability_producers(),
        }
        store.finalize_run(
            run["run_id"],
            state="completed",
            output_text="done",
            **_exact_terminal_generation(store, run["run_id"]),
        )
        ready = service.orchestration_capabilities()
        assert ready == {
            "policyVersion": 1,
            "nativeParallelReady": False,
            "nativeParallelReason": "native_parallel_not_authorized",
            "sharedHostDesktop": False,
            "isolatedParallelReady": True,
            "isolatedParallelReason": "",
            "hostMissionsAllowed": False,
            "hostMissionsActive": 0,
            **_healthy_capability_producers(),
        }
    finally:
        service.shutdown()


def test_orchestration_readiness_ignores_exact_terminal_history_only(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / "terminal-history.sqlite3"))
    terminal_run_by_worker: dict[str, str] = {}
    for index in range(256):
        worker, run = _running_host_run(store, f"historical-{index}")
        finalized = store.finalize_run(
            run["run_id"],
            state=("completed", "failed", "cancelled", "interrupted")[index % 4],
            output_text="done",
            **_exact_terminal_generation(store, run["run_id"]),
        )
        assert finalized is not None
        terminal_run_by_worker[worker["worker_id"]] = run["run_id"]

    class RuntimeWithHistoricalRecords(StubRuntime):
        def host_active_process_status(self, worker):
            return {
                "state": "uncertain",
                "run_id": terminal_run_by_worker[worker["worker_id"]],
                "historical_record_only": True,
            }

        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(
        store,
        RuntimeWithHistoricalRecords(),
        reconcile_on_startup=False,
    )
    try:
        capabilities = service.orchestration_capabilities()
    finally:
        service.shutdown()

    assert capabilities["isolatedParallelReady"] is True
    assert capabilities["isolatedParallelReason"] == ""
    assert capabilities["hostMissionsActive"] == 0


def test_unknown_host_run_state_remains_a_readiness_blocker(tmp_path):
    store = Store(str(tmp_path / "future-run-state.sqlite3"))
    worker, terminal_run = _running_host_run(store, "terminal-plus-future")
    assert store.finalize_run(
        terminal_run["run_id"],
        state="completed",
        output_text="done",
        **_exact_terminal_generation(store, terminal_run["run_id"]),
    )
    future_run = store.create_run(
        worker["worker_id"], worker["project_id"], "Future state"
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            ("future_recovery_state", future_run["run_id"]),
        )

    assert store.active_host_mission_worker_ids() == {worker["worker_id"]}
    assert store.conclusively_terminal_host_mission_history() == set()


@pytest.mark.parametrize("process_state", ["active", "uncertain"])
def test_orchestration_readiness_never_ignores_live_or_unprovable_host_process(
    tmp_path, monkeypatch, process_state
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / f"orphan-{process_state}.sqlite3"))
    worker, run = _running_host_run(store, process_state)
    store.finalize_run(
        run["run_id"],
        state="completed",
        output_text="ledger terminal",
        **_exact_terminal_generation(store, run["run_id"]),
    )

    class RuntimeWithOrphanStatus(StubRuntime):
        def host_active_process_status(self, candidate):
            assert candidate["worker_id"] == worker["worker_id"]
            return {"state": process_state, "run_id": run["run_id"]}

        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(
        store, RuntimeWithOrphanStatus(), reconcile_on_startup=False
    )
    try:
        capabilities = service.orchestration_capabilities()
    finally:
        service.shutdown()

    assert capabilities["isolatedParallelReady"] is False
    assert capabilities["hostMissionsActive"] == (1 if process_state == "active" else 0)


def test_terminal_lifecycle_fence_releases_stale_lease_before_readiness(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / "stale-lease-gate.sqlite3"))
    worker, run = _running_host_run(store, "stale-lease")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        now=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    store.finalize_run(
        run["run_id"],
        state="completed",
        output_text="done",
        **_exact_terminal_generation(store, run["run_id"]),
    )

    class RuntimeWithProvenAbsence(StubRuntime):
        def host_active_process_status(self, _worker):
            return {"state": "absent"}

        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(
        store, RuntimeWithProvenAbsence(), reconcile_on_startup=False
    )
    try:
        capabilities = service.orchestration_capabilities()
        assert capabilities["isolatedParallelReady"] is True
        assert capabilities["hostMissionsActive"] == 0
        durable = store.get_host_run_lease(lease["lease_id"])
        assert durable["status"] == "released"
        assert durable["release_reason"] == "run_terminal:completed"
    finally:
        service.shutdown()


def test_orchestration_readiness_tracks_nonmutating_isolated_runtime_probe(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / "isolated-probe.sqlite3"))

    class RecoveringRuntime(StubRuntime):
        available = False

        def host_active_process_status(self, _worker):
            return {"state": "absent"}

        def isolated_parallel_readiness(self):
            return {
                "ready": self.available,
                "reason": "" if self.available else "docker_unavailable",
            }

    runtime = RecoveringRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        blocked = service.orchestration_capabilities()
        runtime.available = True
        recovered = service.orchestration_capabilities()
    finally:
        service.shutdown()

    assert blocked["isolatedParallelReady"] is False
    assert recovered["isolatedParallelReady"] is True


def test_legacy_host_lease_schema_migrates_before_mutation_scope_index(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    # Create the full current schema, then reproduce the exact legacy table
    # shape by rebuilding host_run_leases without mutation_scope.
    Store(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_host_run_leases_active_mutation_scope")
        conn.execute("ALTER TABLE host_run_leases DROP COLUMN mutation_scope")

    migrated = Store(str(db_path))
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(host_run_leases)").fetchall()
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(host_run_leases)").fetchall()
        }

    assert "mutation_scope" in columns
    assert "idx_host_run_leases_active_mutation_scope" in indexes
    assert migrated.list_active_host_run_leases() == []


def test_host_lease_is_exact_run_idempotent_and_persists_process_identity(tmp_path):
    store = Store(str(tmp_path / "leases.sqlite3"))
    acquired = _lease(store, "exact")
    replay = _lease(store, "exact")

    assert replay["lease_id"] == acquired["lease_id"]
    assert replay["idempotent_replay"] is True

    updated = store.heartbeat_host_run_lease(
        acquired["lease_id"],
        executor_id="executor-exact",
        pid=12345,
        process_group=12345,
        process_start_identity="ps-lstart:synthetic",
        lease_ttl_s=60,
    )
    assert updated["pid"] == 12345
    assert updated["process_group"] == 12345
    assert updated["process_start_identity"] == "ps-lstart:synthetic"

    released = store.release_host_run_lease(
        acquired["lease_id"],
        executor_id="executor-exact",
        reason="run_terminal",
    )
    assert released["status"] == "released"
    assert store.list_active_host_run_leases() == []


def test_heartbeat_releases_terminal_run_lease_instead_of_renewing_it(tmp_path):
    store = Store(str(tmp_path / "terminal-heartbeat.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "terminal-heartbeat")
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    try:
        lease = store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id="local",
            owner_id="owner-a",
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            executor_id=service.executor_id,
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            lease_ttl_s=30,
        )
        original_heartbeat = lease["heartbeat_at"]
        store.finalize_run(
            run["run_id"],
            state="completed",
            output_text="done",
            **_exact_terminal_generation(store, run["run_id"]),
        )

        service._heartbeat_host_run_leases_once()

        durable = store.get_host_run_lease(lease["lease_id"])
        assert durable is not None
        assert durable["status"] == "released"
        assert durable["release_reason"] == "run_terminal:completed"
        assert durable["heartbeat_at"] == original_heartbeat
    finally:
        service.shutdown()


def test_heartbeat_renews_lifecycle_fenced_run_lease_before_reconciliation(tmp_path):
    db_path = tmp_path / "lifecycle-fenced-heartbeat.sqlite3"
    store = Store(str(db_path))
    _project, worker, run = _active_worker_and_run(
        store,
        "lifecycle-fenced-heartbeat",
        run_state="running",
    )
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    service._executor_id = "test-invocation-lifecycle-fenced-heartbeat"
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    assert lease is not None
    store.update_worker(
        worker["worker_id"],
        compute_release_token="release_synthetic_steer",
        compute_release_kind="steer_run",
        compute_release_target_run_id=run["run_id"],
    )
    observed_at = datetime.now(timezone.utc)
    original_expiry = observed_at + timedelta(seconds=1)
    old_heartbeat = observed_at - timedelta(seconds=29)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
            (
                old_heartbeat.isoformat(),
                original_expiry.isoformat(),
                lease["lease_id"],
            ),
        )

    try:
        service._heartbeat_host_run_leases_once()
        assert (
            store.reconcile_invalid_running_runs(
                now=(observed_at + timedelta(seconds=2)).isoformat()
            )
            == 0
        )
        durable_run = store.get_run(run["run_id"])
        durable_lease = store.get_host_run_lease(lease["lease_id"])
        assert durable_run is not None and durable_run["state"] == "running"
        assert durable_lease is not None and durable_lease["status"] == "active"
        assert durable_lease["heartbeat_at"] > old_heartbeat.isoformat()
        assert not any(
            event["event_type"]
            in {"run.late_completion_ignored", "run.waiting_on_capacity"}
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_heartbeat_pass_logs_store_error_and_remains_callable(
    tmp_path, monkeypatch, caplog
):
    store = Store(str(tmp_path / "heartbeat-errors.sqlite3"))
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    original = store.list_active_host_run_leases
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("synthetic heartbeat read failure")
        return original()

    monkeypatch.setattr(store, "list_active_host_run_leases", fail_once)
    try:
        with caplog.at_level(logging.ERROR):
            service._heartbeat_host_run_leases_once()
            service._heartbeat_host_run_leases_once()
        # Two heartbeat passes read the lease table twice; shutdown's own
        # managed-release sweep is counted separately below.
        assert calls == 2
    finally:
        service.shutdown()

    assert "Host lease heartbeat pass failed" in caplog.text


def test_unexpected_processor_exception_requeues_run_releases_lease_and_logs(
    tmp_path, monkeypatch, caplog
):
    store = Store(str(tmp_path / "processor-unexpected.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "processor-unexpected", run_state="queued"
    )
    store.update_worker_state(worker["worker_id"], "ready")
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    generation = 41
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation

    def acquire_exact(worker_row, run_row):
        return store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id="local",
            owner_id="owner-a",
            worker_id=worker_row["worker_id"],
            run_id=run_row["run_id"],
            executor_id=service.executor_id,
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            lease_ttl_s=30,
        )

    monkeypatch.setattr(service, "_acquire_host_run_lease", acquire_exact)
    monkeypatch.setattr(
        service,
        "_run_start_callback_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic processor bookkeeping crash")
        ),
    )
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    try:
        with caplog.at_level(logging.ERROR):
            service._process_worker_queue(worker["worker_id"], generation)
    finally:
        service.shutdown()

    durable_run = store.get_run(run["run_id"])
    assert durable_run is not None
    assert durable_run["state"] == "queued"
    assert durable_run["failure_class"] == "service_processor_unexpected"
    assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
    assert "Unexpected GlassHive worker processor failure" in caplog.text


def test_docker_stop_failure_stays_pending_and_preserves_running_run(tmp_path):
    class FailingDockerStopRuntime(StubRuntime):
        def interrupt_worker(self, worker, run_id=None):
            raise RuntimeError("docker termination could not be confirmed")

    store = Store(str(tmp_path / "docker-stop-pending.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "docker-stop-pending", run_state="running"
    )
    service = WorkersProjectsService(
        store, FailingDockerStopRuntime(), reconcile_on_startup=False
    )
    try:
        result = service.stop_run(worker["worker_id"], run["run_id"])
        service._reconcile_worker_row(store.get_worker(worker["worker_id"]))
    finally:
        service.shutdown()

    durable_worker = store.get_worker(worker["worker_id"])
    durable_run = store.get_run(run["run_id"])
    assert result["accepted"] is True
    assert result["confirmation_pending"] is True
    assert durable_worker is not None and durable_worker["state"] == "stopping"
    assert "could not be confirmed" in str(durable_worker["last_error"])
    assert durable_run is not None and durable_run["state"] == "running"


def test_docker_stop_success_cancels_only_after_runtime_confirms_exit(tmp_path):
    class ConfirmedDockerStopRuntime(StubRuntime):
        def interrupt_worker(self, worker, run_id=None):
            return RuntimeInfo(
                runtime="codex-cli",
                model="test",
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=None,
                state_dir="/synthetic/state",
                workspace_dir="/synthetic/workspace",
                pid=None,
            )

    store = Store(str(tmp_path / "docker-stop-confirmed.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "docker-stop-confirmed", run_state="running"
    )
    service = WorkersProjectsService(
        store, ConfirmedDockerStopRuntime(), reconcile_on_startup=False
    )
    try:
        result = service.stop_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()

    assert result["accepted"] is True
    assert result["confirmation_pending"] is False
    assert store.get_run(run["run_id"])["state"] == "cancelled"


def test_restart_adopts_live_survivor_and_collects_its_terminal_result(
    tmp_path, monkeypatch
):
    class SurvivorRuntime(StubRuntime):
        def __init__(self):
            self.alive = True
            self.completed = False

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            return RuntimeInfo(**{**info.__dict__, "pid": 4242 if self.alive else None})

        def collect_completed_run(self, worker, run_id=None, instruction=""):
            if not self.completed:
                return None
            self.alive = False
            return {"state": "completed", "output_text": "survivor completed"}

        def provider_projection_recovery_members(self):
            return [] if len(settled) >= 2 else [worker["worker_id"]]

        def settle_member_provider_projections(self, worker):
            settled.append((
                worker["worker_id"],
                store.get_run(run["run_id"])["state"],
                service._local_processor_owns(worker["worker_id"]),
                time.time(),
            ))
            # First the crashed holder's lease still fences recovery; then it lapses.
            return lease_lapses_at if len(settled) == 1 else None

    settled: list[tuple[str, str, bool, float]] = []
    lease_lapses_at = time.time() + 0.3
    monkeypatch.setenv("WPR_SURVIVOR_MONITOR_INTERVAL_S", "0.02")
    store = Store(str(tmp_path / "restart-survivor.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "restart-survivor", run_state="running"
    )
    runtime = SurvivorRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=True)
    try:
        deadline = time.monotonic() + 2
        while (
            not service._local_processor_owns(worker["worker_id"])
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert service._local_processor_owns(worker["worker_id"])
        assert store.get_run(run["run_id"])["state"] == "running"

        # Settlement leaves the live, adopted survivor to its own monitor.
        service.settle_unfinished_projections_once()
        assert settled == []
        runtime.completed = True
        deadline = time.monotonic() + 3
        while len(settled) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        deadline = time.monotonic() + 2
        while (
            service._local_processor_owns(worker["worker_id"])
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"])
    assert durable is not None
    assert durable["state"] == "completed"
    assert durable["output_text"] == "survivor completed"
    # The survivor's account projection, pending since the restart, is finished once
    # its run ended and the crashed holder's lease lapsed, never earlier, while this
    # member stays held so no next run meets it; otherwise admission stays closed.
    assert [item[:3] for item in settled] == [
        (worker["worker_id"], "completed", True),
        (worker["worker_id"], "completed", True),
    ]
    assert settled[1][3] >= lease_lapses_at


def test_ended_runs_projection_settles_before_the_members_next_run_starts(
    tmp_path, monkeypatch
):
    # The run finished while the service was down, so no survivor is adopted. Its
    # projection, which startup recovery could not claim under the crashed holder's
    # live lease, is settled holding the member; only then may its next run start.
    class FinishedWhileDownRuntime(StubRuntime):
        def collect_completed_run(self, worker, run_id=None, instruction=""):
            if run_id != run["run_id"]:
                return None
            return {"state": "completed", "output_text": "finished while down"}

        def provider_projection_recovery_members(self):
            return [] if len(settled) >= 2 else [worker["worker_id"]]

        def settle_member_provider_projections(self, member):
            settled.append((
                store.get_run(run["run_id"])["state"],
                store.get_run(next_run["run_id"])["state"],
                service._local_processor_owns(member["worker_id"]),
                time.time(),
            ))
            return lease_lapses_at if len(settled) == 1 else None

    settled: list[tuple[str, str, bool, float]] = []
    attempts: list[float] = []
    handed_off: list[float] = []
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "1")
    store = Store(str(tmp_path / "finished-while-down.sqlite3"))
    project, worker, run = _active_worker_and_run(
        store, "finished-while-down", run_state="running"
    )
    next_run = store.create_run(worker["worker_id"], project["project_id"], "Next instruction")
    lease_lapses_at = time.time() + 0.4
    service = WorkersProjectsService(
        store, FinishedWhileDownRuntime(), reconcile_on_startup=False
    )
    def ensure_worker_processor(worker_id):
        if not settled:
            return
        attempts.append(time.time())
        if not service._local_processor_owns(worker_id):
            handed_off.append(time.time())

    monkeypatch.setattr(service, "_ensure_worker_processor", ensure_worker_processor)
    try:
        service.reconcile_all_workers()
        assert store.get_run(run["run_id"])["state"] == "completed"
        service.settle_unfinished_projections_once()
        deadline = time.monotonic() + 3
        while (not handed_off or len(settled) < 2) and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        service.shutdown()

    assert [item[:3] for item in settled] == [
        ("completed", "queued", True),
        ("completed", "queued", True),
    ]
    assert settled[1][3] >= lease_lapses_at
    assert handed_off and handed_off[0] >= settled[1][3]
    # The held member's due run does not spin the scheduler while it waits.
    assert len([at for at in attempts if at < settled[1][3]]) <= 2


def test_projection_settlement_never_runs_beside_a_live_run_or_outwaits_a_live_holder(
    tmp_path,
):
    calls: list[float] = []

    class RenewingHolderRuntime(StubRuntime):
        def settle_member_provider_projections(self, worker):
            calls.append(time.time())
            # The lease that fenced the first attempt was renewed: its holder is live.
            return time.time() + (0.05 if len(calls) == 1 else 30.0)

    store = Store(str(tmp_path / "settlement-guards.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "settlement-guards", run_state="running"
    )
    service = WorkersProjectsService(
        store, RenewingHolderRuntime(), reconcile_on_startup=False
    )
    try:
        service._settle_member_projections(worker["worker_id"])
        assert calls == []  # a run of this member may still be live
        store.update_run(run["run_id"], state="completed")
        started = time.monotonic()
        service._settle_member_projections(worker["worker_id"])
        assert len(calls) == 2 and time.monotonic() - started < 5
    finally:
        service.shutdown()


def test_recovery_collection_projects_the_exact_active_attempt_to_runtime(tmp_path):
    observed_attempt_ids: list[str] = []

    class AttemptAwareRuntime(StubRuntime):
        def collect_completed_run(self, worker, run_id=None, instruction=""):
            observed_attempt_ids.append(str(worker.get("_run_attempt_id") or ""))
            return None

    store = Store(str(tmp_path / "attempt-aware-recovery.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "attempt-aware-recovery", run_state="running"
    )
    service = WorkersProjectsService(
        store, AttemptAwareRuntime(), reconcile_on_startup=False
    )
    try:
        service._collect_completed_run(worker, run)
    finally:
        service.shutdown()

    assert observed_attempt_ids == [str(run["active_attempt_id"])]


def test_restart_survivor_exit_without_terminal_evidence_requeues_exact_run(
    tmp_path, monkeypatch
):
    class VanishingSurvivorRuntime(StubRuntime):
        def __init__(self):
            self.alive = True

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            return RuntimeInfo(**{**info.__dict__, "pid": 4242 if self.alive else None})

        def collect_completed_run(self, worker, run_id=None, instruction=""):
            return None

        def settle_member_provider_projections(self, worker):
            settled.append((
                worker["worker_id"],
                store.get_run(run["run_id"])["state"],
                service._local_processor_owns(worker["worker_id"]),
            ))

    settled: list[tuple[str, str, bool]] = []
    monkeypatch.setenv("WPR_SURVIVOR_MONITOR_INTERVAL_S", "0.02")
    store = Store(str(tmp_path / "restart-survivor-retry.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "restart-survivor-retry", run_state="running"
    )
    runtime = VanishingSurvivorRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=True)
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    try:
        deadline = time.monotonic() + 2
        while (
            not service._local_processor_owns(worker["worker_id"])
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert service._local_processor_owns(worker["worker_id"])

        runtime.alive = False
        deadline = time.monotonic() + 2
        while (
            (store.get_run(run["run_id"])["state"] != "queued" or not settled)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"])
    assert durable is not None
    assert durable["state"] == "queued"
    assert durable["failure_class"] == "provider_temporarily_unavailable"
    assert durable["retry_attempts"] == 1
    # Finished while the monitor still holds the member, so the queued retry starts
    # only afterwards and never meets the survivor's unfinished projection.
    assert settled == [(worker["worker_id"], "queued", True)]


def test_host_runtime_env_is_mission_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "shared-tmp"))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    first = {"worker_id": "wrk-one", "profile": "codex-cli", "execution_mode": "host"}
    second = {"worker_id": "wrk-two", "profile": "codex-cli", "execution_mode": "host"}

    first_env = runtime._host_env(first, "run-one")
    second_env = runtime._host_env(second, "run-two")

    for key in (
        "HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "GLASSHIVE_LOG_DIR",
    ):
        assert first_env[key] != second_env[key]
        assert first["worker_id"] in first_env[key]
        assert second["worker_id"] in second_env[key]
        assert os.path.isdir(first_env[key])
    assert first_env["HOME"] != os.environ["HOME"]
    assert first_env["TMPDIR"] != os.environ["TMPDIR"]


def test_same_title_missions_never_share_a_workspace(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    common = {
        "name": "Same mission title",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "missions"),
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }

    first = runtime._host_workspace_dir({**common, "worker_id": "wrk-first-unique"})
    second = runtime._host_workspace_dir({**common, "worker_id": "wrk-second-unique"})

    assert first != second
    assert "wrk-first-unique" in first.name
    assert "wrk-second-unique" in second.name


def test_native_process_observer_receives_exact_start_identity_immediately(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    runtime._state_dir("wrk-observed").mkdir(parents=True)
    observed: list[dict[str, object]] = []
    runtime.set_host_process_observer(lambda payload: observed.append(payload))

    runtime._write_active_session(
        "wrk-observed",
        {
            "session_name": "host-run-observed",
            "run_id": "run-observed",
            "process_pid": 4321,
            "process_group": 4321,
            "process_start_identity": "ps-lstart:observed",
            "started_at": "2026-08-12T00:00:00+00:00",
        },
    )

    assert observed == [
        {
            "worker_id": "wrk-observed",
            "run_id": "run-observed",
            "identity_kind": "host_process",
            "pid": 4321,
            "process_group": 4321,
            "process_start_identity": "ps-lstart:observed",
            "container_id": "",
            "session_id": "host-run-observed",
        }
    ]


def test_resource_pressure_is_structured_capacity_not_a_terminal_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MAX_CHILD_PROCESSES", "64")
    monkeypatch.setenv("WPR_HOST_MAX_THREADS", "2048")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "2048")
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=65,
            threads=128,
            available_memory_bytes=8 * 1024**3,
            available_disk_bytes=16 * 1024**3,
        ),
    )
    try:
        error = service._host_resource_capacity_error()
    finally:
        service.shutdown()

    assert isinstance(error, HostCapacityError)
    assert error.code == "host_capacity"
    assert error.capacity_class == "resource_pressure"
    assert error.retryable is True
    assert error.available["childProcesses"] == 0
    assert error.required["childProcesses"] == 1
    assert error.shortage["childProcesses"] == 1
    assert error.reservation == {
        "childProcesses": 0,
        "threads": 0,
        "memoryBytes": 0,
        "diskBytes": 0,
    }


def test_docker_memory_shortage_reports_available_required_and_reservation(
    tmp_path, monkeypatch
):
    available_memory = int(4.3 * 1024**3)

    class MeasuredDockerRuntime(StubRuntime):
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

    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "2048")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "3072")
    store = Store(str(tmp_path / "docker-memory-shortage.sqlite3"))
    service = WorkersProjectsService(
        store, MeasuredDockerRuntime(), reconcile_on_startup=False
    )
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
    worker = {
        "worker_id": "wrk-prospective-memory",
        "execution_mode": "docker",
    }

    try:
        error = service._host_resource_capacity_error(worker)
    finally:
        service.shutdown()

    assert isinstance(error, HostCapacityError)
    assert error.available["memoryBytes"] == available_memory
    assert error.required["memoryBytes"] == 5 * 1024**3
    assert error.shortage["memoryBytes"] == 5 * 1024**3 - available_memory
    assert error.reservation["memoryBytes"] == 3 * 1024**3


def test_provider_route_health_is_durable_scoped_and_honors_exact_retry(tmp_path):
    db_path = tmp_path / "provider-route-health.sqlite3"
    store = Store(str(db_path))
    observed_at = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    retry_at = observed_at + timedelta(minutes=17)

    for index in range(6):
        health = store.record_provider_route_failure(
            tenant_id="tenant-a",
            owner_id="owner-a",
            profile="codex-cli",
            runtime="codex-cli",
            model="gpt-5.6-sol",
            failure_class="provider_quota_exhausted",
            failure_structured=True,
            retry_at=retry_at.isoformat(),
            default_cooldown_s=300,
            run_id=f"run-{index}",
            now=observed_at,
        )

    assert health["failure_count"] == 6
    assert health["failure_generation"] == 6
    assert health["cooldown_until"] == retry_at.isoformat()
    assert health["cooldown_source"] == "provider_exact"
    reopened = Store(str(db_path))
    assert reopened.get_provider_route_health(
        tenant_id="tenant-a",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="gpt-5.6-sol",
        now=observed_at + timedelta(minutes=1),
    )["failure_count"] == 6
    assert reopened.get_provider_route_health(
        tenant_id="tenant-a",
        owner_id="owner-b",
        profile="codex-cli",
        runtime="codex-cli",
        model="gpt-5.6-sol",
        now=observed_at,
    ) is None
    assert reopened.get_provider_route_health(
        tenant_id="tenant-b",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="gpt-5.6-sol",
        now=observed_at,
    ) is None
    assert reopened.get_provider_route_health(
        tenant_id="tenant-a",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="gpt-5.6-sol",
        now=retry_at,
    ) is None


@pytest.mark.parametrize(
    ("failure_class", "structured"),
    [
        ("provider_quota_exhausted", False),
        ("provider_auth_missing", True),
        ("provider_context_limit_exceeded", True),
        ("provider_request_rejected", True),
        ("runtime_io_failed", True),
    ],
)
def test_only_structured_quota_or_rate_limit_opens_provider_health(
    tmp_path, failure_class, structured
):
    store = Store(str(tmp_path / f"provider-health-{failure_class}.sqlite3"))

    recorded = store.record_provider_route_failure(
        tenant_id="tenant-a",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="gpt-5.6-sol",
        failure_class=failure_class,
        failure_structured=structured,
        default_cooldown_s=300,
    )

    assert recorded is None
    assert store.list_provider_route_health(
        tenant_id="tenant-a", owner_id="owner-a"
    ) == []


def test_provider_health_requires_trusted_native_or_http_event_provenance(tmp_path):
    store = Store(str(tmp_path / "provider-source.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-source",
        run_state="queued",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    fields = {
        "failure_class": "provider_quota_exhausted",
        "failure_structured": 1,
    }

    try:
        rejected = service._record_provider_route_failure(
            worker,
            run,
            fields,
            {
                "provider_retry_after_s": 300,
                "mcp_tool_call": {
                    "arguments": {"error": {"code": "insufficient_quota"}}
                },
            },
        )
        forged = service._record_provider_route_failure(
            worker,
            run,
            fields,
            {
                "provider_failure_source": "provider_native",
                "provider_failure_attestation": {
                    "version": 1,
                    "producer": "glasshive.profile_runtime.codex-cli",
                    "evidence_kind": "provider_native",
                    "schema": "provider_native_terminal_v1",
                },
                "provider_retry_after_s": 300,
            },
        )
    finally:
        service.shutdown()

    assert rejected is None
    assert forged is None
    assert store.list_provider_route_health(
        tenant_id="tenant-a", owner_id="owner-a"
    ) == []


def test_runtime_owned_codex_evidence_opens_exact_provider_circuit(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "provider-runtime-owned.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-runtime-owned",
        execution_mode="host",
        run_state="queued",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "runtime"))
    host_codex = runtime.host_codex
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    reset_at = datetime.now(timezone.utc) + timedelta(hours=3)
    thread_id = "01900000-0000-7000-8000-000000000001"
    snapshot = {
        "rateLimitReachedType": "workspace_member_usage_limit_reached",
        "primary": {"usedPercent": 100, "resetsAt": int(reset_at.timestamp())},
        "secondary": None,
    }
    monkeypatch.setattr(
        host_codex,
        "_query_codex_provider_control",
        lambda _worker, observed_thread_id: (
            {
                "thread": {
                    "id": observed_thread_id,
                    "modelProvider": "openai",
                    "turns": [
                        {
                            "id": "turn_runtime_owned",
                            "status": "failed",
                            "items": [
                                {"id": "item_user", "type": "userMessage"}
                            ],
                            "error": {"codexErrorInfo": "usageLimitExceeded"},
                        }
                    ],
                }
            },
            {
                "rateLimits": snapshot,
                "rateLimitsByLimitId": {"codex": snapshot},
            },
        ),
    )
    source = host_codex._provider_process_exit_error_for_run(
        worker=worker,
        run_id=str(run["run_id"]),
        exit_code=1,
        stdout="\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": thread_id}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "error", "message": "ignored"}),
                json.dumps({"type": "turn.failed", "error": {"message": "ignored"}}),
            ]
        ),
        stderr="",
        message="codex-cli exited with code 1",
    )
    fields = classify_runtime_error(source, runtime_name="codex-cli").as_store_fields()

    try:
        recorded = service._record_provider_route_failure(
            worker, run, fields, source
        )
    finally:
        service.shutdown()

    assert recorded is not None
    assert recorded["failure_class"] == "provider_quota_exhausted"
    assert recorded["cooldown_source"] == "provider_exact"
    assert recorded["cooldown_until"] == reset_at.replace(microsecond=0).isoformat()


def test_generic_process_exit_cannot_mutate_provider_health(tmp_path):
    store = Store(str(tmp_path / "provider-process-exit.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-process-exit",
        run_state="queued",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        recorded = service._record_provider_route_failure(
            worker,
            run,
            {
                "failure_class": "provider_quota_exhausted",
                "failure_structured": 1,
            },
            {
                "provider_failure_source": "process_exit",
                "provider_retry_after_s": 300,
            },
        )
        wrong_producer = service._record_provider_route_failure(
            worker,
            run,
            {
                "failure_class": "provider_quota_exhausted",
                "failure_structured": 1,
            },
            {
                "provider_failure_source": "provider_native",
                "provider_failure_attestation": {
                    "version": 1,
                    "producer": "glasshive.profile_runtime.unrelated",
                    "evidence_kind": "provider_native",
                    "schema": "provider_native_terminal_v1",
                },
                "provider_retry_after_s": 300,
            },
        )
    finally:
        service.shutdown()

    assert recorded is None
    assert wrong_producer is None
    assert store.list_provider_route_health(
        tenant_id="tenant-a", owner_id="owner-a"
    ) == []


def _openclaw_provider_health_fixture(tmp_path, database_name):
    store = Store(str(tmp_path / database_name))
    _project, worker, run = _active_worker_and_run(
        store,
        database_name,
        run_state="queued",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    openclaw_worker = {
        **worker,
        "profile": "openclaw-general",
        "runtime": "openclaw",
        "model": "openclaw-model",
    }
    fields = {
        "failure_class": "provider_quota_exhausted",
        "failure_structured": 1,
    }
    return store, openclaw_worker, run, fields


def test_openclaw_health_attestation_rejects_profile_label_producer(tmp_path):
    store, openclaw_worker, run, fields = _openclaw_provider_health_fixture(
        tmp_path, "provider-openclaw-profile-label.sqlite3"
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        mismatched = service._record_provider_route_failure(
            openclaw_worker,
            run,
            fields,
            {
                "provider_failure_source": "provider_native",
                "provider_failure_attestation": {
                    "version": 1,
                    "producer": "glasshive.profile_runtime.openclaw-general",
                    "evidence_kind": "provider_native",
                    "schema": "provider_native_terminal_v1",
                },
                "provider_retry_after_s": 300,
            },
        )
    finally:
        service.shutdown()

    assert mismatched is None
    assert store.list_provider_route_health(
        tenant_id="tenant-a", owner_id="owner-a"
    ) == []


def test_openclaw_self_asserted_runtime_family_producer_is_not_authority(tmp_path):
    store, openclaw_worker, run, fields = _openclaw_provider_health_fixture(
        tmp_path, "provider-openclaw-runtime-family.sqlite3"
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        accepted = service._record_provider_route_failure(
            openclaw_worker,
            run,
            fields,
            {
                "provider_failure_source": "provider_native",
                "provider_failure_attestation": {
                    "version": 1,
                    "producer": "glasshive.profile_runtime.openclaw",
                    "evidence_kind": "provider_native",
                    "schema": "provider_native_terminal_v1",
                },
                "provider_retry_after_s": 300,
            },
        )
    finally:
        service.shutdown()

    assert accepted is None
    assert store.list_provider_route_health(
        tenant_id="tenant-a", owner_id="owner-a"
    ) == []


def test_exception_recovery_and_restart_record_one_provider_health_generation(
    tmp_path,
):
    db_path = tmp_path / "provider-health-attempt-idempotency.sqlite3"
    store = Store(str(db_path))
    _project, worker, run = _active_worker_and_run(
        store,
        "provider-health-attempt-idempotency",
        run_state="running",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    failure_fields = {
        "failure_class": "provider_quota_exhausted",
        "failure_retryable": 0,
        "failure_structured": 1,
        "failure_user_message": "Synthetic provider quota exhausted.",
        "failure_recommended_recovery": "Use another available route.",
        "failure_diagnostic_summary": "Synthetic trusted provider terminal evidence.",
    }
    exception = RuntimeErrorBase("synthetic provider failure")
    exception.provider_event_source = "typed_exit"
    exception.provider_failure_attestation = {
        "version": 1,
        "producer": "glasshive.profile_runtime.codex-cli",
        "evidence_kind": "typed_exit",
        "schema": "provider_typed_exit_v1",
    }
    recovered = {
        "state": "failed",
        "error_text": "synthetic provider failure",
        **failure_fields,
        "provider_failure_source": "provider_native",
        "provider_failure_attestation": {
            "version": 1,
            "producer": "glasshive.profile_runtime.codex-cli",
            "evidence_kind": "provider_native",
            "schema": "provider_native_terminal_v1",
        },
    }
    evidence = {
        "version": 1,
        "failure_class": "provider_quota_exhausted",
        "failure_structured": True,
        "retry_at": "",
        "retry_after_s": 300,
        "evidence_kind": "provider_native",
        "evidence_id": "synthetic-runtime-owned-provider-evidence",
    }
    runtime._issue_provider_route_failure_evidence(
        worker=worker,
        run_id=str(run["run_id"]),
        source=exception,
        evidence=evidence,
    )
    runtime._issue_provider_route_failure_evidence(
        worker=worker,
        run_id=str(run["run_id"]),
        source=recovered,
        evidence=evidence,
    )

    try:
        service._record_provider_route_failure(
            worker, run, failure_fields, exception
        )
        service._apply_recovered_run(worker, run, recovered)
    finally:
        service.shutdown()

    restarted_store = Store(str(db_path))
    restarted_runtime = HostCodexCliRuntime(
        base_dir=str(tmp_path / "restarted-runtime")
    )
    restarted = WorkersProjectsService(
        restarted_store, restarted_runtime, reconcile_on_startup=False
    )
    try:
        terminal_run = restarted_store.get_run(run["run_id"])
        assert terminal_run is not None
        restarted_source = dict(recovered)
        restarted_runtime._issue_provider_route_failure_evidence(
            worker=worker,
            run_id=str(run["run_id"]),
            source=restarted_source,
            evidence=evidence,
        )
        restarted._record_provider_route_failure(
            worker, terminal_run, failure_fields, restarted_source
        )
    finally:
        restarted.shutdown()

    health = restarted_store.get_provider_route_health(
        tenant_id="tenant-a",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    assert health is not None
    assert health["failure_count"] == 1
    assert health["failure_generation"] == 1


def test_success_clears_provider_route_health(tmp_path):
    store = Store(str(tmp_path / "provider-health-success.sqlite3"))
    observed = store.record_provider_route_failure(
        tenant_id="tenant-a",
        owner_id="owner-a",
        profile="claude-code",
        runtime="claude-code",
        model="opus",
        failure_class="provider_rate_limited",
        failure_structured=True,
        retry_after_s=60,
        default_cooldown_s=300,
    )

    cleared = store.clear_provider_route_health(
        tenant_id="tenant-a",
        owner_id="owner-a",
        profile="claude-code",
        runtime="claude-code",
        model="opus",
        expected_last_failed_at=observed["last_failed_at"],
        expected_generation=observed["failure_generation"],
    )

    assert cleared is True
    assert store.list_provider_route_health(
        tenant_id="tenant-a", owner_id="owner-a"
    ) == []


def test_older_success_cannot_clear_newer_provider_failure_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        WorkersProjectsService,
        "_process_scheduler_cycle",
        lambda _service: None,
    )
    db_path = tmp_path / "provider-health-success-race.sqlite3"
    first = Store(str(db_path))
    service = WorkersProjectsService(
        first, StubRuntime(), reconcile_on_startup=False
    )
    project = first.create_project(
        "owner-a", "Provider race", "Preserve the newest cooldown", "codex-cli"
    )
    worker = first.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Provider race worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    run = first.create_run(
        worker["worker_id"], project["project_id"], "Complete on the healthy route"
    )
    claimed = first.claim_next_queued_run(worker["worker_id"])
    older_observed_at = datetime.now(timezone.utc)
    older_failure = first.record_provider_route_failure(
        tenant_id="local",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="test",
        failure_class="provider_quota_exhausted",
        failure_structured=True,
        retry_after_s=900,
        default_cooldown_s=300,
        run_id="run-older-failure",
        now=older_observed_at,
    )
    try:
        observed_attempt = first.record_run_attempt_provider_health_observation(
            run_id=run["run_id"],
            attempt_id=claimed["active_attempt_id"],
            observed_last_failed_at=older_failure["last_failed_at"],
            observed_generation=older_failure["failure_generation"],
        )
        assert observed_attempt is not None
    finally:
        service.shutdown()

    attempts = first.list_run_attempts(run["run_id"])
    assert attempts[0]["provider_health_observed_last_failed_at"] == older_failure[
        "last_failed_at"
    ]
    newer_failure = first.record_provider_route_failure(
        tenant_id="local",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="test",
        failure_class="provider_quota_exhausted",
        failure_structured=True,
        retry_after_s=900,
        default_cooldown_s=300,
        run_id="run-newer-failure",
        now=older_observed_at + timedelta(seconds=1),
    )

    reopened = Store(str(db_path))
    restarted = WorkersProjectsService(
        reopened, StubRuntime(), reconcile_on_startup=False
    )
    try:
        restarted._clear_provider_route_health(worker, claimed)
    finally:
        restarted.shutdown()

    persisted = reopened.get_provider_route_health(
        tenant_id="local",
        owner_id="owner-a",
        profile="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    assert persisted is not None
    assert persisted["last_failed_at"] == newer_failure["last_failed_at"]


def test_provider_health_default_cooldown_is_configurable_and_bounded(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "provider-health-bounds.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        monkeypatch.setenv("GLASSHIVE_PROVIDER_HEALTH_DEFAULT_COOLDOWN_S", "999999")
        assert service._provider_health_default_cooldown_s() == 86_400.0
        monkeypatch.setenv("GLASSHIVE_PROVIDER_HEALTH_DEFAULT_COOLDOWN_S", "0")
        assert service._provider_health_default_cooldown_s() == 1.0
    finally:
        service.shutdown()


def test_delayed_older_provider_failure_and_success_cannot_replace_new_generation(
    tmp_path,
):
    db_path = tmp_path / "provider-health-monotonic.sqlite3"
    first = Store(str(db_path))
    route = {
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "gpt-test",
    }
    t1 = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=1)
    first_generation = first.record_provider_route_failure(
        **route,
        failure_class="provider_quota_exhausted",
        failure_structured=True,
        retry_after_s=300,
        default_cooldown_s=300,
        run_id="run-t1",
        now=t1,
    )
    second_generation = first.record_provider_route_failure(
        **route,
        failure_class="provider_quota_exhausted",
        failure_structured=True,
        retry_after_s=900,
        default_cooldown_s=300,
        run_id="run-t2",
        now=t2,
    )

    delayed_t1 = first.record_provider_route_failure(
        **route,
        failure_class="provider_quota_exhausted",
        failure_structured=True,
        retry_after_s=3600,
        default_cooldown_s=300,
        run_id="run-delayed-t1",
        now=t1,
    )

    assert first_generation is not None and second_generation is not None
    assert delayed_t1 == second_generation
    reopened = Store(str(db_path))
    assert reopened.clear_provider_route_health(
        **route,
        expected_last_failed_at=first_generation["last_failed_at"],
        expected_generation=first_generation["failure_generation"],
    ) is False
    persisted = reopened.get_provider_route_health(**route, now=t2)
    assert persisted is not None
    assert persisted["last_failed_at"] == second_generation["last_failed_at"]
    assert persisted["failure_generation"] == second_generation["failure_generation"]
    assert persisted["cooldown_until"] == second_generation["cooldown_until"]
    assert persisted["last_run_id"] == "run-t2"


def test_claim_admit_invoke_states_are_truthful_and_attempts_append_only(tmp_path):
    db_path = tmp_path / "truthful-lifecycle.sqlite3"
    store = Store(str(db_path))
    project = store.create_project(
        "owner-a", "Truthful lifecycle", "Prove admission states", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Truthful worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    queued = store.create_run(
        worker["worker_id"], project["project_id"], "Run only after admission"
    )

    claimed = store.claim_next_queued_run(worker["worker_id"])

    assert claimed["state"] == "claimed"
    assert claimed["started_at"] is None
    assert claimed["runtime_invoked_at"] is None
    attempts = store.list_run_attempts(queued["run_id"])
    assert [(item["attempt_number"], item["state"]) for item in attempts] == [
        (1, "claimed")
    ]
    assert attempts[0]["claimed_at"]
    assert attempts[0]["admitted_at"] is None

    assert store.admit_claimed_run(
        queued["run_id"], lease_id="missing", executor_id="executor-a"
    ) is None
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=queued["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    admitted = store.admit_claimed_run(
        queued["run_id"],
        lease_id=lease["lease_id"],
        executor_id="executor-a",
    )

    assert admitted["state"] == "admitted"
    assert admitted["started_at"] is None
    assert admitted["runtime_invoked_at"] is None
    assert store.mark_run_runtime_invoked(
        queued["run_id"], lease_id="missing", executor_id="executor-a"
    ) is None

    running = store.mark_run_runtime_invoked(
        queued["run_id"],
        lease_id=lease["lease_id"],
        executor_id="executor-a",
    )

    assert running["state"] == "running"
    assert running["started_at"]
    assert running["runtime_invoked_at"] == running["started_at"]
    first_started_at = running["started_at"]
    first_attempt = store.list_run_attempts(queued["run_id"])[0]
    assert first_attempt["state"] == "running"
    assert first_attempt["lease_id"] == lease["lease_id"]
    assert first_attempt["runtime_invoked_at"] == running["runtime_invoked_at"]

    store.release_host_run_lease(
        lease["lease_id"], executor_id="executor-a", reason="synthetic_retry"
    )
    requeued = store.requeue_run_for_retry(
        queued["run_id"],
        retry_after="2000-01-01T00:00:00+00:00",
        error_text="Synthetic retry",
        last_retry_class="provider_temporarily_unavailable",
        expected_attempt_id=first_attempt["attempt_id"],
        expected_lease_id=lease["lease_id"],
        expected_executor_id="executor-a",
        expected_startup_token=lease["startup_token"],
    )
    closed_first_attempt = dict(store.list_run_attempts(queued["run_id"])[0])
    reclaimed = store.claim_next_queued_run(worker["worker_id"])
    attempts = store.list_run_attempts(queued["run_id"])

    assert requeued["state"] == "queued"
    assert reclaimed["state"] == "claimed"
    assert [item["attempt_number"] for item in attempts] == [1, 2]
    assert attempts[0]["state"] == "retry_queued"
    assert attempts[0]["ended_at"]
    assert attempts[1]["state"] == "claimed"
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
    assert attempts[0] == closed_first_attempt
    second_lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=queued["run_id"],
        executor_id="executor-b",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    second_admitted = store.admit_claimed_run(
        queued["run_id"],
        lease_id=second_lease["lease_id"],
        executor_id="executor-b",
    )
    second_running = store.mark_run_runtime_invoked(
        queued["run_id"],
        lease_id=second_lease["lease_id"],
        executor_id="executor-b",
    )
    final_attempts = store.list_run_attempts(queued["run_id"])

    assert second_admitted is not None and second_running is not None
    assert second_running["started_at"] == first_started_at
    assert second_running["runtime_invoked_at"] != first_attempt["runtime_invoked_at"]
    assert final_attempts[0] == closed_first_attempt
    assert final_attempts[1]["runtime_invoked_at"] == second_running["runtime_invoked_at"]
    reopened_attempts = Store(str(db_path)).list_run_attempts(queued["run_id"])
    assert [item["attempt_id"] for item in reopened_attempts] == [
        item["attempt_id"] for item in attempts
    ]


def test_admission_rejects_and_reconciles_exactly_expired_active_lease(
    tmp_path, monkeypatch
):
    expired_at = "2000-01-01T00:00:00+00:00"
    monkeypatch.setattr(store_module, "utc_now", lambda: expired_at)
    store = Store(str(tmp_path / "expired-admission-lease.sqlite3"))
    worker, run = _running_host_run(store, "expired-admission")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="expired-admission-owner",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET expires_at = ? WHERE lease_id = ?",
            (expired_at, lease["lease_id"]),
        )

    assert store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="expired-admission-owner",
    ) is None
    service = object.__new__(WorkersProjectsService)
    service.store = store
    service.runtime = StubRuntime()
    service._scheduler_wake_event = Event()
    result = service.reconcile_host_run_leases(stale_after_s=3600)

    assert result["released"] == 1
    assert result["renewed"] == 0
    assert result["unchanged"] == 0
    assert store.get_host_run_lease(lease["lease_id"])["status"] == "released"
    assert store.get_run(run["run_id"])["state"] == "queued"


def test_runtime_invocation_rejects_exactly_expired_active_lease(
    tmp_path, monkeypatch
):
    expired_at = "2000-01-01T00:00:00+00:00"
    monkeypatch.setattr(store_module, "utc_now", lambda: expired_at)
    store = Store(str(tmp_path / "expired-invocation-lease.sqlite3"))
    worker, run = _running_host_run(store, "expired-invocation")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="expired-invocation-owner",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    admitted = store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="expired-invocation-owner",
    )
    assert admitted is not None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET expires_at = ? WHERE lease_id = ?",
            (expired_at, lease["lease_id"]),
        )

    assert store.mark_run_runtime_invoked(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="expired-invocation-owner",
    ) is None
    durable = store.get_run(run["run_id"])
    assert durable["state"] == "admitted"
    assert durable["runtime_invoked_at"] is None
    assert store.list_run_attempts(run["run_id"])[0]["runtime_invoked_at"] is None


def test_concurrent_admission_reserves_capacity_once_and_reports_shortage(tmp_path):
    store = Store(str(tmp_path / "quantitative-capacity.sqlite3"))
    subjects = []
    for index in range(2):
        project = store.create_project(
            f"owner-{index}", f"Project {index}", "Capacity", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id=f"owner-{index}",
            name=f"Worker {index}",
            role="worker",
            profile="codex-cli",
            backend="codex-cli",
            runtime="codex-cli",
            model="test",
            execution_mode="docker",
        )
        run = store.create_run(
            worker["worker_id"], project["project_id"], f"Run {index}"
        )
        subjects.append((worker, store.claim_next_queued_run(worker["worker_id"])))

    available = {
        "childProcesses": 64,
        "threads": 2048,
        "memoryBytes": 6 * 1024**3,
        "diskBytes": 12 * 1024**3,
    }
    required = {
        "childProcesses": 1,
        "threads": 1,
        "memoryBytes": 2 * 1024**3,
        "diskBytes": 4 * 1024**3,
    }
    reservation = {
        "childProcesses": 20,
        "threads": 512,
        "memoryBytes": 3 * 1024**3,
        "diskBytes": 4 * 1024**3,
    }
    start = Barrier(2)

    def reserve(index):
        worker, run = subjects[index]
        start.wait(timeout=2)
        try:
            return store.acquire_host_run_lease(
                runtime_family="codex",
                lane="mission",
                tenant_id="local",
                owner_id=worker["owner_id"],
                worker_id=worker["worker_id"],
                run_id=run["run_id"],
                executor_id=f"executor-{index}",
                conversation_limit=4,
                mission_limit=4,
                account_mission_limit=4,
                tenant_mission_limit=12,
                lease_ttl_s=30,
                capacity_available=available,
                capacity_required=required,
                capacity_reservation=reservation,
                capacity_observed_lease_ids=[],
                capacity_next_retry_at="2026-08-22T12:00:05+00:00",
            )
        except HostRunLeaseCapacityError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, range(2)))

    leases = [result for result in results if isinstance(result, dict)]
    blocked = [
        result
        for result in results
        if isinstance(result, HostRunLeaseCapacityError)
    ]
    assert len(leases) == 1
    assert leases[0]["reserved_memory_bytes"] == reservation["memoryBytes"]
    assert len(blocked) == 1
    assert blocked[0].capacity_class == "resource_pressure"
    assert blocked[0].available["memoryBytes"] == 3 * 1024**3
    assert blocked[0].required["memoryBytes"] == 5 * 1024**3
    assert blocked[0].shortage["memoryBytes"] == 2 * 1024**3
    assert blocked[0].reservation == reservation
    assert blocked[0].next_retry_at == "2026-08-22T12:00:05+00:00"
    assert len(store.list_active_host_run_leases()) == 1


def test_worker_processor_enters_running_only_at_runtime_invocation(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "processor-lifecycle.sqlite3"))

    class ObservingRuntime(StubRuntime):
        def __init__(self):
            self.observed = None

        def run_task(
            self,
            worker,
            instruction,
            timeout_sec=None,
            run_id=None,
        ):
            super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)
            durable = store.get_run(str(run_id)) or {}
            lease = store.get_active_host_run_lease_for_run(str(run_id)) or {}
            self.observed = {
                "state": durable.get("state"),
                "runtime_invoked_at": durable.get("runtime_invoked_at"),
                "lease_status": lease.get("status"),
                "lease_attempt_id": lease.get("attempt_id"),
                "active_attempt_id": durable.get("active_attempt_id"),
            }
            return f"STUB_OK: {instruction}"

    runtime = ObservingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
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
    project = store.create_project(
        "owner-a", "Processor lifecycle", "Prove invocation boundary", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Processor worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = service.assign_run(
        worker["worker_id"], "Observe the exact invocation boundary", start_processor=False
    )
    generation = 1
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation

    try:
        service._process_worker_queue(worker["worker_id"], generation)
    finally:
        service.shutdown()

    assert runtime.observed == {
        "state": "running",
        "runtime_invoked_at": runtime.observed["runtime_invoked_at"],
        "lease_status": "active",
        "lease_attempt_id": runtime.observed["active_attempt_id"],
        "active_attempt_id": runtime.observed["active_attempt_id"],
    }
    assert runtime.observed["runtime_invoked_at"]
    terminal = store.get_run(run["run_id"])
    assert terminal["state"] == "completed"
    attempts = store.list_run_attempts(run["run_id"])
    assert len(attempts) == 1
    assert attempts[0]["state"] == "completed"
    assert attempts[0]["ended_at"]


def test_runtime_return_keeps_exact_lease_until_terminal_cas(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "runtime-return-terminal-cas.sqlite3"))

    class BearerCaptureRuntime(StubRuntime):
        def __init__(self):
            self.bearers = []

        def run_task(
            self,
            worker,
            instruction,
            timeout_sec=None,
            run_id=None,
        ):
            super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)
            bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
            self.bearers.append(
                str(
                    (bundle.get("env") or {}).get(
                        "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
                    )
                    or ""
                )
            )
            return "One exact provider attempt completed."

    runtime = BearerCaptureRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
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
    project = store.create_project(
        "owner-a", "Runtime return fence", "Keep one exact bearer", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Conversation worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
        trusted_run_lane="conversation",
        bootstrap_bundle={
            "run_mode": "conversation",
            "env": {},
            "glasshive_capability_broker": {
                "authority_kind": "conversation_orchestrator",
                "allowed_host_tools": ["active_work"],
            },
        },
    )
    store.update_worker_state(worker["worker_id"], "ready")
    bearer = "synthetic-single-use-runtime-return-bearer"
    run = service.assign_run(
        worker["worker_id"],
        "Use the exact transient capability once",
        start_processor=False,
        run_local_bundle={
            "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": bearer}
        },
    )
    observed_gap = []
    original_release = service._release_host_run_lease

    def release_then_reconcile(run_id, *, reason):
        original_release(run_id, reason=reason)
        if reason == "runtime_returned":
            observed_gap.append(
                {
                    "reconciled": store.reconcile_invalid_running_runs(),
                    "run": store.get_run(run_id),
                    "lease": store.get_active_host_run_lease_for_run(run_id),
                }
            )

    monkeypatch.setattr(service, "_release_host_run_lease", release_then_reconcile)
    generation = 1
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation

    try:
        service._process_worker_queue(worker["worker_id"], generation)
    finally:
        service.shutdown()

    assert len(observed_gap) == 1
    assert observed_gap[0]["reconciled"] == 0
    assert observed_gap[0]["run"]["state"] == "running"
    assert observed_gap[0]["lease"]["status"] == "active"
    terminal = store.get_run(run["run_id"])
    assert terminal["state"] == "completed"
    assert terminal["retry_attempts"] == 0
    attempts = store.list_run_attempts(run["run_id"])
    assert [(attempt["attempt_number"], attempt["state"]) for attempt in attempts] == [
        (1, "completed")
    ]
    assert runtime.bearers == [bearer]
    assert store.get_active_host_run_lease_for_run(run["run_id"]) is None


def test_expired_prelaunch_lease_cas_prevents_any_runtime_dispatch(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "prelaunch-cas.sqlite3"))

    class LaunchCountingRuntime(StubRuntime):
        requires_run_start_identity = True

        def __init__(self):
            self.launches = 0
            self.start_observer = None

        def set_run_start_observer(self, observer):
            self.start_observer = observer

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            self.launches += 1
            raise AssertionError("runtime dispatch must remain fenced")

    runtime = LaunchCountingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
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
    project = store.create_project(
        "owner-a", "Prelaunch fence", "Do not dispatch after lease expiry", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Prelaunch worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = service.assign_run(
        worker["worker_id"], "Prove the prelaunch CAS", start_processor=False
    )
    original_validate = store.validate_host_run_start_reservation

    def expire_after_validation(**kwargs):
        reservation = original_validate(**kwargs)
        assert reservation is not None
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with store._connect() as conn:
            conn.execute(
                "UPDATE host_run_leases SET expires_at = ? WHERE lease_id = ?",
                (expired, kwargs["lease_id"]),
            )
        return reservation

    monkeypatch.setattr(store, "validate_host_run_start_reservation", expire_after_validation)
    generation = 1
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation

    try:
        service._process_worker_queue(worker["worker_id"], generation)
    finally:
        service.shutdown()

    assert runtime.launches == 0
    durable = store.get_run(run["run_id"])
    assert durable is not None
    assert durable["state"] != "running"
    assert durable["runtime_invoked_at"] is None


def test_disk_headroom_is_a_structured_capacity_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_HOST_MAX_CHILD_PROCESSES", "64")
    monkeypatch.setenv("WPR_HOST_MAX_THREADS", "2048")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "2048")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "4096")
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=2,
            threads=32,
            available_memory_bytes=8 * 1024**3,
            available_disk_bytes=1024**3,
        ),
    )
    try:
        error = service._host_resource_capacity_error()
    finally:
        service.shutdown()

    assert isinstance(error, HostCapacityError)
    assert error.code == "host_capacity"
    assert error.capacity_class == "resource_pressure"
    assert error.available["diskBytes"] == 1024**3
    assert error.required["diskBytes"] == 4 * 1024**3
    assert error.shortage["diskBytes"] == 3 * 1024**3


@pytest.mark.parametrize(
    "failed_probe",
    ["process_probe_ok", "memory_probe_ok", "disk_probe_ok"],
)
def test_unknown_host_resource_probe_queues_fail_closed_and_recovers(
    tmp_path, monkeypatch, failed_probe
):
    store = Store(str(tmp_path / f"runtime-{failed_probe}.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    healthy = {
        "child_processes": 0,
        "threads": 0,
        "available_memory_bytes": 16 * 1024**3,
        "available_disk_bytes": 32 * 1024**3,
        "process_probe_ok": True,
        "memory_probe_ok": True,
        "disk_probe_ok": True,
    }
    unavailable = {**healthy, failed_probe: False}
    readings = iter((HostResourceUsage(**unavailable), HostResourceUsage(**healthy)))
    monkeypatch.setattr(service_module, "host_resource_usage", lambda _leases: next(readings))
    try:
        blocked = service._host_resource_capacity_error()
        recovered = service._host_resource_capacity_error()
    finally:
        service.shutdown()

    assert isinstance(blocked, HostCapacityError)
    assert blocked.code == "host_capacity"
    assert blocked.capacity_class == "resource_probe_unavailable"
    assert recovered is None


def test_host_resource_probe_errors_are_never_fabricated_as_infinite_capacity(
    monkeypatch,
):
    monkeypatch.setattr(
        service_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("probe unavailable")),
    )
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("disk probe unavailable")),
    )

    usage = REAL_HOST_RESOURCE_USAGE([{"pid": 4242}])

    assert usage.process_probe_ok is False
    assert usage.memory_probe_ok is False
    assert usage.disk_probe_ok is False
    assert usage.available_memory_bytes == 0
    assert usage.available_disk_bytes == 0


def test_host_resource_probe_counts_macos_process_trees_without_unsupported_thcount(
    monkeypatch,
):
    def fake_run(command, **_kwargs):
        if command == ["ps", "-axo", "pid=,ppid="]:
            return service_module.subprocess.CompletedProcess(
                command, 0, "100 1\n101 100\n200 1\n", ""
            )
        if command == ["ps", "-M", "-p", "100,101"]:
            return service_module.subprocess.CompletedProcess(
                command,
                0,
                "USER PID COMMAND\nuser 100 root\n 100 thread\nuser 101 child\n",
                "",
            )
        if command == ["sysctl", "-n", "hw.memsize"]:
            return service_module.subprocess.CompletedProcess(
                command, 0, str(16 * 1024**3), ""
            )
        if command == ["vm_stat"]:
            return service_module.subprocess.CompletedProcess(
                command,
                0,
                "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                "Pages free: 1000000.\nPages inactive: 1000000.\n"
                "Pages speculative: 1000000.\n",
                "",
            )
        return service_module.subprocess.CompletedProcess(command, 1, "", "unsupported")

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: service_module.shutil._ntuple_diskusage(100, 20, 80),
    )

    usage = REAL_HOST_RESOURCE_USAGE([{"pid": 100}])

    assert usage.process_probe_ok is True
    assert usage.child_processes == 2
    assert usage.threads == 3


def test_conversation_executor_uses_the_typed_account_capacity_limit(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_CONVERSATION_EXECUTOR_WORKERS", raising=False)
    monkeypatch.setenv("WPR_HOST_ACCOUNT_ACTIVE_LIMIT", "3")
    service = WorkersProjectsService(
        Store(str(tmp_path / "conversation-slots.sqlite3")),
        StubRuntime(),
        max_workers=8,
        reconcile_on_startup=False,
    )
    try:
        assert service.conversation_executor._max_workers == 3
    finally:
        service.shutdown()


def test_host_capacity_policy_has_one_configured_three_four_source_of_truth(
    tmp_path, monkeypatch
):
    for name in (
        "WPR_HOST_CONVERSATION_SLOTS_PER_CLI",
        "WPR_HOST_MISSION_SLOTS_PER_CLI",
        "WPR_HOST_ACCOUNT_ACTIVE_LIMIT",
        "WPR_HOST_TENANT_ACTIVE_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    service = WorkersProjectsService(
        Store(str(tmp_path / "capacity-policy.sqlite3")),
        StubRuntime(),
        max_workers=8,
        reconcile_on_startup=False,
    )
    try:
        assert service._host_capacity_policy() == {
            "conversation_limit": 2,
            "mission_limit": 3,
            "account_mission_limit": 4,
            "tenant_mission_limit": 12,
        }
        assert service.conversation_executor._max_workers == 4
    finally:
        service.shutdown()


def test_shared_target_repository_mutation_scope_serializes_host_missions(tmp_path):
    store = Store(str(tmp_path / "leases.sqlite3"))
    _first_project, first_worker, first_run = _active_worker_and_run(
        store,
        "shared-target-first",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    _second_project, second_worker, second_run = _active_worker_and_run(
        store,
        "shared-target-second",
        tenant_id="tenant-a",
        owner_id="owner-b",
    )
    target = tmp_path / "shared-repository"
    target.mkdir()
    mutation_scope = __import__("hashlib").sha256(
        f"repo:{target.resolve()}".encode("utf-8")
    ).hexdigest()

    first = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=first_worker["worker_id"],
        run_id=first_run["run_id"],
        executor_id="executor-first",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        mutation_scope=mutation_scope,
        lease_ttl_s=30,
    )
    with pytest.raises(HostRunLeaseCapacityError) as blocked:
        store.acquire_host_run_lease(
            runtime_family="claude",
            lane="mission",
            tenant_id="tenant-a",
            owner_id="owner-b",
            worker_id=second_worker["worker_id"],
            run_id=second_run["run_id"],
            executor_id="executor-second",
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            mutation_scope=mutation_scope,
            lease_ttl_s=30,
        )
    assert blocked.value.capacity_class == "mutation_scope"

    store.release_host_run_lease(
        first["lease_id"], executor_id="executor-first", reason="run_terminal"
    )
    second = store.acquire_host_run_lease(
        runtime_family="claude",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-b",
        worker_id=second_worker["worker_id"],
        run_id=second_run["run_id"],
        executor_id="executor-second",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        mutation_scope=mutation_scope,
        lease_ttl_s=30,
    )
    assert second["mutation_scope"] == mutation_scope


def test_model_authored_mutation_scopes_cannot_bypass_conservative_serialization(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    target = tmp_path / "target-repository"
    target.mkdir()
    base = {
        "worker_id": "wrk-structured-target",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "scratch"),
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "mission", "target_repository_root": str(target)}
        ),
    }
    try:
        first = service._host_mutation_scope(
            {**base, "name": "Research without mutation words"}
        )
        second = service._host_mutation_scope(
            {**base, "name": "EDIT DELETE COMMIT MUTATE"}
        )
        unscoped_first = service._host_mutation_scope(
            {
                **base,
                "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
                "name": "EDIT DELETE COMMIT MUTATE",
            }
        )
        unscoped_second = service._host_mutation_scope(
            {
                **base,
                "worker_id": "wrk-other-unscoped",
                "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
                "name": "Read-only sounding words cannot weaken the guard",
            }
        )
    finally:
        service.shutdown()

    forged_explicit = service._host_mutation_scope(
        {
            **base,
            "worker_id": "wrk-forged-explicit",
            "bootstrap_bundle_json": json.dumps(
                {"run_mode": "mission", "host_mutation_scope": "unique-forged-scope"}
            ),
        }
    )

    # No caller currently provides authenticated target provenance. Both
    # structured fields are therefore untrusted model/bootstrap data and may
    # not buy a separate mutation lane.
    assert first == second == unscoped_first == unscoped_second == forged_explicit
    assert unscoped_first


@pytest.mark.parametrize(
    ("failure_class", "error"),
    [
        ("host_worker_busy", RuntimeErrorBase("host worker busy")),
        (
            "provider_rate_limited",
            ProviderRateLimitError("provider throttled", retry_after_s=0.1),
        ),
    ],
)
def test_structural_capacity_waits_remain_queued_beyond_retry_budget(
    tmp_path, monkeypatch, failure_class, error
):
    monkeypatch.setenv("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_RETRY_BASE_DELAY_S", "0.1")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "0.1")
    # This unit test drives every retry generation itself. Prevent the real
    # scheduler thread from racing that synthetic processor for the same row.
    monkeypatch.setattr(
        WorkersProjectsService,
        "process_due_worker_retries_once",
        lambda self: {"started": 0, "requeued": 0, "failed": 0},
    )
    store = Store(str(tmp_path / f"indefinite-{failure_class}.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = store.create_project("owner-a", "Capacity", "Wait", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Capacity worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Wait durably"
    )
    fields = {
        "failure_class": failure_class,
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "Waiting for structural capacity.",
        "failure_recommended_recovery": "Wait.",
        "failure_diagnostic_summary": "Synthetic persistent capacity wait.",
    }
    try:
        for attempt_number in range(8):
            current = store.get_run(run["run_id"])
            if current["state"] == "queued":
                current = _invoke_run_attempt(
                    store,
                    worker,
                    current,
                    suffix=f"{failure_class}-{attempt_number}",
                )
            service._requeue_retryable_run(
                worker, current, error, failure_fields=fields
            )
        durable = store.get_run(run["run_id"])
    finally:
        service.shutdown()

    assert durable["state"] == "queued"
    assert durable["retry_attempts"] == 0
    assert durable["failure_retryable"] == 1
    assert not [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.failed"
    ]


def _create_due_retry_worker(
    store: Store,
    *,
    suffix: str,
    execution_mode: str,
    retry_class: str,
) -> str:
    project = store.create_project(
        "owner-a",
        f"Retry project {suffix}",
        "Refresh only the capacity snapshot required by this retry.",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name=f"Retry worker {suffix}",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], f"Retry {suffix}"
    )
    store.update_run(
        run["run_id"],
        retry_after=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        last_retry_class=retry_class,
    )
    return str(worker["worker_id"])


def test_due_docker_capacity_retry_refreshes_snapshot_before_cached_admission(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService,
        "_process_scheduler_cycle",
        lambda _self: None,
    )
    store = Store(str(tmp_path / "capacity-refresh.sqlite3"))
    runtime = StubRuntime()
    snapshot = {"healthy": False}
    probe_calls: list[bool] = []

    def isolated_resource_usage(*, cached_only=False):
        probe_calls.append(cached_only)
        if not cached_only:
            snapshot["healthy"] = True
        return {
            "process_probe_ok": snapshot["healthy"],
            "memory_probe_ok": snapshot["healthy"],
            "disk_probe_ok": snapshot["healthy"],
        }

    runtime.isolated_resource_usage = isolated_resource_usage  # type: ignore[attr-defined]
    due_worker_ids = [
        _create_due_retry_worker(
            store,
            suffix=str(index),
            execution_mode="docker",
            retry_class="host_capacity",
        )
        for index in range(2)
    ]
    service = WorkersProjectsService(
        store, runtime, max_workers=1, reconcile_on_startup=False
    )
    dispatched: list[str] = []

    def dispatch(worker_id):
        assert runtime.isolated_resource_usage(cached_only=True)[
            "memory_probe_ok"
        ] is True
        dispatched.append(worker_id)

    service._ensure_worker_processor = dispatch  # type: ignore[method-assign]
    try:
        processed = service.process_due_worker_retries_once(limit=100)
    finally:
        service.shutdown()

    assert processed == due_worker_ids
    assert dispatched == due_worker_ids
    assert probe_calls == [False, True, True]


def test_packaged_due_retry_uses_readiness_loop_snapshot_without_second_probe(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService,
        "_process_scheduler_cycle",
        lambda _self: None,
    )
    store = Store(str(tmp_path / "packaged-capacity-retry.sqlite3"))
    runtime = StubRuntime()
    worker_id = _create_due_retry_worker(
        store,
        suffix="packaged",
        execution_mode="docker",
        retry_class="host_capacity",
    )
    service = WorkersProjectsService(
        store, runtime, max_workers=1, reconcile_on_startup=False
    )
    dispatched = []
    service._ensure_worker_processor = dispatched.append  # type: ignore[method-assign]
    runtime.isolated_resource_usage = lambda **_kwargs: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        AssertionError("the packaged retry scheduler must not start a second probe")
    )
    monkeypatch.setattr(
        "workers_projects_runtime.execution_profile.packaged_linux", lambda: True
    )
    try:
        assert service.process_due_worker_retries_once(limit=100) == [worker_id]
    finally:
        service.shutdown()
    assert dispatched == [worker_id]


def test_due_noncapacity_or_host_retry_does_not_probe_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService,
        "_process_scheduler_cycle",
        lambda _self: None,
    )
    store = Store(str(tmp_path / "capacity-refresh-exclusions.sqlite3"))
    runtime = StubRuntime()
    runtime.isolated_resource_usage = lambda **_kwargs: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        AssertionError("unrelated retries must not probe Docker capacity")
    )
    due_worker_ids = [
        _create_due_retry_worker(
            store,
            suffix="provider",
            execution_mode="docker",
            retry_class="provider_rate_limited",
        ),
        _create_due_retry_worker(
            store,
            suffix="host",
            execution_mode="host",
            retry_class="host_capacity",
        ),
    ]
    service = WorkersProjectsService(
        store, runtime, max_workers=1, reconcile_on_startup=False
    )
    dispatched: list[str] = []
    service._ensure_worker_processor = dispatched.append  # type: ignore[method-assign]
    try:
        processed = service.process_due_worker_retries_once(limit=100)
    finally:
        service.shutdown()

    assert processed == due_worker_ids
    assert dispatched == due_worker_ids


def test_failed_due_capacity_refresh_remains_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService,
        "_process_scheduler_cycle",
        lambda _self: None,
    )
    store = Store(str(tmp_path / "capacity-refresh-fail-closed.sqlite3"))
    runtime = StubRuntime()
    probe_calls: list[bool] = []

    def isolated_resource_usage(*, cached_only=False):
        probe_calls.append(cached_only)
        return {
            "process_probe_ok": False,
            "memory_probe_ok": False,
            "disk_probe_ok": False,
        }

    runtime.isolated_resource_usage = isolated_resource_usage  # type: ignore[attr-defined]
    worker_id = _create_due_retry_worker(
        store,
        suffix="unavailable",
        execution_mode="docker",
        retry_class="host_capacity",
    )
    service = WorkersProjectsService(
        store, runtime, max_workers=1, reconcile_on_startup=False
    )
    observed: list[dict[str, object]] = []
    service._ensure_worker_processor = (  # type: ignore[method-assign]
        lambda _worker_id: observed.append(
            runtime.isolated_resource_usage(cached_only=True)
        )
    )
    try:
        assert service.process_due_worker_retries_once(limit=100) == [worker_id]
    finally:
        service.shutdown()

    assert probe_calls == [False, True]
    assert observed == [
        {
            "process_probe_ok": False,
            "memory_probe_ok": False,
            "disk_probe_ok": False,
        }
    ]


def test_queue_processor_reserves_capacity_before_attempt_and_releases_retained_compute(
    tmp_path,
    monkeypatch,
):
    class CapacityReleaseRuntime(StubRuntime):
        def __init__(self):
            self.compute_terminations = 0

        def terminate_worker(self, worker):
            self.compute_terminations += 1
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "preclaim-capacity-wait.sqlite3"))
    runtime = CapacityReleaseRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = store.create_project(
        "owner-a", "Preclaim capacity", "Wait before execution", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Preclaim capacity worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = service.assign_run(
        worker["worker_id"], "Wait without starting", start_processor=False
    )
    admission_calls = 0

    def acquire_or_wait(worker_row, run_row):
        nonlocal admission_calls
        admission_calls += 1
        if admission_calls <= 3:
            error = HostCapacityError(
                "Synthetic host capacity wait.",
                capacity_class=(
                    "resource_pressure" if admission_calls % 2 else "family_lane"
                ),
            )
            error.available = {"memoryBytes": 4 * 1024**3}
            error.required = {"memoryBytes": 5 * 1024**3}
            error.shortage = {"memoryBytes": 1024**3}
            error.reservation = {"memoryBytes": 1024**3}
            raise error
        return store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id="local",
            owner_id="owner-a",
            worker_id=worker_row["worker_id"],
            run_id=run_row["run_id"],
            executor_id=service.executor_id,
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            lease_ttl_s=30,
        )

    monkeypatch.setattr(service, "_acquire_host_run_lease", acquire_or_wait)
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)

    def process_once(generation):
        with service._processors_lock:
            service._active_processors.add(worker["worker_id"])
            service._processor_generations[worker["worker_id"]] = generation
        service._process_worker_queue(worker["worker_id"], generation)

    try:
        for generation in range(1, 4):
            process_once(generation)
            store.update_run(
                run["run_id"], retry_after=datetime.now(timezone.utc).isoformat()
            )

        waiting = store.get_run(run["run_id"])
        assert waiting is not None and waiting["state"] == "queued"
        assert store.list_run_attempts(run["run_id"]) == []
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM capacity_attempts WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone()[0] == 1
        assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
        assert runtime.compute_terminations == 1
        assert store.get_worker(worker["worker_id"])["compute_released_at"]

        process_once(4)
        completed = store.get_run(run["run_id"])
        attempts = store.list_run_attempts(run["run_id"])
    finally:
        service.shutdown()

    assert completed is not None and completed["state"] == "completed"
    assert len(attempts) == 1
    assert attempts[0]["state"] == "completed"


def test_structural_waits_do_not_consume_a_later_execution_retry_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_RETRY_BASE_DELAY_S", "0.01")
    store = Store(str(tmp_path / "wait-then-runtime-retry.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    # This test drives every attempt directly. Stop the background scheduler
    # before creating its queued row so no second processor can claim it.
    service.shutdown()
    project = store.create_project("owner-a", "Retry budget", "Preserve it", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Retry budget worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Wait, then retry"
    )
    capacity_fields = {
        "failure_class": "host_capacity",
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "Waiting for capacity.",
        "failure_recommended_recovery": "Wait.",
        "failure_diagnostic_summary": "Synthetic capacity wait.",
    }
    runtime_fields = {
        **capacity_fields,
        "failure_class": "provider_temporarily_unavailable",
        "failure_diagnostic_summary": "Synthetic ordinary retryable failure.",
    }
    try:
        for attempt_number in range(4):
            current = store.get_run(run["run_id"])
            if current["state"] == "queued":
                current = _invoke_run_attempt(
                    store,
                    worker,
                    current,
                    suffix=f"capacity-{attempt_number}",
                )
            service._requeue_retryable_run(
                worker,
                current,
                RuntimeErrorBase("synthetic capacity wait"),
                failure_fields=capacity_fields,
            )

        after_waits = store.get_run(run["run_id"])
        assert after_waits["retry_attempts"] == 0
        first_execution = _invoke_run_attempt(
            store,
            worker,
            after_waits,
            suffix="first-execution",
        )
        service._requeue_retryable_run(
            worker,
            first_execution,
            RuntimeErrorBase("synthetic execution retry"),
            failure_fields=runtime_fields,
        )
        after_first_execution_failure = store.get_run(run["run_id"])
        assert after_first_execution_failure["state"] == "queued"
        assert after_first_execution_failure["retry_attempts"] == 1

        second_execution = _invoke_run_attempt(
            store,
            worker,
            after_first_execution_failure,
            suffix="second-execution",
        )
        service._requeue_retryable_run(
            worker,
            second_execution,
            RuntimeErrorBase("synthetic execution retry again"),
            failure_fields=runtime_fields,
        )
        exhausted = store.get_run(run["run_id"])
    finally:
        service.shutdown()

    assert exhausted["state"] == "failed"
    assert exhausted["retry_attempts"] == 1
    assert exhausted["failure_retryable"] == 1
    assert exhausted["failure_class"] == "provider_temporarily_unavailable"
    assert "capacity" not in exhausted["failure_user_message"].lower()
    assert exhausted["failure_user_message"] == "This work could not finish. You can retry it."
    assert "after 1 attempts for provider_temporarily_unavailable" in exhausted["failure_diagnostic_summary"]
    # Terminal state owns automatic scheduling, independently of manual Retry.
    for generation in (51, 52):
        runtime = StubRuntime()
        invocations = []
        def unexpected_invocation(*args, **kwargs):
            invocations.append(args)
            raise AssertionError("An exhausted failed run must not restart automatically")
        runtime.run_task = unexpected_invocation
        recovered = WorkersProjectsService(
            store, runtime, reconcile_on_startup=False, start_background_consumers=False
        )
        with recovered._processors_lock:
            recovered._active_processors.add(worker["worker_id"])
            recovered._processor_generations[worker["worker_id"]] = generation
        try:
            recovered._process_worker_queue(worker["worker_id"], generation)
            assert invocations == []
            assert store.get_run(run["run_id"])["state"] == "failed"
            assert store.peek_next_queued_run(worker["worker_id"]) is None
            assert store.get_run(run["run_id"])["retry_attempts"] == 1
        finally:
            recovered.shutdown()


class _ReconcilingRuntime(StubRuntime):
    def __init__(self):
        self.identities: dict[str, dict[str, object]] = {}
        self.observer = None
        # Stop-and-confirm seams: cleanup succeeds only for runs listed in ``cleanable`` and
        # absence is provable only for runs listed in ``absent``. Every call is recorded.
        self.cleanable: set[str] = set()
        self.absent: set[str] = set()
        self.cleanup_calls: list[tuple[str, dict[str, object]]] = []
        self.absence_calls: list[str] = []

    def set_host_process_observer(self, observer):
        self.observer = observer

    def host_process_identity(self, worker: dict, run_id: str):
        return self.identities.get(run_id)

    def cleanup_unconfirmed_run_start(self, worker: dict, run_id: str, lease_identity: dict[str, object]) -> bool:
        self.cleanup_calls.append((run_id, dict(lease_identity)))
        return run_id in self.cleanable

    def host_process_absence(self, worker: dict, run_id: str) -> bool:
        self.absence_calls.append(run_id)
        return run_id in self.absent

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        identity = self.identities.get(str(worker.get("_active_run_id") or "")) or {}
        return RuntimeInfo(
            runtime="codex-cli",
            model="test",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=None,
            workspace_dir=None,
            pid=int(identity.get("pid") or 0) or None,
        )


def _running_host_run(store: Store, suffix: str):
    project = store.create_project(
        "owner-a", f"Project {suffix}", "Goal", "codex-cli", tenant_id="tenant-a"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name=f"Worker {suffix}",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
        tenant_id="tenant-a",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Do it")
    run = store.claim_next_queued_run(worker["worker_id"])
    return worker, run


def test_direct_run_creation_downgrades_unproven_running_state(tmp_path):
    store = Store(str(tmp_path / "direct-running.sqlite3"))
    project = store.create_project(
        "owner-a", "Direct running", "Reject synthetic invocation", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Direct running worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )

    created = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Do not synthesize dispatch",
        state="running",
    )
    durable = store.get_run(created["run_id"])

    assert created["state"] == "queued"
    assert durable is not None and durable["state"] == "queued"
    assert durable["started_at"] is None
    assert durable["runtime_invoked_at"] is None
    assert durable["active_attempt_id"] == ""


def test_typed_running_snapshot_restoration_uses_real_generation(tmp_path):
    store = Store(str(tmp_path / "trusted-running-restoration.sqlite3"))
    project = store.create_project(
        "owner-a", "Trusted restore", "Restore a durable running snapshot", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Trusted restore worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )

    restored = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Restore through the real durable transition",
        state=RunRestorationState.RUNNING,
    )
    durable = store.get_run(restored["run_id"])
    active = store.get_active_run(worker["worker_id"])

    assert restored["state"] == "running"
    assert durable is not None and durable["state"] == "running"
    assert durable["started_at"]
    assert durable["runtime_invoked_at"]
    assert durable["active_attempt_id"]
    assert active is not None and active["run_id"] == restored["run_id"]


def test_generic_mutations_cannot_corrupt_an_exact_running_generation(tmp_path):
    store = Store(str(tmp_path / "generic-running-mutations.sqlite3"))
    _project, _worker, running = _active_worker_and_run(
        store,
        "generic-running-mutations",
        run_state="running",
    )
    first_invoked_at = running["runtime_invoked_at"]
    first_started_at = running["started_at"]

    assert store.finalize_run_if_state(
        running["run_id"], "running", "running"
    ) is None
    assert store.update_run(
        running["run_id"], started_at="2000-01-01T00:00:00+00:00"
    ) is None
    assert store.transition_run_if_state(
        running["run_id"],
        "running",
        "running",
        started_at="2000-01-01T00:00:00+00:00",
    ) is None
    assert store.transition_run_if_state(
        running["run_id"],
        "running",
        "running",
        runtime_invoked_at=None,
    ) is None

    durable = store.get_run(running["run_id"])
    assert durable["state"] == "running"
    assert durable["started_at"] == first_started_at
    assert durable["runtime_invoked_at"] == first_invoked_at
    assert store.list_run_attempts(running["run_id"])[0]["ended_at"] is None


def test_legacy_running_confirmation_fails_closed_without_exact_attempt(tmp_path):
    store = Store(str(tmp_path / "legacy-confirm.sqlite3"))
    worker, run = _running_host_run(store, "legacy-confirm")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="legacy-executor",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    legacy_started_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE runs
            SET state = 'running', started_at = ?, runtime_invoked_at = NULL,
                active_attempt_id = ''
            WHERE run_id = ?
            """,
            (legacy_started_at, run["run_id"]),
        )

    confirmed = store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=legacy_started_at,
        lease_id=lease["lease_id"],
        startup_token=lease["startup_token"],
        executor_id="legacy-executor",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="legacy-process-start",
        container_id="",
        session_id="legacy-session",
    )

    durable = store.get_run(run["run_id"])
    assert confirmed is None
    assert durable is not None and durable["state"] != "running"
    assert durable["runtime_invoked_at"] is None
    assert durable["started_at"] == legacy_started_at
    assert store.get_host_run_lease(lease["lease_id"])["startup_state"] != "confirmed"


def test_startup_reconcile_downgrades_running_without_invocation_or_lease(tmp_path, monkeypatch):
    # Startup recovery runs on its own thread: it requeues the uninvoked run and may then
    # start a processor for it. Hold that start so the requeued state is observed once
    # recovery has finished, instead of racing a legitimate re-dispatch.
    monkeypatch.setattr(WorkersProjectsService, "_ensure_worker_processor",
                        lambda _self, _worker_id: None)
    db_path = tmp_path / "startup-running.sqlite3"
    store = Store(str(db_path))
    worker, run = _running_host_run(store, "startup-invalid")
    first_started_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE runs
            SET state = 'running', started_at = ?, runtime_invoked_at = NULL,
                active_attempt_id = ''
            WHERE run_id = ?
            """,
            (first_started_at, run["run_id"]),
        )

    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=True)
    try:
        service._startup_recovery_thread.join(timeout=30)
        assert not service._startup_recovery_thread.is_alive()
        reconciled = store.get_run(run["run_id"])
    finally:
        service.shutdown()

    assert reconciled is not None and reconciled["state"] == "queued"
    assert reconciled["runtime_invoked_at"] is None
    assert reconciled["active_attempt_id"] == ""
    assert reconciled["started_at"] == first_started_at


def _admit_and_invoke(store: Store, run: dict, lease: dict) -> dict:
    admitted = store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=lease["executor_id"],
    )
    assert admitted is not None
    invoked = store.mark_run_runtime_invoked(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=lease["executor_id"],
    )
    assert invoked is not None
    return invoked


def test_runtime_invocation_rejects_attempt_bound_to_another_lease(tmp_path):
    store = Store(str(tmp_path / "runtime-invocation-lease-mismatch.sqlite3"))
    worker, run = _running_host_run(store, "runtime-invocation-lease-mismatch")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    admitted = store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="executor-a",
    )
    assert admitted is not None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE run_attempts SET lease_id = ? WHERE attempt_id = ?",
            ("hrl_corrupt_other_generation", admitted["active_attempt_id"]),
        )

    invoked = store.mark_run_runtime_invoked(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="executor-a",
    )

    durable = store.get_run(run["run_id"])
    attempt = store.list_run_attempts(run["run_id"])[0]
    assert invoked is None
    assert durable is not None and durable["state"] == "admitted"
    assert durable["runtime_invoked_at"] is None
    assert attempt["state"] == "admitted"
    assert attempt["runtime_invoked_at"] is None


def test_startup_confirmation_rejects_attempt_bound_to_another_lease(tmp_path):
    store = Store(str(tmp_path / "startup-confirmation-lease-mismatch.sqlite3"))
    worker, run = _running_host_run(store, "startup-confirmation-lease-mismatch")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    admitted = store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="executor-a",
    )
    assert admitted is not None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE run_attempts SET lease_id = ? WHERE attempt_id = ?",
            ("hrl_corrupt_other_generation", admitted["active_attempt_id"]),
        )

    confirmed = store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at="",
        lease_id=lease["lease_id"],
        startup_token=lease["startup_token"],
        executor_id="executor-a",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-start-identity",
        container_id="",
        session_id="host-run-startup",
        allow_runtime_invocation=True,
    )

    durable = store.get_run(run["run_id"])
    attempt = store.list_run_attempts(run["run_id"])[0]
    assert confirmed is None
    assert durable is not None and durable["state"] == "admitted"
    assert durable["runtime_invoked_at"] is None
    assert attempt["state"] == "admitted"
    assert attempt["runtime_invoked_at"] is None
    assert store.get_host_run_lease(lease["lease_id"])["startup_state"] == "reserved"


def test_every_admitted_transition_rejects_attempt_lease_mismatch(tmp_path):
    store = Store(str(tmp_path / "admission-lease-mismatch.sqlite3"))
    worker, run = _running_host_run(store, "admission-lease-mismatch")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE run_attempts SET lease_id = ? WHERE attempt_id = ?",
            ("hrl_corrupt_other_generation", run["active_attempt_id"]),
        )

    assert store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="executor-a",
    ) is None
    assert store.transition_run_if_state(
        run["run_id"],
        "claimed",
        "admitted",
        admitted_at=datetime.now(timezone.utc).isoformat(),
    ) is None
    durable = store.get_run(run["run_id"])
    attempt = store.get_run_attempt(run["active_attempt_id"])
    assert durable is not None and durable["state"] == "claimed"
    assert attempt is not None and attempt["state"] == "claimed"


def test_host_lease_startup_confirmation_is_exact_durable_and_idempotent(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    worker, run = _running_host_run(store, "startup-confirm")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    assert lease["startup_token"]
    assert lease["startup_state"] == "reserved"
    assert lease["startup_confirmed_at"] is None
    run = _admit_and_invoke(store, run, lease)

    confirmed = store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=str(run["started_at"]),
        lease_id=lease["lease_id"],
        startup_token=lease["startup_token"],
        executor_id="executor-a",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-start-identity",
        container_id="",
        session_id="host-run-startup",
    )
    assert confirmed is not None
    assert confirmed["lease"]["startup_state"] == "confirmed"
    assert confirmed["lease"]["startup_confirmed_at"]
    assert confirmed["event"]["event_type"] == "run.started"
    replay = store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=str(run["started_at"]),
        lease_id=lease["lease_id"],
        startup_token=lease["startup_token"],
        executor_id="executor-a",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-start-identity",
        container_id="",
        session_id="host-run-startup",
    )
    assert replay is not None and replay["idempotent_replay"] is True
    started_events = [
        item
        for item in store.list_events(worker["worker_id"])
        if item["event_type"] == "run.started"
        and item["run_id"] == run["run_id"]
    ]
    assert len(started_events) == 1
    assert store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=str(run["started_at"]),
        lease_id=lease["lease_id"],
        startup_token="stale-startup-token",
        executor_id="executor-a",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-start-identity",
        container_id="",
        session_id="host-run-startup",
    ) is None


@pytest.mark.parametrize(
    ("requires_identity", "execution_mode", "identity_kind"),
    [
        (True, "host", "in_process"),
        (True, "docker", "host_process"),
        (False, "docker", "docker_session"),
    ],
)
def test_run_start_observer_rejects_runtime_mode_identity_grade_mismatch_before_store(
    tmp_path,
    monkeypatch,
    requires_identity,
    execution_mode,
    identity_kind,
):
    class IdentityGradeRuntime(StubRuntime):
        requires_run_start_identity = requires_identity

    monkeypatch.setattr(
        WorkersProjectsService,
        "_process_scheduler_cycle",
        lambda _service: None,
    )
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(
        store,
        IdentityGradeRuntime(),
        reconcile_on_startup=False,
    )
    project = store.create_project(
        "owner-a",
        "Identity grade mismatch",
        "Reject a lower or cross-substrate startup identity",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Identity grade worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Start with the exact permitted identity grade",
        state="running",
    )
    store.update_worker_state(worker["worker_id"], "running")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id=service._executor_id,
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    with service._pending_run_starts_lock:
        service._pending_run_starts[run["run_id"]] = {
            "run_id": run["run_id"],
            "run_started_at": run["started_at"],
            "worker_id": worker["worker_id"],
            "worker": worker,
            "lease_id": lease["lease_id"],
            "startup_token": lease["startup_token"],
        }
    if identity_kind == "in_process":
        reported_identity = {
            "pid": 0,
            "process_group": 0,
            "process_start_identity": "",
            "container_id": "",
            "session_id": "in-process",
        }
    elif identity_kind == "host_process":
        reported_identity = {
            "pid": 4242,
            "process_group": 4242,
            "process_start_identity": "ps-lstart:cross-grade-host",
            "container_id": "",
            "session_id": "cross-grade-host",
        }
    else:
        reported_identity = {
            "pid": 4242,
            "process_group": 4242,
            "process_start_identity": (
                f"docker:cross-grade-container:cross-grade-session:{run['run_id']}:4242"
            ),
            "container_id": "cross-grade-container",
            "session_id": "cross-grade-session",
        }
    try:
        with pytest.raises(RunStartupRejectedError, match="identity grade"):
            service._observe_run_start(
                {
                    "worker_id": worker["worker_id"],
                    "run_id": run["run_id"],
                    "identity_kind": identity_kind,
                    **reported_identity,
                }
            )
        durable_lease = store.get_host_run_lease(lease["lease_id"]) or {}
        assert durable_lease["startup_state"] == "reserved"
        assert not [
            event
            for event in store.list_events(worker["worker_id"])
            if event["event_type"] == "run.started"
        ]
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("pid", "start_identity"),
    [
        (0, "docker:container-a:session-a:run-placeholder:42"),
        (42, ""),
        (42, "docker:container-b:session-a:run-placeholder:42"),
    ],
)
def test_docker_startup_confirmation_requires_generation_bound_identity(
    tmp_path, pid, start_identity
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    worker, run = _running_host_run(store, f"docker-start-{pid}")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    identity = start_identity.replace("run-placeholder", run["run_id"])

    with pytest.raises(ValueError, match="exact identity"):
        store.confirm_host_run_start(
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            run_started_at=str(run["started_at"]),
            lease_id=lease["lease_id"],
            startup_token=lease["startup_token"],
            executor_id="executor-a",
            identity_kind="docker_session",
            pid=pid,
            process_group=pid,
            process_start_identity=identity,
            container_id="container-a",
            session_id="session-a",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"process_group": 999},
        {"pid": 999},
        {"process_start_identity": "unexpected-start"},
        {"container_id": "unexpected-container"},
        {"session_id": "unexpected-session"},
    ],
)
def test_in_process_startup_confirmation_requires_the_complete_exact_tuple(
    tmp_path, overrides
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    worker, run = _running_host_run(store, "in-process-exact-tuple")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    identity = {
        "identity_kind": "in_process",
        "pid": 0,
        "process_group": 0,
        "process_start_identity": "",
        "container_id": "",
        "session_id": "in-process",
        **overrides,
    }

    with pytest.raises(ValueError, match="exact identity"):
        store.confirm_host_run_start(
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            run_started_at=str(run["started_at"]),
            lease_id=lease["lease_id"],
            startup_token=lease["startup_token"],
            executor_id="executor-a",
            **identity,
        )
    assert (store.get_host_run_lease(lease["lease_id"]) or {})[
        "startup_state"
    ] == "reserved"
    assert not [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.started"
    ]


def test_compute_release_claim_fences_concurrent_queue_until_token_finalize(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    other_store = Store(str(tmp_path / "runtime.sqlite3"))
    project = store.create_project("owner-a", "Release fence", "Goal", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Release fence worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    terminal = store.create_run(
        worker["worker_id"], project["project_id"], "Finished", state="completed"
    )
    store.update_worker(worker["worker_id"], state="completed", last_run_id=terminal["run_id"])
    snapshot = store.get_worker(worker["worker_id"])

    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=snapshot["updated_at"],
        expected_last_run_id=terminal["run_id"],
        expected_state="completed",
        expected_container_id="container-a",
        owner="reaper-a",
        ttl_s=300,
    )
    assert claim is not None
    queued = other_store.create_run(
        worker["worker_id"], project["project_id"], "Concurrent follow-up"
    )

    assert other_store.claim_next_queued_run(worker["worker_id"]) is None
    assert store.finalize_worker_compute_release(
        worker["worker_id"],
        str(claim["token"]),
        int(claim["epoch"]),
        expected_kind="idle",
        compute_released_at="2026-08-13T00:00:00+00:00",
        runtime_fields={"runtime": "codex-cli", "pid": None},
        idle_state="completed",
    ) is not None
    claimed = other_store.claim_next_queued_run(worker["worker_id"])
    assert claimed is not None
    assert claimed["run_id"] == queued["run_id"]


def test_compute_release_finalize_token_mismatch_cannot_clear_newer_claim(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    project = store.create_project("owner-a", "Release CAS", "Goal", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Release CAS worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    snapshot = store.get_worker(worker["worker_id"])
    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=snapshot["updated_at"],
        expected_last_run_id="",
        expected_state=str(snapshot["state"]),
        expected_container_id="container-a",
        owner="reaper-new",
        ttl_s=300,
    )
    assert claim is not None

    assert store.finalize_worker_compute_release(
        worker["worker_id"],
        "obsolete-token",
        int(claim["epoch"]),
        expected_kind="idle",
        compute_released_at="2026-08-13T00:00:00+00:00",
        runtime_fields={"runtime": "codex-cli", "pid": None},
        idle_state=str(snapshot["state"]),
    ) is None
    refreshed = store.get_worker(worker["worker_id"])
    assert refreshed["compute_release_token"] == claim["token"]
    assert refreshed["compute_released_at"] is None


def test_stale_lease_reconciliation_keeps_verified_process_and_releases_dead_owner(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _ReconcilingRuntime()
    live_worker, live_run = _running_host_run(store, "live")
    dead_worker, dead_run = _running_host_run(store, "dead")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    live_lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=live_worker["worker_id"],
        run_id=live_run["run_id"],
        executor_id="dead-executor-live",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    dead_lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=dead_worker["worker_id"],
        run_id=dead_run["run_id"],
        executor_id="dead-executor-dead",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    for lease, worker, run, pid, identity, session_id in (
        (
            live_lease,
            live_worker,
            live_run,
            777,
            "ps-lstart:live",
            "host-live",
        ),
        (
            dead_lease,
            dead_worker,
            dead_run,
            778,
            "ps-lstart:dead",
            "host-dead",
        ),
    ):
        run = _admit_and_invoke(store, run, lease)
        assert store.confirm_host_run_start(
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            run_started_at=str(run["started_at"]),
            lease_id=lease["lease_id"],
            startup_token=lease["startup_token"],
            executor_id=lease["executor_id"],
            identity_kind="host_process",
            pid=pid,
            process_group=pid,
            process_start_identity=identity,
            container_id="",
            session_id=session_id,
        )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ? WHERE lease_id IN (?, ?)",
            (old.isoformat(), live_lease["lease_id"], dead_lease["lease_id"]),
        )
    runtime.identities[live_run["run_id"]] = {
        "pid": 777,
        "process_group": 777,
        "process_start_identity": "ps-lstart:live",
        "verified": True,
    }
    # The dead owner's recorded generation is proven absent by exact cleanup before release.
    runtime.cleanable.add(dead_run["run_id"])
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 1, "released": 1, "unchanged": 0}
    assert [call[0] for call in runtime.cleanup_calls] == [dead_run["run_id"]]
    assert runtime.cleanup_calls[0][1]["process_start_identity"] == "ps-lstart:dead"
    assert store.get_host_run_lease(live_lease["lease_id"])["status"] == "active"
    assert store.get_host_run_lease(live_lease["lease_id"])["process_start_identity"] == "ps-lstart:live"
    assert store.get_host_run_lease(dead_lease["lease_id"])["status"] == "released"


def test_stale_unconfirmed_start_lease_remains_fenced_without_death_proof(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _ReconcilingRuntime()
    worker, run = _running_host_run(store, "unconfirmed-start")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    admitted = store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=lease["executor_id"],
    )
    assert admitted and admitted["state"] == "admitted"
    assert store.mark_host_run_start_termination_unconfirmed(
        lease_id=lease["lease_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        startup_token=lease["startup_token"],
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ? WHERE lease_id = ?",
            (old.isoformat(), lease["lease_id"]),
        )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 0, "unchanged": 1}
    retained = store.get_host_run_lease(lease["lease_id"]) or {}
    assert retained["status"] == "active"
    assert retained["startup_state"] == "termination_unconfirmed"
    assert store.has_unconfirmed_host_run_start(worker["worker_id"]) is True


def test_stale_unconfirmed_start_lease_releases_only_with_exact_absence_proof(tmp_path):
    class ConfirmedAbsentRuntime(_ReconcilingRuntime):
        def host_process_absence(self, worker, run_id):
            return worker["worker_id"].startswith("wrk_") and bool(run_id)

    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = ConfirmedAbsentRuntime()
    worker, run = _running_host_run(store, "unconfirmed-start-absent")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    admitted = store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=lease["executor_id"],
    )
    assert admitted and admitted["state"] == "admitted"
    assert store.mark_host_run_start_termination_unconfirmed(
        lease_id=lease["lease_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        startup_token=lease["startup_token"],
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ? WHERE lease_id = ?",
            (old.isoformat(), lease["lease_id"]),
        )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 1, "unchanged": 0}
    released = store.get_host_run_lease(lease["lease_id"]) or {}
    assert released["status"] == "released"
    assert released["release_reason"] == "startup_generation_cleaned"
    assert store.has_unconfirmed_host_run_start(worker["worker_id"]) is False
    assert (store.get_run(run["run_id"]) or {})["state"] == "queued"


class _ReservedStartupCrashRuntime(StubRuntime):
    """Synthetic durable-session reader for the reserve/publish/confirm crash window."""

    def __init__(self) -> None:
        self.identities: dict[str, dict[str, object]] = {}
        self.cleanup_calls: list[tuple[str, str, dict[str, object]]] = []
        self.cleanup_confirmed = True

    def host_process_identity(self, worker: dict, run_id: str):
        return self.identities.get(run_id)

    def cleanup_unconfirmed_run_start(
        self, worker: dict, run_id: str, lease_identity: dict[str, object]
    ) -> bool:
        self.cleanup_calls.append(
            (str(worker["worker_id"]), str(run_id), dict(lease_identity))
        )
        return self.cleanup_confirmed

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        identity = self.identities.get(str(worker.get("_active_run_id") or "")) or {}
        return RuntimeInfo(
            runtime="codex-cli",
            model="test",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=None,
            workspace_dir=None,
            pid=int(identity.get("pid") or 0) or None,
        )


def _reserved_startup_crash_fixture(
    tmp_path,
    *,
    execution_mode: str,
    captured_identity: dict[str, object],
):
    store = Store(str(tmp_path / f"{execution_mode}-startup-crash.sqlite3"))
    project = store.create_project(
        "owner-a", f"{execution_mode} startup crash", "Recover exact start", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name=f"{execution_mode} crash worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.invalid/glasshive",
                "conversation_id": "conv-startup-crash",
                "parent_message_id": "msg-user",
                "message_id": "msg-assistant",
            }
        },
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Recover me")
    run = store.claim_next_queued_run(worker["worker_id"])
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    run = _admit_and_invoke(store, run, lease)
    identity = dict(captured_identity)
    identity["process_start_identity"] = str(
        identity.get("process_start_identity") or ""
    ).replace("{run_id}", str(run["run_id"]))
    store.heartbeat_host_run_lease(
        lease["lease_id"],
        executor_id="dead-executor",
        pid=int(identity.get("pid") or 0) or None,
        process_group=int(identity.get("process_group") or 0) or None,
        process_start_identity=str(identity["process_start_identity"]),
        startup_identity_kind=str(identity.get("identity_kind") or ""),
        startup_container_id=str(identity.get("container_id") or ""),
        startup_session_id=str(identity.get("session_id") or ""),
        lease_ttl_s=30,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ? WHERE lease_id = ?",
            (old.isoformat(), lease["lease_id"]),
        )
    identity["startup_token_digest"] = hashlib.sha256(
        str(lease["startup_token"]).encode("utf-8")
    ).hexdigest()
    return store, store.get_worker(worker["worker_id"]), run, store.get_host_run_lease(
        lease["lease_id"]
    ), identity


def test_reserved_restart_reconstructs_file_published_generation_before_lease_observer(
    tmp_path,
):
    store, worker, run, lease, identity = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode="host",
        captured_identity={
            "identity_kind": "host_process",
            "pid": 951,
            "process_group": 951,
            "process_start_identity": "ps-lstart:file-published",
            "container_id": "",
            "session_id": "host-file-published",
            "verified": True,
        },
    )
    # Simulate a crash after active_session publication but before its observer
    # could copy the exact generation into the lease row.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET pid = NULL, process_group = NULL, "
            "process_start_identity = '', startup_identity_kind = '', "
            "startup_container_id = '', startup_session_id = '' WHERE lease_id = ?",
            (lease["lease_id"],),
        )
    runtime = _ReservedStartupCrashRuntime()
    runtime.identities[run["run_id"]] = identity
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._deliver_callback_record = lambda *args, **kwargs: None
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 1, "released": 0, "unchanged": 0}
    confirmed = store.get_host_run_lease(lease["lease_id"]) or {}
    assert confirmed["startup_state"] == "confirmed"
    assert confirmed["process_start_identity"] == "ps-lstart:file-published"


def test_reserved_restart_cannot_synthesize_invocation_for_admitted_run(tmp_path):
    store = Store(str(tmp_path / "restart-admitted-no-invocation.sqlite3"))
    project = store.create_project(
        "owner-a", "Restart admitted", "Do not synthesize dispatch", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Restart admitted worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Remain admitted"
    )
    run = store.claim_next_queued_run(worker["worker_id"])
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=300,
    )
    admitted = store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id="dead-executor",
    )
    assert admitted is not None
    identity = {
        "identity_kind": "host_process",
        "pid": 952,
        "process_group": 952,
        "process_start_identity": "ps-lstart:admitted-without-dispatch",
        "container_id": "",
        "session_id": "host-admitted",
        "verified": True,
        "startup_token_digest": hashlib.sha256(
            str(lease["startup_token"]).encode("utf-8")
        ).hexdigest(),
    }
    store.heartbeat_host_run_lease(
        lease["lease_id"],
        executor_id="dead-executor",
        pid=identity["pid"],
        process_group=identity["process_group"],
        process_start_identity=identity["process_start_identity"],
        startup_identity_kind=identity["identity_kind"],
        startup_session_id=identity["session_id"],
        lease_ttl_s=300,
    )
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ? WHERE lease_id = ?",
            (old, lease["lease_id"]),
        )

    runtime = _ReservedStartupCrashRuntime()
    runtime.identities[run["run_id"]] = identity
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"]) or {}
    attempt = store.list_run_attempts(run["run_id"])[0]
    durable_lease = store.get_host_run_lease(lease["lease_id"]) or {}
    assert result == {"renewed": 0, "released": 0, "unchanged": 1}
    assert durable["state"] == "admitted"
    assert durable["runtime_invoked_at"] is None
    assert attempt["state"] == "admitted"
    assert attempt["runtime_invoked_at"] is None
    assert durable_lease["startup_state"] == "reserved"
    assert not [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.started" and event["run_id"] == run["run_id"]
    ]


@pytest.mark.parametrize(
    ("execution_mode", "captured_identity"),
    [
        (
            "docker",
            {
                "identity_kind": "docker_session",
                "pid": 501,
                "process_group": 501,
                "process_start_identity": "docker:container-old:job-crash:{run_id}:71",
                "container_id": "container-old",
                "session_id": "job-crash",
                "verified": True,
            },
        ),
        (
            "host",
            {
                "identity_kind": "host_process",
                "pid": 601,
                "process_group": 601,
                "process_start_identity": "ps-lstart:synthetic-old-generation",
                "container_id": "",
                "session_id": "host-crash",
                "verified": True,
            },
        ),
    ],
)
def test_reserved_verified_restart_confirms_original_generation_exactly_once(
    tmp_path, execution_mode, captured_identity
):
    store, worker, run, lease, identity = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode=execution_mode,
        captured_identity=captured_identity,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.identities[run["run_id"]] = identity
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._deliver_callback_record = lambda *args, **kwargs: None
    try:
        first = service.reconcile_host_run_leases(stale_after_s=0)
        second = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    confirmed = store.get_host_run_lease(lease["lease_id"]) or {}
    assert first == {"renewed": 1, "released": 0, "unchanged": 0}
    assert second == {"renewed": 1, "released": 0, "unchanged": 0}
    assert confirmed["startup_state"] == "confirmed"
    assert confirmed["startup_token"] == lease["startup_token"]
    assert confirmed["startup_identity_kind"] == identity["identity_kind"]
    assert confirmed["startup_container_id"] == identity["container_id"]
    assert confirmed["startup_session_id"] == identity["session_id"]
    started = [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.started" and event["run_id"] == run["run_id"]
    ]
    assert len(started) == 1
    with sqlite3.connect(store.db_path) as conn:
        callbacks = conn.execute(
            "SELECT callback_id, event_type, payload_json FROM callback_outbox "
            "WHERE run_id = ? AND event_type = 'run.started'",
            (run["run_id"],),
        ).fetchall()
    assert len(callbacks) == 1
    payload = json.loads(callbacks[0][2])
    assert payload["event"] == "run.started"
    assert payload["run_id"] == run["run_id"]
    assert "startup_token" not in payload
    assert len(callbacks[0][2].encode("utf-8")) < 16_384


@pytest.mark.parametrize(
    ("execution_mode", "captured", "replacement"),
    [
        (
            "host",
            {
                "identity_kind": "host_process",
                "pid": 701,
                "process_group": 701,
                "process_start_identity": "ps-lstart:captured-old-generation",
                "container_id": "",
                "session_id": "host-old",
                "verified": True,
            },
            {
                "pid": 702,
                "process_group": 702,
                "process_start_identity": "ps-lstart:replacement-generation",
                "session_id": "host-replacement",
            },
        ),
        (
            "docker",
            {
                "identity_kind": "docker_session",
                "pid": 801,
                "process_group": 801,
                "process_start_identity": "docker:container-old:job-old:{run_id}:81",
                "container_id": "container-old",
                "session_id": "job-old",
                "verified": True,
            },
            {
                "pid": 802,
                "process_group": 802,
                "process_start_identity": "docker:container-new:job-new:{run_id}:82",
                "container_id": "container-new",
                "session_id": "job-new",
            },
        ),
    ],
)
def test_reserved_restart_rejects_replacement_generation_and_requeues_after_exact_cleanup(
    tmp_path, execution_mode, captured, replacement
):
    store, worker, run, lease, captured = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode=execution_mode,
        captured_identity=captured,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.identities[run["run_id"]] = {
        **captured,
        **{
            key: (
                str(value).replace("{run_id}", str(run["run_id"]))
                if isinstance(value, str)
                else value
            )
            for key, value in replacement.items()
        },
    }
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._deliver_callback_record = lambda *args, **kwargs: None
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 1, "unchanged": 0}
    assert runtime.cleanup_calls == [(worker["worker_id"], run["run_id"], {
        key: captured[key]
        for key in (
            "identity_kind",
            "pid",
            "process_group",
            "process_start_identity",
            "container_id",
            "session_id",
        )
    })]
    assert (store.get_host_run_lease(lease["lease_id"]) or {})["status"] == "released"
    recovered_run = store.get_run(run["run_id"]) or {}
    assert recovered_run["state"] == "queued"
    assert recovered_run["failure_class"] == "service_startup_fenced"
    assert not [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.started" and event["run_id"] == run["run_id"]
    ]


def test_reserved_restart_cleanup_uncertainty_keeps_durable_start_fence(tmp_path):
    captured = {
        "identity_kind": "host_process",
        "pid": 901,
        "process_group": 901,
        "process_start_identity": "ps-lstart:captured-uncertain",
        "container_id": "",
        "session_id": "host-uncertain",
        "verified": True,
    }
    store, worker, run, lease, captured = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode="host",
        captured_identity=captured,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.cleanup_confirmed = False
    runtime.identities[run["run_id"]] = {
        **captured,
        "pid": 902,
        "process_group": 902,
        "process_start_identity": "ps-lstart:replacement-uncertain",
        "session_id": "host-replacement",
    }
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 0, "unchanged": 1}
    retained = store.get_host_run_lease(lease["lease_id"]) or {}
    assert retained["status"] == "active"
    assert retained["startup_state"] == "termination_unconfirmed"
    assert (store.get_run(run["run_id"]) or {})["state"] == "settling"


def test_termination_unconfirmed_retries_exact_cleanup_and_requeues_once_proven(
    tmp_path,
):
    captured = {
        "identity_kind": "host_process",
        "pid": 911,
        "process_group": 911,
        "process_start_identity": "ps-lstart:captured-retry-cleanup",
        "container_id": "",
        "session_id": "host-retry-cleanup",
        "verified": True,
    }
    store, worker, run, lease, captured = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode="host",
        captured_identity=captured,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.cleanup_confirmed = False
    runtime.identities[run["run_id"]] = {
        **captured,
        "pid": 912,
        "process_group": 912,
        "process_start_identity": "ps-lstart:replacement-retry-cleanup",
        "session_id": "host-replacement-retry-cleanup",
    }
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        first = service.reconcile_host_run_leases(stale_after_s=0)
        assert first == {"renewed": 0, "released": 0, "unchanged": 1}
        assert (store.get_host_run_lease(lease["lease_id"]) or {})[
            "startup_state"
        ] == "termination_unconfirmed"

        runtime.cleanup_confirmed = True
        second = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert second == {"renewed": 0, "released": 1, "unchanged": 0}
    released = store.get_host_run_lease(lease["lease_id"]) or {}
    assert released["status"] == "released"
    assert released["release_reason"] == "startup_generation_cleaned"
    recovered = store.get_run(run["run_id"]) or {}
    assert recovered["state"] == "queued"
    assert recovered["failure_class"] == "service_startup_fenced"
    cleanup_identity = {
        key: captured[key]
        for key in (
            "identity_kind",
            "pid",
            "process_group",
            "process_start_identity",
            "container_id",
            "session_id",
        )
    }
    assert runtime.cleanup_calls == [
        (worker["worker_id"], run["run_id"], cleanup_identity),
        (worker["worker_id"], run["run_id"], cleanup_identity),
    ]


def test_managed_shutdown_stops_the_live_generation_without_finalizing_the_run(tmp_path, monkeypatch):
    """One generation, one external effect: shutdown releases the lease, stops the live native
    process of the running run, and the old generation's interrupted exit leaves the run running
    for the restarted service to requeue instead of becoming a terminal state or a callback."""
    import threading

    from workers_projects_runtime.openclaw_runtime import (
        WorkerInterruptedError,
        notify_runtime_started,
        runtime_start_boundary,
    )

    store = Store(str(tmp_path / "managed-shutdown-generation.sqlite3"))
    started = threading.Event()
    release = threading.Event()

    class LiveGenerationRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.stopped: list[tuple[str, str | None]] = []

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
            started.set()
            release.wait(timeout=10)
            raise WorkerInterruptedError("native process stopped by managed shutdown")

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            return info.__class__(**{**info.__dict__, "pid": 4242 if started.is_set() and not release.is_set() else None})

        def host_process_absence(self, worker, run_id):
            return release.is_set()

        def cleanup_unconfirmed_run_start(self, worker, run_id, identity):
            self.interrupt_worker(worker, run_id=run_id)
            return self.host_process_absence(worker, run_id)

        def interrupt_worker(self, worker, run_id=None):
            self.stopped.append((str(worker["worker_id"]), run_id))
            release.set()
            return super().pause_worker(worker)

    runtime = LiveGenerationRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
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
    project = store.create_project("owner-a", "Generation seam", "Prove one generation", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Generation worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = service.assign_run(worker["worker_id"], "Do durable work.", start_processor=False)
    assert run is not None
    generation = 1
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation
    processor = threading.Thread(
        target=service._process_worker_queue, args=(worker["worker_id"], generation), daemon=True
    )
    processor.start()
    assert started.wait(timeout=10)
    lease = store.get_active_host_run_lease_for_run(str(run["run_id"]))
    assert lease is not None and lease["status"] == "active"
    service._executor_id = str(lease["executor_id"])
    service.shutdown()
    processor.join(timeout=10)
    assert not processor.is_alive()
    assert runtime.stopped == [(str(worker["worker_id"]), str(run["run_id"]))]
    released = store.get_host_run_lease(str(lease["lease_id"]))
    assert released["status"] == "released"
    assert released["release_reason"] == "managed_shutdown"
    durable = store.get_run(str(run["run_id"]))
    assert durable["state"] == "running"
    events = [event["event_type"] for event in store.list_events(str(worker["worker_id"]))]
    assert "run.interrupted" not in events
    assert "run.failed" not in events


def _queued_docker_run(store: Store, service: WorkersProjectsService, suffix: str):
    project = store.create_project("owner-a", f"Project {suffix}", f"Goal {suffix}", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name=f"Worker {suffix}",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = service.assign_run(worker["worker_id"], "Do durable work.", start_processor=False)
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = 1
    return str(worker["worker_id"]), str(run["run_id"])


def test_superseded_processor_lost_claim_keeps_the_live_generation_lease(
    tmp_path, monkeypatch
):
    """A processor superseded between its startup reservation and its claim drops only
    that unclaimed reservation. The next generation of the same executor may already
    run the work under the adopted lease; stripping that fence would leave managed
    shutdown unable to stop the generation or record managed_shutdown for it."""
    import threading

    from workers_projects_runtime.openclaw_runtime import (
        WorkerInterruptedError,
        notify_runtime_started,
        runtime_start_boundary,
    )

    store = Store(str(tmp_path / "superseded-preclaim.sqlite3"))
    started = threading.Event()
    release = threading.Event()

    class LiveGenerationRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.stopped: list[tuple[str, str | None]] = []

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
            started.set()
            release.wait(timeout=10)
            raise WorkerInterruptedError("native process stopped by managed shutdown")

        def host_process_absence(self, worker, run_id):
            return release.is_set()

        def cleanup_unconfirmed_run_start(self, worker, run_id, identity):
            self.interrupt_worker(worker, run_id=run_id)
            return self.host_process_absence(worker, run_id)

        def interrupt_worker(self, worker, run_id=None):
            self.stopped.append((str(worker["worker_id"]), run_id))
            if run_id:
                release.set()
            return super().pause_worker(worker)

    runtime = LiveGenerationRuntime()
    service = WorkersProjectsService(
        store, runtime, reconcile_on_startup=False, start_background_consumers=False
    )
    service._emit_callback = lambda *_args, **_kwargs: None
    worker_id, run_id = _queued_docker_run(store, service, "superseded-preclaim")
    reservations: list[dict] = []
    superseded: list[object] = []
    original_claim = store.claim_next_queued_run

    def superseded_before_claim(claim_worker_id, **kwargs):
        if not reservations:
            reservations.append(dict(store.get_active_host_run_lease_for_run(run_id) or {}))
            # An operator interrupt supersedes this processor after its reservation;
            # the scheduler's retry phase dispatches the next generation, which adopts
            # the reservation, claims the run and starts it.
            service.interrupt_worker(claim_worker_id)
            superseded.extend(service.process_due_worker_retries_once())
            superseded.append(started.wait(timeout=10))
        return original_claim(claim_worker_id, **kwargs)

    monkeypatch.setattr(store, "claim_next_queued_run", superseded_before_claim)
    try:
        service._process_worker_queue(worker_id, 1)
        assert superseded == [worker_id, True]
        reservation = reservations[0]
        assert (reservation["startup_state"], reservation["attempt_id"]) == ("reserved", "")
        live = store.get_host_run_lease(str(reservation["lease_id"]))
        assert store.get_run(run_id)["state"] == "running"
        assert (live["status"], live["release_reason"], live["startup_state"]) == (
            "active",
            "",
            "confirmed",
        )
        # The lifespan's first shutdown step still stops and types that generation.
        assert service.release_owned_host_run_leases() == 1
        assert (worker_id, run_id) in runtime.stopped
        released = store.get_host_run_lease(str(reservation["lease_id"]))
        assert (released["status"], released["release_reason"]) == (
            "released",
            "managed_shutdown",
        )
    finally:
        release.set()
        service.shutdown()


def test_lost_preclaim_still_releases_its_own_unclaimed_reservation(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "lost-preclaim.sqlite3"))
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False, start_background_consumers=False
    )
    service._emit_callback = lambda *_args, **_kwargs: None
    worker_id, run_id = _queued_docker_run(store, service, "lost-preclaim")
    reservations: list[dict] = []
    original_claim = store.claim_next_queued_run

    def paused_before_claim(claim_worker_id, **kwargs):
        reservations.append(dict(store.get_active_host_run_lease_for_run(run_id) or {}))
        # No other generation adopts the reservation; only the claim is lost.
        store.update_worker_state(claim_worker_id, "paused")
        return original_claim(claim_worker_id, **kwargs)

    monkeypatch.setattr(store, "claim_next_queued_run", paused_before_claim)
    try:
        service._process_worker_queue(worker_id, 1)
    finally:
        service.shutdown()
    assert (reservations[0]["startup_state"], reservations[0]["attempt_id"]) == ("reserved", "")
    released = store.get_host_run_lease(str(reservations[0]["lease_id"]))
    assert (released["status"], released["release_reason"]) == (
        "released",
        "preclaim_generation_lost",
    )
    assert store.get_run(run_id)["state"] == "queued"


def _stamp_docker_session_identity(store: Store, lease_id: str, *, run_id: str, container_id: str, session_id: str) -> None:
    """Model the identity the docker adapter publishes when its screen session starts."""
    identity = f"docker:{container_id}:{session_id}:{run_id}:4242"
    with store._connect() as conn:
        conn.execute(
            """
            UPDATE host_run_leases
            SET startup_state = 'confirmed', startup_identity_kind = 'docker_session',
                startup_container_id = ?, startup_session_id = ?,
                process_start_identity = ?, pid = 4242, process_group = 4242
            WHERE lease_id = ?
            """,
            (container_id, session_id, identity, lease_id),
        )


def test_managed_shutdown_retains_verified_docker_generation_and_restart_adopts_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_RESTART_SURVIVOR_ADOPTION", "1")
    """Seam: a managed restart must not stop a live docker Worker generation or release its
    lease early. Shutdown detaches only this process's wait, extends the same lease for the
    restart window, and leaves the run running; the restarted service adopts the survivor and
    collects exactly one terminal result, so one generation yields one effect and one
    completion callback."""
    import threading

    from workers_projects_runtime.openclaw_runtime import (
        WorkerDetachedError,
        notify_runtime_started,
        runtime_start_boundary,
    )

    monkeypatch.setenv("WPR_SURVIVOR_MONITOR_INTERVAL_S", "0.02")
    store = Store(str(tmp_path / "managed-restart-adoption.sqlite3"))
    started = threading.Event()
    release = threading.Event()
    container_id = "a" * 64
    session_id = "job-retained"

    class RetainedGenerationRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.stopped: list[tuple[str, str | None]] = []
            self.detached: list[tuple[str, str | None]] = []
            self.cleared_grants: list[str] = []
            self.alive = True
            self.completed = False

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
            started.set()
            release.wait(timeout=10)
            raise WorkerDetachedError("detached for managed restart")

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            return info.__class__(**{**info.__dict__, "pid": 4242 if self.alive else None})

        def host_process_identity(self, worker, run_id):
            if not self.alive:
                return None
            return {
                "identity_kind": "docker_session",
                "pid": 4242,
                "process_group": 4242,
                "process_start_identity": f"docker:{container_id}:{session_id}:{run_id}:4242",
                "container_id": container_id,
                "session_id": session_id,
                "verified": True,
            }

        def detach_worker(self, worker, run_id=None):
            self.detached.append((str(worker["worker_id"]), run_id))
            release.set()
            return True

        def interrupt_worker(self, worker, run_id=None):
            self.stopped.append((str(worker["worker_id"]), run_id))
            release.set()
            return super().pause_worker(worker)

        def clear_run_local_capability_grant(self, worker):
            self.cleared_grants.append(str(worker["worker_id"]))

        def collect_completed_run(self, worker, run_id=None, instruction=""):
            if not self.completed:
                return None
            self.alive = False
            return {"state": "completed", "output_text": "retained generation finished once"}

    runtime = RetainedGenerationRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    first_callbacks: list[str] = []
    service._emit_callback = lambda _worker, event, *args, **kwargs: first_callbacks.append(str(event))
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
    project = store.create_project("owner-a", "Retained generation", "Adopt across restart", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Retained worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = service.assign_run(worker["worker_id"], "Do durable work once.", start_processor=False)
    assert run is not None
    generation = 1
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation
    processor = threading.Thread(
        target=service._process_worker_queue, args=(worker["worker_id"], generation), daemon=True
    )
    processor.start()
    assert started.wait(timeout=10)
    lease = store.get_active_host_run_lease_for_run(str(run["run_id"]))
    assert lease is not None and lease["status"] == "active"
    _stamp_docker_session_identity(
        store, str(lease["lease_id"]), run_id=str(run["run_id"]), container_id=container_id, session_id=session_id
    )
    service._executor_id = str(lease["executor_id"])
    before_shutdown = datetime.now(timezone.utc)
    service.shutdown()
    processor.join(timeout=10)
    assert not processor.is_alive()

    assert runtime.detached == [(str(worker["worker_id"]), str(run["run_id"]))]
    assert runtime.stopped == []
    # The retained generation keeps its run-local provider authority.
    assert runtime.cleared_grants == []
    retained = store.get_host_run_lease(str(lease["lease_id"]))
    assert (retained["status"], retained["release_reason"]) == ("active", "")
    assert retained["release_reason"] in ("", None)
    assert datetime.fromisoformat(str(retained["expires_at"])) >= before_shutdown + timedelta(seconds=120)
    durable = store.get_run(str(run["run_id"]))
    assert durable["state"] == "running"
    events = [event["event_type"] for event in store.list_events(str(worker["worker_id"]))]
    assert "run.generation_retained" in events
    assert "run.interrupted" not in events
    assert "run.failed" not in events
    assert "run.requeued" not in events
    assert "run.completed" not in first_callbacks
    assert "run.interrupted" not in first_callbacks

    restarted = WorkersProjectsService(store, runtime, reconcile_on_startup=True)
    restart_callbacks: list[str] = []
    restarted._emit_callback = lambda _worker, event, *args, **kwargs: restart_callbacks.append(str(event))
    try:
        deadline = time.monotonic() + 2
        while (
            not restarted._local_processor_owns(worker["worker_id"])
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert restarted._local_processor_owns(worker["worker_id"])
        assert store.get_run(str(run["run_id"]))["state"] == "running"
        runtime.completed = True
        deadline = time.monotonic() + 3
        while (
            store.get_run(str(run["run_id"]))["state"] != "completed"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        restarted.shutdown()

    final = store.get_run(str(run["run_id"]))
    assert final["state"] == "completed"
    assert final["output_text"] == "retained generation finished once"
    assert final["run_id"] == run["run_id"]
    events = [event["event_type"] for event in store.list_events(str(worker["worker_id"]))]
    assert events.count("run.started") == 1
    assert events.count("run.completed") == 1
    assert "run.requeued" not in events
    assert "run.interrupted" not in events
    assert restart_callbacks.count("run.completed") == 1
    assert "run.started" not in restart_callbacks
    released = store.get_host_run_lease(str(lease["lease_id"]))
    assert released["status"] == "released"
    # The terminal CAS releases the adopted lease in the same transaction; the survivor
    # monitor's own release is the fallback when a collection lands outside that CAS.
    assert released["release_reason"] in {"run_terminal:completed", "survivor_terminal"}


def test_managed_shutdown_keeps_stop_and_release_without_a_verifiable_generation(tmp_path, monkeypatch):
    """Without a verified docker session identity the shutdown contract is unchanged: the
    generation is stopped and the lease released for restart requeue."""
    import threading

    from workers_projects_runtime.openclaw_runtime import (
        WorkerInterruptedError,
        notify_runtime_started,
        runtime_start_boundary,
    )

    store = Store(str(tmp_path / "managed-shutdown-unverified.sqlite3"))
    started = threading.Event()
    release = threading.Event()

    class UnverifiableRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.stopped: list[tuple[str, str | None]] = []
            self.detached: list[tuple[str, str | None]] = []

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
            started.set()
            release.wait(timeout=10)
            raise WorkerInterruptedError("stopped by managed shutdown")

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            return info.__class__(**{**info.__dict__, "pid": 4242 if started.is_set() and not release.is_set() else None})

        def host_process_identity(self, worker, run_id):
            return None

        def detach_worker(self, worker, run_id=None):
            self.detached.append((str(worker["worker_id"]), run_id))
            return True

        def host_process_absence(self, worker, run_id):
            return release.is_set()

        def cleanup_unconfirmed_run_start(self, worker, run_id, identity):
            self.interrupt_worker(worker, run_id=run_id)
            return self.host_process_absence(worker, run_id)

        def interrupt_worker(self, worker, run_id=None):
            self.stopped.append((str(worker["worker_id"]), run_id))
            release.set()
            return super().pause_worker(worker)

    runtime = UnverifiableRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
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
    project = store.create_project("owner-a", "Unverified generation", "Stop and release", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Unverified worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = service.assign_run(worker["worker_id"], "Do work.", start_processor=False)
    assert run is not None
    generation = 1
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation
    processor = threading.Thread(
        target=service._process_worker_queue, args=(worker["worker_id"], generation), daemon=True
    )
    processor.start()
    assert started.wait(timeout=10)
    lease = store.get_active_host_run_lease_for_run(str(run["run_id"]))
    assert lease is not None
    _stamp_docker_session_identity(
        store, str(lease["lease_id"]), run_id=str(run["run_id"]), container_id="b" * 64, session_id="job-unverified"
    )
    service._executor_id = str(lease["executor_id"])
    service.shutdown()
    processor.join(timeout=10)
    assert not processor.is_alive()
    assert runtime.detached == []
    assert runtime.stopped == [(str(worker["worker_id"]), str(run["run_id"]))]
    released = store.get_host_run_lease(str(lease["lease_id"]))
    assert released["status"] == "released"
    assert released["release_reason"] == "managed_shutdown"
    assert store.get_run(str(run["run_id"]))["state"] == "running"


def test_stale_lease_collects_file_published_terminal_result_before_declaring_the_owner_dead(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_RESTART_SURVIVOR_ADOPTION", "1")
    """A generation that finished while no executor watched it (a restart gap) leaves its
    terminal transcript behind. Stale-lease reconciliation must collect that single result
    under the still-active lease instead of requeueing the exact run into a duplicate."""

    class FinishedSurvivorRuntime(StubRuntime):
        def host_process_identity(self, worker, run_id):
            return None

        def collect_completed_run(self, worker, run_id=None, instruction=""):
            return {"state": "completed", "output_text": "finished during the restart gap"}

    store = Store(str(tmp_path / "stale-lease-collect.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "stale-collect", run_state="running")
    # The lease expired during the restart gap; terminal evidence still wins over a requeue.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET expires_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", str(run["run_id"])),
        )
    service = WorkersProjectsService(
        store,
        FinishedSurvivorRuntime(),
        reconcile_on_startup=False,
        start_background_consumers=False,
    )
    callbacks: list[str] = []
    service._emit_callback = lambda _worker, event, *args, **kwargs: callbacks.append(str(event))
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()
    assert result["released"] == 1
    final = store.get_run(str(run["run_id"]))
    assert final["state"] == "completed"
    assert final["output_text"] == "finished during the restart gap"
    assert callbacks.count("run.completed") == 1
    assert "run.requeued" not in callbacks
    lease = store.get_active_host_run_lease_for_run(str(run["run_id"]))
    assert lease is None
    events = [event["event_type"] for event in store.list_events(str(worker["worker_id"]))]
    assert "run.completed" in events
    assert "run.requeued" not in events


def _expire_lease(store: Store, run_id: str) -> dict:
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET expires_at = ?, heartbeat_at = ? WHERE run_id = ? AND status = 'active'",
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", run_id),
        )
    lease = store.get_active_host_run_lease_for_run(run_id)
    assert lease is not None
    return lease


class _VerifiedSurvivorRuntime(StubRuntime):
    """Models a docker generation that outlived its executor and its lease."""

    def __init__(self, lease: dict, *, run_id: str, mismatch: bool = False):
        super().__init__()
        self._lease = lease
        self._run_id = run_id
        self._mismatch = mismatch
        self.alive = True
        self.completed = False

    def reconcile_worker(self, worker):
        info = super().reconcile_worker(worker)
        return RuntimeInfo(**{**info.__dict__, "pid": 4242 if self.alive else None})

    def host_process_identity(self, worker, run_id):
        if not self.alive:
            return None
        identity = str(self._lease["process_start_identity"])
        if self._mismatch:
            identity = identity.rsplit(":", 1)[0] + ":replacement"
        return {
            "identity_kind": "docker_session",
            "pid": 4242,
            "process_group": 4242,
            "process_start_identity": identity,
            "container_id": str(self._lease["startup_container_id"]),
            "session_id": str(self._lease["startup_session_id"]),
            "verified": True,
        }

    def collect_completed_run(self, worker, run_id=None, instruction=""):
        if not self.completed:
            return None
        self.alive = False
        return {"state": "completed", "output_text": "survivor finished after the executor died"}


def test_expired_lease_with_verified_matching_survivor_is_revived_and_adopted(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_RESTART_SURVIVOR_ADOPTION", "1")
    """Seam: a launcher restart kills the executor (no graceful shutdown) and outlasts the lease
    TTL while the docker generation keeps running. Startup must revive the exact lease from the
    verified live identity, keep the run running, adopt the survivor, and complete it once,
    instead of downgrading it into a duplicate generation."""
    monkeypatch.setenv("WPR_SURVIVOR_MONITOR_INTERVAL_S", "0.02")
    store = Store(str(tmp_path / "expired-survivor-revive.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "revive", run_state="running")
    lease = _expire_lease(store, str(run["run_id"]))
    assert lease["startup_identity_kind"] == "docker_session"
    runtime = _VerifiedSurvivorRuntime(lease, run_id=str(run["run_id"]))
    service = WorkersProjectsService(
        store, runtime, reconcile_on_startup=False, start_background_consumers=False
    )
    callbacks: list[str] = []
    service._emit_callback = lambda _worker, event, *args, **kwargs: callbacks.append(str(event))
    try:
        result = service.reconcile_host_run_leases(stale_after_s=3600)
        assert result["renewed"] == 1
        assert result["released"] == 0
        revived = store.get_host_run_lease(str(lease["lease_id"]))
        assert revived["status"] == "active"
        assert str(revived["expires_at"]) > store_module.utc_now()
        assert revived["reconciled_at"]
        # The exact generation is provable again: startup does not downgrade the run.
        assert store.reconcile_invalid_running_runs() == 0
        assert store.get_run(str(run["run_id"]))["state"] == "running"

        service.reconcile_all_workers()
        deadline = time.monotonic() + 2
        while not service._local_processor_owns(worker["worker_id"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service._local_processor_owns(worker["worker_id"])
        runtime.completed = True
        deadline = time.monotonic() + 3
        while store.get_run(str(run["run_id"]))["state"] != "completed" and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        service.shutdown()
    final = store.get_run(str(run["run_id"]))
    assert final["state"] == "completed"
    assert final["output_text"] == "survivor finished after the executor died"
    assert final["active_attempt_id"] == run["active_attempt_id"]
    events = [event["event_type"] for event in store.list_events(str(worker["worker_id"]))]
    assert events.count("run.completed") == 1
    assert "run.requeued" not in events
    assert "run.interrupted" not in events
    assert callbacks.count("run.completed") == 1
    assert "run.started" not in callbacks


def test_expired_lease_is_not_revived_for_a_replacement_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_RESTART_SURVIVOR_ADOPTION", "1")
    """A live session whose identity differs from the lease's recorded identity is not the
    fenced generation; the expired lease stays fenced and the run is downgraded as before."""
    store = Store(str(tmp_path / "expired-survivor-mismatch.sqlite3"))
    _project, _worker, run = _active_worker_and_run(store, "mismatch", run_state="running")
    lease = _expire_lease(store, str(run["run_id"]))
    runtime = _VerifiedSurvivorRuntime(lease, run_id=str(run["run_id"]), mismatch=True)
    service = WorkersProjectsService(
        store, runtime, reconcile_on_startup=False, start_background_consumers=False
    )
    try:
        result = service.reconcile_host_run_leases(stale_after_s=3600)
        assert result["renewed"] == 0
        assert result["released"] == 0
        assert result["unchanged"] == 1
        stale = store.get_host_run_lease(str(lease["lease_id"]))
        assert stale["status"] == "active"
        assert str(stale["expires_at"]) < store_module.utc_now()
        assert store.reconcile_invalid_running_runs() == 1
        assert store.get_run(str(run["run_id"]))["state"] == "queued"
    finally:
        service.shutdown()


def test_retained_run_keeps_its_armed_capability_revocation(tmp_path):
    """The processor unwind revokes the run-local grant by explicit run id; a run retained for
    restart must keep its revocation armed so the live generation keeps its provider authority."""
    store = Store(str(tmp_path / "retained-revocation.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "retained-grant", run_state="running")
    revocation = store.enqueue_capability_grant_revocation(
        {
            "authorizationRef": "gha_retained",
            "originRef": "ghi_retained",
            "workRef": "work_retained",
            "workerId": str(worker["worker_id"]),
            "runId": str(run["run_id"]),
            "grantId": "ghcb_retained",
            "containerGenerationId": "c" * 64,
        }
    )
    revocation_id = str(revocation["revocation_id"])
    assert revocation["status"] == "armed"
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False, start_background_consumers=False
    )
    try:
        with service._processors_lock:
            service._retained_restart_run_ids.add(str(run["run_id"]))
        admitted = {**worker, "_run_local_capability_revocation_id": revocation_id}
        def revocation_status() -> str:
            rows = [
                row for row in store.list_capability_grant_revocations()
                if str(row.get("revocation_id") or "") == revocation_id
            ]
            assert len(rows) == 1
            return str(rows[0].get("status") or "")

        service._revoke_run_local_capability_grant(admitted, run_id=str(run["run_id"]))
        assert revocation_status() == "armed"
        # An unrelated run still revokes as before.
        with service._processors_lock:
            service._retained_restart_run_ids.discard(str(run["run_id"]))
        service._revoke_run_local_capability_grant(admitted, run_id=str(run["run_id"]))
        assert revocation_status() != "armed"
    finally:
        service.shutdown()


def test_survivor_adoption_is_off_by_default_so_the_retry_path_owns_restart_recovery(tmp_path, monkeypatch):
    """Default recovery shape: an expired lease with a verified live survivor is not revived; the
    run is downgraded to queued and its retry resumes the same provider session."""
    monkeypatch.delenv("WPR_RESTART_SURVIVOR_ADOPTION", raising=False)
    store = Store(str(tmp_path / "adoption-default-off.sqlite3"))
    _project, _worker, run = _active_worker_and_run(store, "default-off", run_state="running")
    lease = _expire_lease(store, str(run["run_id"]))
    runtime = _VerifiedSurvivorRuntime(lease, run_id=str(run["run_id"]))
    service = WorkersProjectsService(
        store, runtime, reconcile_on_startup=False, start_background_consumers=False
    )
    try:
        result = service.reconcile_host_run_leases(stale_after_s=3600)
        assert result["renewed"] == 0
        assert store.reconcile_invalid_running_runs() == 1
        assert store.get_run(str(run["run_id"]))["state"] == "queued"
    finally:
        service.shutdown()


def test_stale_lease_requeues_only_after_its_generation_is_stopped_and_proven_absent(tmp_path):
    """No duplicate execution: an unverifiable generation keeps its fence until it is proven dead.

    A launcher restart kills the executor before its lease heartbeat expires while the container
    screen session it started keeps working. The stale-lease pass must stop that exact generation
    and prove absence before the run is requeued; ambiguity keeps the run fenced for the next pass.
    Survivor adoption stays off throughout (nothing is collected or revived).
    """

    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _ReconcilingRuntime()
    worker, run = _running_host_run(store, "fence")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-fence",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    run = _admit_and_invoke(store, run, lease)
    assert store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=str(run["started_at"]),
        lease_id=lease["lease_id"],
        startup_token=lease["startup_token"],
        executor_id=lease["executor_id"],
        identity_kind="host_process",
        pid=779,
        process_group=779,
        process_start_identity="ps-lstart:fence",
        container_id="",
        session_id="host-fence",
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
            (old.isoformat(), old.isoformat(), lease["lease_id"]),
        )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        assert service._restart_survivor_adoption_enabled() is False
        # Pass 1: identity unreadable, cleanup cannot confirm, absence unproven -> fenced.
        first = service.reconcile_host_run_leases(stale_after_s=0)
        assert first == {"renewed": 0, "released": 0, "unchanged": 1}
        assert store.get_host_run_lease(lease["lease_id"])["status"] == "active"
        assert store.get_run(run["run_id"])["state"] == "running"
        assert [call[0] for call in runtime.cleanup_calls] == [run["run_id"]]
        assert runtime.cleanup_calls[0][1]["identity_kind"] == "host_process"
        assert runtime.cleanup_calls[0][1]["pid"] == 779
        assert runtime.absence_calls == [run["run_id"]]

        # Pass 2: the exact recorded generation is stopped and proven gone -> requeue.
        runtime.cleanable.add(run["run_id"])
        second = service.reconcile_host_run_leases(stale_after_s=0)
        assert second == {"renewed": 0, "released": 1, "unchanged": 0}
    finally:
        service.shutdown()

    released = store.get_host_run_lease(lease["lease_id"])
    assert released["status"] == "released"
    assert released["release_reason"] == "stale_owner_no_verified_process"
    requeued = store.get_run(run["run_id"])
    assert requeued["state"] == "queued"
    # Retry starts only after the stop was confirmed: the cleanup preceded the release.
    assert [call[0] for call in runtime.cleanup_calls] == [run["run_id"], run["run_id"]]


@pytest.mark.parametrize("evidence", [
    {},
    {"runtime_invoked_at": None},
    {"started_at": None},
    {"runtime_invoked_at": "2026-01-01T00:00:00+00:00", "started_at": None},
    {"runtime_invoked_at": None, "started_at": "2026-01-01T00:00:00+00:00"},
    {"runtime_invoked_at": "", "started_at": None},
])
def test_old_processor_failure_requires_explicit_no_start_evidence(evidence):
    from workers_projects_runtime.failure_classification import is_user_resumable_failure
    assert not is_user_resumable_failure(
        failure_class="service_processor_unexpected", retryable=0, **evidence
    )
    # A stored explicit retryability decision keeps its existing semantics.
    assert is_user_resumable_failure(
        failure_class="service_processor_unexpected", retryable=1, **evidence
    )


@pytest.mark.parametrize("proof", ["unknown", "cleanup_error", "absent"])
def test_managed_shutdown_retains_exact_lease_until_generation_absence_is_proven(tmp_path, proof):
    store = Store(str(tmp_path / "shutdown-proof.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "shutdown-proof", run_state="running")
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    runtime = _ReconcilingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False,
                                     start_background_consumers=False)
    service._executor_id = lease["executor_id"]
    captured = service._reserved_start_identity(lease)
    assert captured is not None
    cleanup = runtime.cleanup_unconfirmed_run_start

    def inspect_fence(target, run_id, identity):
        # A processor unwinding during shutdown must not release the lease before proof.
        assert store.get_host_run_lease(lease["lease_id"])["status"] == "active"
        assert service._released_for_managed_shutdown(run, {"expected_lease_id": lease["lease_id"]})
        service._release_host_run_lease(run_id, reason="processor_exit")
        assert store.get_host_run_lease(lease["lease_id"])["status"] == "active"
        assert identity == captured
        if proof == "cleanup_error":
            raise RuntimeError("Synthetic stop failed")
        return cleanup(target, run_id, identity)

    runtime.cleanup_unconfirmed_run_start = inspect_fence
    if proof == "absent":
        runtime.cleanable.add(run["run_id"])
    try:
        assert service.release_owned_host_run_leases() == (1 if proof == "absent" else 0)
        assert store.get_run(run["run_id"])["state"] == "running"
        current = store.get_host_run_lease(lease["lease_id"])
        assert current["status"] == ("released" if proof == "absent" else "active")
        if proof != "absent":
            assert store.reconcile_invalid_running_runs() == 0
            assert store.get_run(run["run_id"])["active_attempt_id"] == run["active_attempt_id"]
            runtime.cleanup_unconfirmed_run_start = cleanup
            runtime.cleanable.add(run["run_id"])
            assert service.release_owned_host_run_leases() == 1
        assert service.release_owned_host_run_leases() == 0
        assert store.reconcile_invalid_running_runs() == 1
        assert store.get_run(run["run_id"])["state"] == "queued"
    finally:
        service.shutdown()
        store.close()
