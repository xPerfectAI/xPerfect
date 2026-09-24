from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.broker_admission import BrokerAdmissionError
from workers_projects_runtime.local_qa_control import LocalQAControlPlane
from workers_projects_runtime.openclaw_runtime import HostCapacityError, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.signed_links import sign_link_token
from workers_projects_runtime.store import Store


_CANDIDATE_DIGEST = "sha256:" + "1" * 64
_COMPONENT_DIGEST = "sha256:" + "2" * 64


class _WorkspaceStubRuntime(StubRuntime):
    def ensure_worker_ready(self, worker: dict):
        info = super().ensure_worker_ready(worker)
        info.workspace_dir = str(worker.get("workspace_dir") or info.workspace_dir)
        return info

    def reconcile_worker(self, worker: dict):
        return self.ensure_worker_ready(worker)


@pytest.fixture(autouse=True)
def _disable_service_background_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WorkersProjectsService, "_replay_startup_recovery", lambda _self: None)
    monkeypatch.setattr(WorkersProjectsService, "_callback_retry_loop", lambda _self: None)
    monkeypatch.setattr(WorkersProjectsService, "_scheduler_loop", lambda _self: None)
    monkeypatch.setattr(WorkersProjectsService, "_host_lease_heartbeat_loop", lambda _self: None)


def test_all_catalog_boundaries_have_named_real_runtime_callsites() -> None:
    expected = {
        "provider_auth_missing": ("service.py", "_run_local_admitted_worker"),
        "provider_quota_cooldown_fallback": ("service.py", "_handle_unhealthy_provider_route"),
        "provider_unavailable": ("service.py", "_process_worker_queue_parallel"),
        "provider_internal_retry_threshold": ("local_qa_control.py", "_apply_selected_liveness_boundary"),
        "declared_long_fresh_then_stale": ("local_qa_control.py", "_apply_selected_liveness_boundary"),
        "maximum_capacity_overflow": ("service.py", "_acquire_host_run_lease"),
        "measured_memory_4_3_gib_vs_5_gib": ("service.py", "_acquire_host_run_lease"),
        "last_reservation_competition": ("service.py", "_acquire_host_run_lease"),
        "low_disk": ("service.py", "_acquire_host_run_lease"),
        "callback_transport_interruption": ("service.py", "_deliver_claimed_callback_record"),
        "claimed_queue_stall": ("service.py", "_process_worker_queue_parallel"),
        "admitted_queue_stall": ("service.py", "_process_worker_queue_parallel"),
        "status_refresh_timeout_race": ("service.py", "process_queued_work_status_once"),
        "expired_sender_lease_race": ("service.py", "_deliver_claimed_callback_record"),
        "duplicate_callback_replay": ("service.py", "_deliver_claimed_callback_record"),
        "artifact_link_expired": ("service.py", "local_qa_artifact_fault"),
        "artifact_unavailable_restart_recovery": ("api.py", "_serve_artifact"),
    }
    source_root = Path(__file__).resolve().parents[1] / "src" / "workers_projects_runtime"
    found: set[tuple[str, str, str]] = set()
    for file_name in {file_name for file_name, _function in expected.values()}:
        tree = ast.parse((source_root / file_name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            literals = {
                item.value
                for item in ast.walk(node)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
            for boundary in literals & set(expected):
                found.add((file_name, node.name, boundary))
    missing = {
        boundary: f"{file_name}:{function_name}"
        for boundary, (file_name, function_name) in expected.items()
        if (file_name, function_name, boundary) not in found
    }
    assert missing == {}


def _authority(case_id: str) -> dict[str, str]:
    return {
        "VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE": case_id.lower().replace("-", "_"),
        "VIVENTIUM_LOCAL_QA_CASE_ID": case_id,
        "VIVENTIUM_LOCAL_QA_CASE_TOKEN": "qa-token-" + "t" * 40,
        "VIVENTIUM_LOCAL_QA_SESSION_REF": "qa-session-" + "s" * 32,
        "VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST": _CANDIDATE_DIGEST,
        "VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST": _COMPONENT_DIGEST,
    }


def _fixture(
    tmp_path: Path,
    *,
    case_id: str,
    owner_id: str = "qa_owner_runtime_hooks",
) -> tuple[Store, dict, dict, dict, str, LocalQAControlPlane]:
    store = Store(str(tmp_path / "runtime.sqlite3"))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    delegation = store.reserve_delegation(
        tenant_id="local",
        owner_id=owner_id,
        idempotency_key="qa_idem_runtime_hooks",
        request_digest="sha256:" + "3" * 64,
        origin_ref="qa_origin_runtime_hooks",
        title="Synthetic Parallel Work local-QA fixture",
        goal="Exercise one exact installed runtime boundary.",
        instruction="Synthetic local-QA fixture. Do not perform external work.",
        origin_surface="workbench",
        worker_name="Synthetic local-QA worker",
        worker_role="deterministic fixture",
        profile="synthetic",
        backend="synthetic",
        runtime="synthetic",
        model="synthetic",
        execution_mode="docker",
        workspace_root=str(workspace),
        bootstrap_bundle={
            "callbacks": {"events_webhook_url": "https://qa.invalid/callback"}
        },
    )
    worker = store.get_worker(str(delegation["worker_id"]))
    run = store.get_run(str(delegation["initial_run_id"]))
    assert worker is not None and run is not None
    store.update_worker(str(worker["worker_id"]), workspace_dir=str(workspace))
    worker = store.get_worker(str(delegation["worker_id"]))
    assert worker is not None
    artifact = Path(str(worker["workspace_dir"])) / "result.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact.write_text("synthetic artifact", encoding="utf-8")
    artifact.chmod(0o600)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact_id = "artifact_sha256:" + digest
    store.record_artifact_trace(
        run_id=str(run["run_id"]),
        tenant_id="local",
        owner_id=str(worker["owner_id"]),
        artifact_refs={
            "available": True,
            "overflowCount": 0,
            "refs": [
                {
                    "artifactRef": artifact_id,
                    "fingerprint": "sha256:" + digest,
                    "kind": "text",
                    "sizeBytes": artifact.stat().st_size,
                    "state": "ready",
                }
            ],
        },
    )
    plane = LocalQAControlPlane(
        store.db_path,
        environment=_authority(case_id),
    )
    return store, delegation, worker, run, artifact_id, plane


def _arm(
    plane: LocalQAControlPlane,
    *,
    case_id: str,
    boundary: str,
    delegation: dict,
    worker: dict,
    run: dict,
    artifact_id: str = "",
) -> dict[str, object]:
    request = {
        "contractVersion": 1,
        "caseId": case_id,
        "caseToken": _authority(case_id)["VIVENTIUM_LOCAL_QA_CASE_TOKEN"],
        "sessionRef": _authority(case_id)["VIVENTIUM_LOCAL_QA_SESSION_REF"],
        "candidateDigest": _CANDIDATE_DIGEST,
        "componentArtifactDigest": _COMPONENT_DIGEST,
        "scopeKind": "synthetic_local_qa",
        "boundary": boundary,
        "ownerId": str(worker["owner_id"]),
        "workId": str(delegation["work_ref"]),
        "runId": str(run["run_id"]),
        "artifactId": artifact_id,
        "ttlSeconds": 300,
        "parameters": {},
    }
    return plane.arm(request)


def _arm_selected_fixture_boundary(
    plane: LocalQAControlPlane,
    *,
    boundary: str,
    delegation: dict,
    worker: dict,
    run: dict,
) -> dict[str, object]:
    authority = _authority("PWK-UC-016")
    with plane._connect() as connection:
        artifact_trace = connection.execute(
            "SELECT payload_json FROM work_trace_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
    assert artifact_trace is not None
    artifact_id = json.loads(artifact_trace[0])["artifactRefs"]["refs"][0]["artifactRef"]
    request = {
        "contractVersion": 1,
        "caseId": "PWK-UC-016",
        "caseToken": authority["VIVENTIUM_LOCAL_QA_CASE_TOKEN"],
        "sessionRef": authority["VIVENTIUM_LOCAL_QA_SESSION_REF"],
        "candidateDigest": _CANDIDATE_DIGEST,
        "componentArtifactDigest": _COMPONENT_DIGEST,
        "scopeKind": "selected_synthetic_account_qa",
        "boundary": boundary,
        "ownerId": worker["owner_id"],
        "workId": delegation["work_ref"],
        "runId": run["run_id"],
        "artifactId": "",
        "ttlSeconds": 300,
        "parameters": {},
    }
    attested = {
        "artifactId": artifact_id,
        "candidateDigest": _CANDIDATE_DIGEST,
        "caseId": "PWK-UC-016",
        "componentArtifactDigest": _COMPONENT_DIGEST,
        "ownerId": worker["owner_id"],
        "runId": run["run_id"],
        "sessionRef": authority["VIVENTIUM_LOCAL_QA_SESSION_REF"],
        "workId": delegation["work_ref"],
    }
    canonical = json.dumps(attested, sort_keys=True, separators=(",", ":"))
    request["fixtureAttestation"] = "sha256:" + hmac.new(
        authority["VIVENTIUM_LOCAL_QA_CASE_TOKEN"].encode(),
        ("glasshive-fixture-control-v1\0" + canonical).encode(),
        hashlib.sha256,
    ).hexdigest()
    return plane.arm(request)


def test_selected_fixture_liveness_boundaries_create_exact_consumed_attention_evidence(
    tmp_path: Path,
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path,
        case_id="PWK-UC-016",
        owner_id="selected_synthetic_owner",
    )

    retry = _arm_selected_fixture_boundary(
        plane,
        boundary="provider_internal_retry_threshold",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    stalled = store.get_run(str(run["run_id"]))
    released = store.get_worker(str(worker["worker_id"]))
    assert retry["status"] == "consumed"
    assert stalled is not None and stalled["state"] == "needs_input"
    assert stalled["failure_class"] == "provider_progress_stalled"
    assert stalled["internal_retry_count"] == 3
    assert released is not None and released["compute_released_at"]
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_liveness_events WHERE run_id = ? AND kind = 'internal_retry'",
            (run["run_id"],),
        ).fetchone()[0] == 3

    store.transition_run_if_state(str(run["run_id"]), "needs_input", "queued", ended_at=None)
    long = _arm_selected_fixture_boundary(
        plane,
        boundary="declared_long_fresh_then_stale",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    stale = store.get_run(str(run["run_id"]))
    assert long["status"] == "consumed"
    assert stale is not None and stale["state"] == "needs_input"
    assert stale["liveness_mode"] == "declared_long"
    assert stale["meaningful_progress_sequence"] >= 1
    assert stale["failure_class"] == "provider_progress_stalled"


@pytest.mark.parametrize("boundary", [
    "provider_auth_missing",
    "provider_quota_cooldown_fallback",
    "provider_unavailable",
    "maximum_capacity_overflow",
    "measured_memory_4_3_gib_vs_5_gib",
    "last_reservation_competition",
    "low_disk",
])
def test_selected_fixture_degraded_boundaries_are_consumed_without_creating_work(
    tmp_path: Path,
    boundary: str,
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path / boundary,
        case_id="PWK-UC-016",
        owner_id="selected_synthetic_owner",
    )

    result = _arm_selected_fixture_boundary(
        plane,
        boundary=boundary,
        delegation=delegation,
        worker=worker,
        run=run,
    )

    assert result["status"] == "consumed"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def _service(store: Store, plane: LocalQAControlPlane) -> WorkersProjectsService:
    return WorkersProjectsService(
        store,
        StubRuntime(),
        reconcile_on_startup=False,
        local_qa_control_plane=plane,
    )


def _run_processor_sync(service: WorkersProjectsService, worker_id: str) -> None:
    with service._processors_lock:
        generation = service._processor_generations.get(worker_id, 0) + 1
        service._processor_generations[worker_id] = generation
        service._active_processors.add(worker_id)
    service._process_worker_queue(worker_id, generation)


def _queue_callback_record(store: Store, worker: dict, run: dict, callback_id: str) -> dict:
    payload = {
        "callback_id": callback_id,
        "callback_ts": 1_900_000_000,
        "event": "run.queue_status",
        "worker_id": worker["worker_id"],
        "run_id": run["run_id"],
    }
    return store.upsert_callback_outbox(
        callback_id=callback_id,
        project_id=str(worker["project_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        event_type="run.queue_status",
        url="https://qa.invalid/callback",
        payload_json=json.dumps(payload),
    )


_BOUNDARY_CASES = [
    ("PWK-UC-016", "provider_auth_missing", True, False),
    ("PWK-UC-016", "provider_quota_cooldown_fallback", True, False),
    ("PWK-UC-016", "provider_unavailable", True, False),
    ("PWK-UC-016", "provider_internal_retry_threshold", True, False),
    ("PWK-UC-016", "declared_long_fresh_then_stale", True, False),
    ("PWK-UC-016", "maximum_capacity_overflow", True, False),
    ("PWK-UC-016", "measured_memory_4_3_gib_vs_5_gib", True, False),
    ("PWK-UC-016", "last_reservation_competition", True, False),
    ("PWK-UC-016", "low_disk", True, False),
    ("PWK-UC-017", "callback_transport_interruption", True, False),
    ("PWK-UC-017", "claimed_queue_stall", True, False),
    ("PWK-UC-017", "admitted_queue_stall", True, False),
    ("PWK-UC-017", "status_refresh_timeout_race", True, False),
    ("PWK-UC-017", "expired_sender_lease_race", True, False),
    ("PWK-UC-017", "duplicate_callback_replay", True, False),
    ("PWK-UC-017", "artifact_link_expired", True, True),
    ("PWK-UC-017", "artifact_unavailable_restart_recovery", True, True),
]


@pytest.mark.parametrize(
    ("case_id", "boundary", "run_scoped", "artifact_scoped"),
    _BOUNDARY_CASES,
)
def test_every_boundary_wrong_scope_is_noop_and_one_shot_survives_restart(
    tmp_path: Path,
    case_id: str,
    boundary: str,
    run_scoped: bool,
    artifact_scoped: bool,
) -> None:
    store, delegation, worker, run, artifact_id, plane = _fixture(
        tmp_path, case_id=case_id
    )
    _arm(
        plane,
        case_id=case_id,
        boundary=boundary,
        delegation=delegation,
        worker=worker,
        run=run,
        artifact_id=artifact_id if artifact_scoped else "",
    )
    wrong = store.reserve_delegation(
        tenant_id="local",
        owner_id="qa_owner_wrong_scope",
        idempotency_key="qa_idem_wrong_scope",
        request_digest="sha256:" + "4" * 64,
        origin_ref="qa_origin_wrong_scope",
        title="Synthetic Parallel Work local-QA fixture",
        goal="Wrong-scope negative fixture.",
        instruction="Synthetic local-QA fixture. Do not perform external work.",
        origin_surface="workbench",
        worker_name="Wrong-scope worker",
        worker_role="deterministic fixture",
        profile="synthetic",
        backend="synthetic",
        runtime="synthetic",
        model="synthetic",
        execution_mode="docker",
    )
    wrong_worker = store.get_worker(str(wrong["worker_id"]))
    wrong_run = store.get_run(str(wrong["initial_run_id"]))
    assert wrong_worker is not None and wrong_run is not None
    before_trace = store.work_trace_detail(
        run_id=str(run["run_id"]),
        tenant_id="local",
        owner_id=str(worker["owner_id"]),
    )
    service = _service(store, plane)
    try:
        assert service._consume_local_qa(
            boundary,
            wrong_worker,
            wrong_run,
            artifact_id=("artifact_sha256:" + "9" * 64 if artifact_scoped else ""),
        ) is None
    finally:
        service.shutdown()
    reopened = LocalQAControlPlane(store.db_path, environment=_authority(case_id))
    directive = reopened.consume(
        boundary,
        owner_id=str(worker["owner_id"]),
        work_id=str(delegation["work_ref"]),
        run_id=str(run["run_id"]) if run_scoped else "",
        artifact_id=artifact_id if artifact_scoped else "",
    )
    assert directive is not None
    restarted = LocalQAControlPlane(store.db_path, environment=_authority(case_id))
    assert restarted.consume(
        boundary,
        owner_id=str(worker["owner_id"]),
        work_id=str(delegation["work_ref"]),
        run_id=str(run["run_id"]) if run_scoped else "",
        artifact_id=artifact_id if artifact_scoped else "",
    ) is None
    after_trace = store.work_trace_detail(
        run_id=str(run["run_id"]),
        tenant_id="local",
        owner_id=str(worker["owner_id"]),
    )
    assert after_trace == before_trace


def test_every_boundary_rejects_a_different_run_on_the_same_work(
    tmp_path: Path,
) -> None:
    for case_id, boundary, _run_scoped, artifact_scoped in _BOUNDARY_CASES:
        case_root = tmp_path / f"{case_id}-{boundary}"
        store, delegation, worker, run, artifact_id, plane = _fixture(
            case_root, case_id=case_id
        )
        _arm(
            plane,
            case_id=case_id,
            boundary=boundary,
            delegation=delegation,
            worker=worker,
            run=run,
            artifact_id=artifact_id if artifact_scoped else "",
        )
        newer = store.create_run(
            str(worker["worker_id"]),
            str(worker["project_id"]),
            "Synthetic second run for exact-scope rejection.",
        )
        service = _service(store, plane)
        try:
            assert service._consume_local_qa(
                boundary,
                worker,
                newer,
                artifact_id=artifact_id if artifact_scoped else "",
            ) is None
        finally:
            service.shutdown()
        assert plane.consume(
            boundary,
            owner_id=str(worker["owner_id"]),
            work_id=str(delegation["work_ref"]),
            run_id=str(run["run_id"]),
            artifact_id=artifact_id if artifact_scoped else "",
        ) is not None


def test_provider_auth_control_blocks_before_runtime_admission_and_is_one_shot(tmp_path: Path) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-016"
    )
    _arm(
        plane,
        case_id="PWK-UC-016",
        boundary="provider_auth_missing",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    try:
        with pytest.raises(BrokerAdmissionError) as raised:
            service._run_local_admitted_worker(worker, run)
        assert raised.value.code == "provider_auth_missing"
        assert store.get_run(str(run["run_id"]))["state"] == "queued"
        assert service._run_local_admitted_worker(worker, run)["worker_id"] == worker["worker_id"]
    finally:
        service.shutdown()


def test_provider_quota_control_persists_cooldown_then_uses_healthy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-016"
    )
    _arm(
        plane,
        case_id="PWK-UC-016",
        boundary="provider_quota_cooldown_fallback",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    claimed = store.claim_next_queued_run(
        str(worker["worker_id"]), executor_id=service._executor_id
    )
    assert claimed is not None
    monkeypatch.setattr(
        service, "_trusted_parallel_fallback_profile", lambda _worker: "fallback"
    )
    monkeypatch.setattr(
        service, "_resolve_worker_model", lambda _profile, _mode="docker": "fallback-model"
    )
    try:
        assert service._handle_unhealthy_provider_route(worker, claimed) is True
        current = store.get_run(str(run["run_id"]))
        switched = store.get_worker(str(worker["worker_id"]))
        health = store.get_provider_route_health(**service._provider_route(worker))
        assert current is not None and current["state"] == "queued"
        assert switched is not None and switched["profile"] == "fallback"
        assert health is not None and health["failure_class"] == "provider_quota_exhausted"
        assert health["cooldown_until"]
    finally:
        service.shutdown()


def test_provider_unavailable_control_fails_before_adapter_and_reopens_cleanly(
    tmp_path: Path
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-016"
    )
    _arm(
        plane,
        case_id="PWK-UC-016",
        boundary="provider_unavailable",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    try:
        _run_processor_sync(service, str(worker["worker_id"]))
        failed = store.get_run(str(run["run_id"]))
        assert failed is not None and failed["state"] == "failed"
        assert failed["failure_class"] == "provider_unavailable"
        reopened = LocalQAControlPlane(
            store.db_path, environment=_authority("PWK-UC-016")
        )
        assert reopened.consume(
            "provider_unavailable",
            owner_id=str(worker["owner_id"]),
            work_id=str(delegation["work_ref"]),
        ) is None
    finally:
        service.shutdown()


def test_measured_memory_control_returns_truthful_capacity_facts_then_recovers(tmp_path: Path) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-016"
    )
    _arm(
        plane,
        case_id="PWK-UC-016",
        boundary="measured_memory_4_3_gib_vs_5_gib",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    claimed = store.claim_next_queued_run(
        str(worker["worker_id"]), executor_id=service._executor_id
    )
    assert claimed is not None
    try:
        with pytest.raises(HostCapacityError) as raised:
            service._acquire_host_run_lease(worker, claimed)
        assert raised.value.available["memoryBytes"] == int(4.3 * 1024**3)
        assert raised.value.required["memoryBytes"] == 5 * 1024**3
        assert raised.value.shortage["memoryBytes"] == 5 * 1024**3 - int(4.3 * 1024**3)
        assert raised.value.reservation["memoryBytes"] == 3 * 1024**3
        assert raised.value.next_retry_at
        assert service._acquire_host_run_lease(worker, claimed) is not None
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("boundary", "capacity_class", "dimension"),
    [
        ("maximum_capacity_overflow", "mission_slots", "missionSlots"),
        ("low_disk", "resource_pressure", "diskBytes"),
    ],
)
def test_capacity_controls_reject_at_atomic_lease_then_recover(
    tmp_path: Path,
    boundary: str,
    capacity_class: str,
    dimension: str,
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-016"
    )
    _arm(
        plane,
        case_id="PWK-UC-016",
        boundary=boundary,
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    claimed = store.claim_next_queued_run(
        str(worker["worker_id"]), executor_id=service._executor_id
    )
    assert claimed is not None
    try:
        with pytest.raises(HostCapacityError) as raised:
            service._acquire_host_run_lease(worker, claimed)
        assert raised.value.capacity_class == capacity_class
        assert raised.value.dimension == dimension
        assert raised.value.available
        assert raised.value.required
        assert raised.value.shortage
        assert raised.value.reservation
        assert raised.value.next_retry_at
        assert service._acquire_host_run_lease(worker, claimed) is not None
    finally:
        service.shutdown()


def test_last_reservation_control_keeps_atomic_single_winner_across_services(
    tmp_path: Path
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-016"
    )
    _arm(
        plane,
        case_id="PWK-UC-016",
        boundary="last_reservation_competition",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    first = _service(store, plane)
    second_store = Store(str(store.db_path))
    second_plane = LocalQAControlPlane(
        second_store.db_path, environment=_authority("PWK-UC-016")
    )
    second = _service(second_store, second_plane)
    claimed = store.claim_next_queued_run(
        str(worker["worker_id"]), executor_id=first._executor_id
    )
    assert claimed is not None

    def acquire(service: WorkersProjectsService) -> str:
        try:
            lease = service._acquire_host_run_lease(worker, claimed)
            return "won" if lease is not None else "lost"
        except HostCapacityError:
            return "lost"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(acquire, (first, second)))
        assert sorted(outcomes) == ["lost", "won"]
        active = store.list_active_host_run_leases()
        assert len([row for row in active if row["run_id"] == run["run_id"]]) == 1
    finally:
        first.shutdown()
        second.shutdown()


def test_callback_transport_control_interrupts_real_sender_once_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary="callback_transport_interruption",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    calls: list[bytes] = []

    class _Response:
        status_code = 204

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(
        "workers_projects_runtime.service.httpx.post",
        lambda _url, *, content, headers, timeout: calls.append(content) or _Response(),
    )
    monkeypatch.setattr(service, "_terminal_callback_response_decision", lambda *_args: ("accepted", 0))
    payload = {
        "callback_id": "cb_runtime_transport",
        "callback_ts": 1_900_000_000,
        "event": "run.queue_status",
        "worker_id": worker["worker_id"],
        "run_id": run["run_id"],
    }
    record = store.upsert_callback_outbox(
        callback_id=payload["callback_id"],
        project_id=str(worker["project_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        event_type="run.queue_status",
        url="https://qa.invalid/callback",
        payload_json=json.dumps(payload),
    )
    try:
        service._deliver_callback_record(
            worker,
            record,
            {"events_webhook_url": "https://qa.invalid/callback"},
        )
        assert len(calls) == 1
        settled = store.get_callback_outbox(str(record["callback_id"]))
        assert settled is not None and settled["status"] == "http_accepted"
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("boundary", "expected_stall_state"),
    [
        ("claimed_queue_stall", "claimed"),
        ("admitted_queue_stall", "admitted"),
    ],
)
def test_queue_stall_controls_expire_once_without_resurrecting_terminal_work(
    tmp_path: Path,
    boundary: str,
    expected_stall_state: str,
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary=boundary,
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    try:
        _run_processor_sync(service, str(worker["worker_id"]))
        stalled = store.get_run(str(run["run_id"]))
        assert stalled is not None and stalled["state"] == expected_stall_state
        deadline = datetime.fromisoformat(
            str(stalled["queue_deadline_at"]).replace("Z", "+00:00")
        )
        first = service.process_queued_work_status_once(
            now=deadline + timedelta(milliseconds=1)
        )
        second = service.process_queued_work_status_once(
            now=deadline + timedelta(seconds=1)
        )
        terminal = store.get_run(str(run["run_id"]))
        assert first["timedOut"] == 1
        assert second["timedOut"] == 0
        assert terminal is not None and terminal["state"] == "failed"
        assert terminal["failure_class"] == "queue_wait_timeout"
    finally:
        service.shutdown()


def test_status_refresh_timeout_race_leaves_one_terminal_callback_and_no_refresh(
    tmp_path: Path
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary="status_refresh_timeout_race",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=5)
    store.update_run(
        str(run["run_id"]),
        queue_next_status_at=(now - timedelta(seconds=1)).isoformat(),
        queue_deadline_at=deadline.isoformat(),
    )
    try:
        result = service.process_queued_work_status_once(now=now)
        terminal = store.get_run(str(run["run_id"]))
        callbacks = store.list_callback_outbox_for_run(
            str(run["run_id"]),
            tenant_id="local",
            owner_id=str(worker["owner_id"]),
            limit=20,
        )
        assert result["refreshed"] == 0
        assert terminal is not None and terminal["state"] == "failed"
        assert terminal["queue_status_sequence"] == 0
        assert len([row for row in callbacks if row["event_type"] == "run.failed"]) == 1
    finally:
        service.shutdown()


def test_expired_sender_lease_control_rotates_generation_before_old_sender_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary="expired_sender_lease_race",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    record = _queue_callback_record(store, worker, run, "cb_expired_sender_lease")
    posts: list[bytes] = []
    class _Response:
        status_code = 204

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        "workers_projects_runtime.service.httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Response(),
    )
    monkeypatch.setattr(
        service,
        "_terminal_callback_response_decision",
        lambda *_args: ("accepted", 0),
    )
    try:
        service._deliver_callback_record(
            worker,
            record,
            {"events_webhook_url": "https://qa.invalid/callback"},
        )
        settled = store.get_callback_outbox(str(record["callback_id"]))
        assert len(posts) == 1
        assert settled is not None and settled["status"] == "http_accepted"
        assert settled["delivery_generation"] == 2
    finally:
        service.shutdown()


def test_duplicate_callback_control_replays_same_identity_but_settles_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary="duplicate_callback_replay",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    service = _service(store, plane)
    record = _queue_callback_record(store, worker, run, "cb_duplicate_replay")
    receiver_rows: dict[str, bytes] = {}
    receiver_attempts: list[str] = []

    class _Response:
        status_code = 204

        def raise_for_status(self) -> None:
            return None

    def post(_url, *, content, headers, timeout):
        del headers, timeout
        callback_id = str(json.loads(content)["callback_id"])
        receiver_attempts.append(callback_id)
        receiver_rows.setdefault(callback_id, content)
        return _Response()

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    monkeypatch.setattr(
        service,
        "_terminal_callback_response_decision",
        lambda *_args: ("accepted", 0),
    )
    try:
        service._deliver_callback_record(
            worker,
            record,
            {"events_webhook_url": "https://qa.invalid/callback"},
        )
        settled = store.get_callback_outbox(str(record["callback_id"]))
        assert receiver_attempts == [record["callback_id"], record["callback_id"]]
        assert list(receiver_rows) == [record["callback_id"]]
        assert settled is not None and settled["status"] == "http_accepted"
        assert settled["attempts"] == 1
    finally:
        service.shutdown()


def test_transport_fault_does_not_consume_a_pending_duplicate_replay_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    for boundary in ("callback_transport_interruption", "duplicate_callback_replay"):
        _arm(
            plane,
            case_id="PWK-UC-017",
            boundary=boundary,
            delegation=delegation,
            worker=worker,
            run=run,
        )
    service = _service(store, plane)
    record = _queue_callback_record(store, worker, run, "cb_transport_then_duplicate")
    posts: list[bytes] = []

    class _Response:
        status_code = 204

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr(
        "workers_projects_runtime.service.httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Response(),
    )
    monkeypatch.setattr(
        service,
        "_terminal_callback_response_decision",
        lambda *_args: ("accepted", 0),
    )
    try:
        service._deliver_callback_record(
            worker,
            record,
            {"events_webhook_url": "https://qa.invalid/callback"},
        )
        pending = store.get_callback_outbox(str(record["callback_id"]))
        assert posts == []
        assert pending is not None and pending["status"] == "pending"
        service._deliver_callback_record(
            worker,
            pending,
            {"events_webhook_url": "https://qa.invalid/callback"},
        )
        settled = store.get_callback_outbox(str(record["callback_id"]))
        assert len(posts) == 2
        assert settled is not None and settled["status"] == "http_accepted"
    finally:
        service.shutdown()


def test_runtime_effect_audit_is_hash_only_and_queryable_after_restart(tmp_path: Path) -> None:
    _store, delegation, worker, run, _artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-016"
    )
    _arm(
        plane,
        case_id="PWK-UC-016",
        boundary="provider_auth_missing",
        delegation=delegation,
        worker=worker,
        run=run,
    )
    directive = plane.consume(
        "provider_auth_missing",
        owner_id=str(worker["owner_id"]),
        work_id=str(delegation["work_ref"]),
        run_id=str(run["run_id"]),
    )
    assert directive is not None
    plane.record_effect(directive, outcome="blocked_before_admission")
    reopened = LocalQAControlPlane(plane.db_path, environment=_authority("PWK-UC-016"))
    receipt = reopened.audit(
        {
            "contractVersion": 1,
            "caseId": "PWK-UC-016",
            "caseToken": _authority("PWK-UC-016")["VIVENTIUM_LOCAL_QA_CASE_TOKEN"],
            "sessionRef": _authority("PWK-UC-016")["VIVENTIUM_LOCAL_QA_SESSION_REF"],
            "candidateDigest": _CANDIDATE_DIGEST,
            "componentArtifactDigest": _COMPONENT_DIGEST,
        }
    )
    serialized = json.dumps(receipt, sort_keys=True)
    assert "effect_applied" in serialized
    assert "blocked_before_admission" not in serialized
    assert str(worker["owner_id"]) not in serialized
    assert str(delegation["work_ref"]) not in serialized


def test_artifact_runtime_hook_returns_typed_one_shot_fault(tmp_path: Path) -> None:
    store, delegation, worker, run, artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary="artifact_unavailable_restart_recovery",
        delegation=delegation,
        worker=worker,
        run=run,
        artifact_id=artifact_id,
    )
    service = _service(store, plane)
    artifact = Path(str(worker["workspace_dir"])) / "result.txt"
    try:
        assert service.local_qa_artifact_fault(worker, artifact) == "artifact_unavailable_restart_recovery"
        assert service.local_qa_artifact_fault(worker, artifact) == ""
    finally:
        service.shutdown()


def test_artifact_unavailable_api_recovers_after_service_restart(tmp_path: Path) -> None:
    store, delegation, worker, run, artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary="artifact_unavailable_restart_recovery",
        delegation=delegation,
        worker=worker,
        run=run,
        artifact_id=artifact_id,
    )
    url = f"/v1/workers/{worker['worker_id']}/artifacts/download?path=result.txt"
    first_app = create_app(
        db_path=str(store.db_path), runtime=_WorkspaceStubRuntime(), reconcile_on_startup=False
    )
    first_app.state.service._local_qa_control_plane = plane
    with TestClient(first_app) as client:
        unavailable = client.get(url)
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "artifact_unavailable"

    reopened = LocalQAControlPlane(
        store.db_path, environment=_authority("PWK-UC-017")
    )
    second_app = create_app(
        db_path=str(store.db_path), runtime=_WorkspaceStubRuntime(), reconcile_on_startup=False
    )
    second_app.state.service._local_qa_control_plane = reopened
    with TestClient(second_app) as client:
        recovered = client.get(url)
    assert recovered.status_code == 200
    assert recovered.content == b"synthetic artifact"


def test_expired_artifact_link_rejects_once_before_serving_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delegation, worker, run, artifact_id, plane = _fixture(
        tmp_path, case_id="PWK-UC-017"
    )
    _arm(
        plane,
        case_id="PWK-UC-017",
        boundary="artifact_link_expired",
        delegation=delegation,
        worker=worker,
        run=run,
        artifact_id=artifact_id,
    )
    monkeypatch.setenv(
        "GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-local-qa-signed-link-secret"
    )
    clock = [1_900_000_000]
    monkeypatch.setattr(
        "workers_projects_runtime.signed_links.time.time", lambda: clock[0]
    )
    token = sign_link_token(
        kind="artifact_download",
        worker_id=str(worker["worker_id"]),
        tenant_id="local",
        owner_id=str(worker["owner_id"]),
        path="result.txt",
    )
    assert token
    app = create_app(
        db_path=str(store.db_path), runtime=_WorkspaceStubRuntime(), reconcile_on_startup=False
    )
    app.state.service._local_qa_control_plane = plane
    with TestClient(app) as client:
        expired = client.get(f"/v1/signed-links/{token}")
        clock[0] += 1
        replacement = sign_link_token(
            kind="artifact_download",
            worker_id=str(worker["worker_id"]),
            tenant_id="local",
            owner_id=str(worker["owner_id"]),
            path="result.txt",
        )
        recovered = client.get(f"/v1/signed-links/{replacement}")
    assert expired.status_code == 401, expired.text
    assert expired.json()["detail"]["code"] == "artifact_link_expired"
    assert replacement and replacement != token
    assert recovered.status_code == 200
    assert recovered.content == b"synthetic artifact"


def test_importing_api_is_side_effect_free_and_does_not_create_the_default_database(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    default_database = source_root.parent / "data" / "runtime_phase1.db"
    before = hashlib.sha256(default_database.read_bytes()).hexdigest() if default_database.exists() else ""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import workers_projects_runtime.api; print('imported')",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(source_root)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    after = hashlib.sha256(default_database.read_bytes()).hexdigest() if default_database.exists() else ""
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "imported"
    assert after == before
