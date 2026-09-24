from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import workers_projects_runtime.local_qa_control as local_qa_control
from workers_projects_runtime.local_qa_control import (
    LocalQAAuthorityError,
    LocalQAControlError,
    LocalQAControlPlane,
    PRIVATE_INPUT_LIMIT_BYTES,
    SUPPORTED_FAULTS,
    main,
    read_private_request,
)


NOW = datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)
ARTIFACT_A = "sha256:" + "a" * 64
ARTIFACT_B = "sha256:" + "b" * 64
CANDIDATE_A = "sha256:" + "c" * 64
CANDIDATE_B = "sha256:" + "d" * 64
CANONICAL_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$")


def _secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _case_mode(case_id: str) -> str:
    return case_id.lower().replace("-", "_")


def _authority(
    case_id: str,
    token: str,
    session_ref: str,
    *,
    candidate_digest: str = CANDIDATE_A,
    artifact_digest: str = ARTIFACT_A,
) -> dict[str, str]:
    return {
        "VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE": _case_mode(case_id),
        "VIVENTIUM_LOCAL_QA_CASE_ID": case_id,
        "VIVENTIUM_LOCAL_QA_CASE_TOKEN": token,
        "VIVENTIUM_LOCAL_QA_SESSION_REF": session_ref,
        "VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST": candidate_digest,
        "VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST": artifact_digest,
    }


def _private_base(
    case_id: str,
    token: str,
    session_ref: str,
    *,
    candidate_digest: str = CANDIDATE_A,
    artifact_digest: str = ARTIFACT_A,
) -> dict[str, object]:
    return {
        "contractVersion": 1,
        "caseId": case_id,
        "caseToken": token,
        "sessionRef": session_ref,
        "candidateDigest": candidate_digest,
        "componentArtifactDigest": artifact_digest,
    }


def _fixture_run_id(owner_id: str, work_id: str) -> str:
    return "qa_run_" + hashlib.sha256(f"{owner_id}\0{work_id}".encode()).hexdigest()[:32]


def _arm_request(
    *,
    case_id: str,
    token: str,
    session_ref: str,
    boundary: str,
    owner_id: str,
    work_id: str,
    run_id: str | None = None,
    artifact_id: str = "",
    ttl_seconds: int = 60,
    candidate_digest: str = CANDIDATE_A,
    component_artifact_digest: str = ARTIFACT_A,
) -> dict[str, object]:
    return {
        **_private_base(
            case_id,
            token,
            session_ref,
            candidate_digest=candidate_digest,
            artifact_digest=component_artifact_digest,
        ),
        "scopeKind": "synthetic_local_qa",
        "boundary": boundary,
        "ownerId": owner_id,
        "workId": work_id,
        "runId": (
            run_id
            if run_id is not None
            else _fixture_run_id(owner_id, work_id)
        ),
        "artifactId": artifact_id,
        "ttlSeconds": ttl_seconds,
        "parameters": {},
    }


def _seed_exact_fixture(
    db_path: Path,
    *,
    owner_id: str,
    work_id: str,
    run_id: str,
    artifact_id: str,
    idempotency_key: str,
) -> None:
    identity = hashlib.sha256(f"{owner_id}\0{work_id}".encode()).hexdigest()
    project_id = "qa_project_" + identity[:32]
    worker_id = "qa_worker_" + identity[32:]
    artifact_payload = json.dumps(
        {
            "artifactRefs": {
                "available": True,
                "overflowCount": 0,
                "refs": [
                    {
                        "artifactRef": artifact_id,
                        "fingerprint": "sha256:" + artifact_id.removeprefix("artifact_sha256:"),
                        "kind": "text",
                        "sizeBytes": 1,
                        "state": "ready",
                    }
                ],
            },
            "observedAt": "2026-08-23T16:00:00.000+00:00",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                role TEXT NOT NULL, model TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
                project_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                instruction TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delegations (
                work_ref TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                origin_ref TEXT NOT NULL, title TEXT NOT NULL,
                origin_surface TEXT NOT NULL, project_id TEXT NOT NULL,
                worker_id TEXT NOT NULL, initial_run_id TEXT NOT NULL,
                current_run_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_trace_events (
                run_id TEXT NOT NULL, work_ref TEXT NOT NULL,
                tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO projects VALUES (?, 'local', ?)",
            (project_id, owner_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO workers VALUES "
            "(?, ?, 'local', ?, 'deterministic fixture', 'synthetic')",
            (worker_id, project_id, owner_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO runs VALUES (?, ?, ?, 'local', ?)",
            (
                run_id,
                worker_id,
                project_id,
                "Synthetic local-QA fixture. Do not perform external work.",
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO delegations VALUES "
            "(?, 'local', ?, ?, ?, ?, 'workbench', ?, ?, ?, ?)",
            (
                work_id,
                owner_id,
                idempotency_key,
                "qa_origin_" + secrets.token_hex(16),
                "Synthetic Parallel Work local-QA fixture",
                project_id,
                worker_id,
                run_id,
                run_id,
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO work_trace_events VALUES "
            "(?, ?, 'local', ?, 1, 'artifact.observed', ?)",
            (run_id, work_id, owner_id, artifact_payload),
        )


class _FixtureBackedControlPlane(LocalQAControlPlane):
    def arm(self, request: dict[str, object]) -> dict[str, object]:
        owner_id = str(request.get("ownerId") or "")
        work_id = str(request.get("workId") or "")
        if request.get("scopeKind") == "synthetic_local_qa" and owner_id and work_id:
            identity = hashlib.sha256(f"{owner_id}\0{work_id}".encode()).hexdigest()
            run_id = str(request.get("runId") or "") or "qa_run_" + identity[:32]
            artifact_id = str(request.get("artifactId") or "")
            if not artifact_id.startswith("artifact_sha256:"):
                artifact_id = "artifact_sha256:" + identity
            _seed_exact_fixture(
                self.db_path,
                owner_id=owner_id,
                work_id=work_id,
                run_id=run_id,
                artifact_id=artifact_id,
                idempotency_key="qa_idem_" + identity,
            )
        return super().arm(request)


@pytest.fixture
def private_scope() -> dict[str, str]:
    return {
        "token": _secret("case_token"),
        "session_ref": _secret("session"),
        "owner_id": _secret("qa_owner"),
        "work_id": _secret("work"),
        "run_id": _secret("run"),
        "artifact_id": "artifact_sha256:" + secrets.token_hex(32),
    }


def _plane(
    tmp_path: Path,
    *,
    case_id: str,
    token: str,
    session_ref: str,
    artifact_digest: str = ARTIFACT_A,
    candidate_digest: str = CANDIDATE_A,
    now: datetime = NOW,
) -> LocalQAControlPlane:
    return _FixtureBackedControlPlane(
        tmp_path / "runtime.sqlite3",
        environment=_authority(
            case_id,
            token,
            session_ref,
            candidate_digest=candidate_digest,
            artifact_digest=artifact_digest,
        ),
        clock=lambda: now,
    )


def _assert_private_values_absent(serialized: str, scope: dict[str, str]) -> None:
    for value in scope.values():
        assert value not in serialized


def test_fault_catalog_is_exact_and_data_driven() -> None:
    assert SUPPORTED_FAULTS == {
        "PWK-UC-016": (
            "provider_auth_missing",
            "provider_quota_cooldown_fallback",
            "provider_unavailable",
            "provider_internal_retry_threshold",
            "declared_long_fresh_then_stale",
            "maximum_capacity_overflow",
            "measured_memory_4_3_gib_vs_5_gib",
            "last_reservation_competition",
            "low_disk",
        ),
        "PWK-UC-017": (
            "callback_transport_interruption",
            "claimed_queue_stall",
            "admitted_queue_stall",
            "status_refresh_timeout_race",
            "expired_sender_lease_race",
            "duplicate_callback_replay",
            "artifact_link_expired",
            "artifact_unavailable_restart_recovery",
        ),
    }


@pytest.mark.parametrize(
    ("case_id", "boundary", "requires_run", "requires_artifact"),
    [
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
    ],
)
def test_every_supported_boundary_arms_and_returns_a_typed_one_shot_directive(
    tmp_path: Path,
    private_scope: dict[str, str],
    case_id: str,
    boundary: str,
    requires_run: bool,
    requires_artifact: bool,
) -> None:
    plane = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    request = _arm_request(
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary=boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"] if requires_run else "",
        artifact_id=private_scope["artifact_id"] if requires_artifact else "",
    )

    receipt = plane.arm(request)
    directive = plane.consume(
        boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"] if requires_run else "",
        artifact_id=private_scope["artifact_id"] if requires_artifact else "",
    )

    assert receipt["status"] == "armed"
    assert receipt["controlRef"].startswith("qac_sha256:")
    assert directive is not None
    assert directive.case_id == case_id
    assert directive.boundary == boundary
    assert directive.parameters["faultClass"] == boundary
    assert plane.consume(
        boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"] if requires_run else "",
        artifact_id=private_scope["artifact_id"] if requires_artifact else "",
    ) is None


def test_measured_memory_directive_is_exactly_4_3_gib_against_5_gib(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-016"
    boundary = "measured_memory_4_3_gib_vs_5_gib"
    plane = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    plane.arm(
        _arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary=boundary,
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
        )
    )

    directive = plane.consume(
        boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    )

    assert directive is not None
    available = int(4.3 * 1024**3)
    required = 5 * 1024**3
    assert directive.parameters == {
        "faultClass": boundary,
        "availableMemoryBytes": available,
        "requiredMemoryBytes": required,
        "shortageMemoryBytes": required - available,
        "reservationMemoryBytes": 3 * 1024**3,
        "nextRetrySeconds": 5,
    }


def test_wrong_token_case_owner_work_run_and_artifact_never_consume(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-017"
    boundary = "artifact_unavailable_restart_recovery"
    plane = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    request = _arm_request(
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary=boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
    )
    receipt = plane.arm(request)

    wrong_scope = {name: _secret(name) for name in private_scope}
    attempts = (
        (
            wrong_scope["owner_id"],
            private_scope["work_id"],
            private_scope["run_id"],
            private_scope["artifact_id"],
        ),
        (
            private_scope["owner_id"],
            wrong_scope["work_id"],
            private_scope["run_id"],
            private_scope["artifact_id"],
        ),
        (
            private_scope["owner_id"],
            private_scope["work_id"],
            wrong_scope["run_id"],
            private_scope["artifact_id"],
        ),
        (
            private_scope["owner_id"],
            private_scope["work_id"],
            private_scope["run_id"],
            wrong_scope["artifact_id"],
        ),
    )
    for owner_id, work_id, run_id, artifact_id in attempts:
        assert plane.consume(
            boundary,
            owner_id=owner_id,
            work_id=work_id,
            run_id=run_id,
            artifact_id=artifact_id,
        ) is None

    wrong_token_plane = LocalQAControlPlane(
        tmp_path / "runtime.sqlite3",
        environment=_authority(case_id, wrong_scope["token"], private_scope["session_ref"]),
        clock=lambda: NOW,
        artifact_digest=ARTIFACT_A,
    )
    wrong_case_plane = LocalQAControlPlane(
        tmp_path / "runtime.sqlite3",
        environment=_authority("PWK-UC-016", private_scope["token"], private_scope["session_ref"]),
        clock=lambda: NOW,
        artifact_digest=ARTIFACT_A,
    )
    assert wrong_token_plane.consume(
        boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
    ) is None
    assert wrong_case_plane.consume(
        boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
    ) is None

    query = plane.query(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )
    assert query["controls"] == [
        {
            **receipt,
            "status": "armed",
            "consumedAt": None,
            "clearedAt": None,
        }
    ]


def test_default_off_partial_authority_and_mismatched_tuple_fail_closed(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    disabled = LocalQAControlPlane(
        tmp_path / "disabled.sqlite3",
        environment={},
        clock=lambda: NOW,
    )
    request = _arm_request(
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
                boundary="low_disk",
                owner_id=private_scope["owner_id"],
                work_id=private_scope["work_id"],
                run_id=private_scope["run_id"],
    )
    with pytest.raises(LocalQAAuthorityError, match="inactive"):
        disabled.arm(request)
    assert disabled.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
    ) is None

    partial = LocalQAControlPlane(
        tmp_path / "partial.sqlite3",
        environment={"VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE": "pwk_uc_016"},
        clock=lambda: NOW,
    )
    with pytest.raises(LocalQAAuthorityError, match="canonical tuple"):
        partial.arm(request)
    assert partial.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
    ) is None

    mismatch = LocalQAControlPlane(
        tmp_path / "mismatch.sqlite3",
        environment=_authority("PWK-UC-017", private_scope["token"], private_scope["session_ref"]),
        clock=lambda: NOW,
        artifact_digest=ARTIFACT_A,
    )
    with pytest.raises(LocalQAAuthorityError, match="does not match"):
        mismatch.arm(request)


@pytest.mark.parametrize("ttl_seconds", [0, -1, 3601, True, 1.5])
def test_arm_rejects_out_of_bounds_or_non_integer_expiry(
    tmp_path: Path,
    private_scope: dict[str, str],
    ttl_seconds: object,
) -> None:
    plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    request = _arm_request(
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
    )
    request["ttlSeconds"] = ttl_seconds
    with pytest.raises(LocalQAControlError, match="ttlSeconds"):
        plane.arm(request)


def test_arm_rejects_ordinary_scope_wrong_case_boundary_missing_scope_and_extra_fields(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    base = _arm_request(
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
    )
    invalid = []
    invalid.append({**base, "scopeKind": "ordinary"})
    invalid.append({**base, "boundary": "callback_transport_interruption"})
    invalid.append({**base, "ownerId": ""})
    invalid.append({**base, "workId": ""})
    invalid.append({**base, "unexpected": "field"})
    invalid.append({**base, "parameters": {"hiddenBroadKnob": True}})
    for request in invalid:
        with pytest.raises(LocalQAControlError):
            plane.arm(request)

    missing_run = _arm_request(
        case_id="PWK-UC-017",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="claimed_queue_stall",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id="",
    )
    case_17_plane = _plane(
        tmp_path,
        case_id="PWK-UC-017",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    with pytest.raises(LocalQAControlError, match="runId"):
        case_17_plane.arm(missing_run)

    missing_artifact = {
        **missing_run,
        "boundary": "artifact_link_expired",
        "runId": private_scope["run_id"],
    }
    with pytest.raises(LocalQAControlError, match="artifactId"):
        case_17_plane.arm(missing_artifact)


def test_expired_control_is_never_consumed_and_cleanup_removes_only_terminal_rows(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-016"
    armed = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    armed.arm(
        _arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary="low_disk",
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            ttl_seconds=1,
        )
    )

    expired = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        now=NOW + timedelta(seconds=2),
    )
    assert expired.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    ) is None
    query = expired.query(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )
    assert query["controls"][0]["status"] == "expired"
    cleanup = expired.cleanup(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )
    assert cleanup["removedControls"] == 1
    assert cleanup["removedAuditEvents"] >= 2
    assert expired.query(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )["count"] == 0


def test_cleanup_refuses_live_armed_control_and_clear_is_exact(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-016"
    plane = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    receipt = plane.arm(
        _arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary="provider_unavailable",
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
        )
    )
    base = _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    with pytest.raises(LocalQAControlError, match="armed"):
        plane.cleanup(base)
    with pytest.raises(LocalQAControlError, match="not found"):
        plane.clear({**base, "controlRef": "qac_sha256:" + "0" * 64})

    cleared = plane.clear({**base, "controlRef": receipt["controlRef"]})
    assert cleared["status"] == "cleared"
    assert plane.consume(
        "provider_unavailable",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    ) is None
    cleanup = plane.cleanup(base)
    assert cleanup["removedControls"] == 1


def test_control_survives_restart_and_only_one_concurrent_consumer_wins(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-017"
    boundary = "expired_sender_lease_race"
    first = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    first.arm(
        _arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary=boundary,
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            run_id=private_scope["run_id"],
        )
    )

    restarted = [
        _plane(
            tmp_path,
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
        )
        for _ in range(8)
    ]

    def consume(plane: LocalQAControlPlane):
        return plane.consume(
            boundary,
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            run_id=private_scope["run_id"],
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(consume, restarted))

    assert sum(result is not None for result in results) == 1
    query = first.query(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )
    assert query["controls"][0]["status"] == "consumed"
    with sqlite3.connect(first.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_qa_fault_audit WHERE action = 'consumed'"
        ).fetchone()[0] == 1


def test_later_exact_arm_replay_reuses_original_control_and_changed_ttl_conflicts(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-017"
    boundary = "claimed_queue_stall"
    request = _arm_request(
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary=boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        ttl_seconds=60,
    )
    first = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        now=NOW,
    ).arm(request)

    later_plane = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        now=NOW + timedelta(seconds=10),
    )
    replay = later_plane.arm(request)

    assert replay["status"] == "already_armed"
    assert replay["controlRef"] == first["controlRef"]
    assert replay["expiresAt"] == first["expiresAt"]
    changed_ttl = {**request, "ttlSeconds": 61}
    with pytest.raises(LocalQAControlError, match="ttlSeconds"):
        later_plane.arm(changed_ttl)
    query = later_plane.query(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )
    assert query["count"] == 1
    assert query["controls"][0]["controlRef"] == first["controlRef"]


def test_artifact_replacement_fences_consumption_but_token_cleanup_still_works(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-017"
    boundary = "artifact_link_expired"
    first = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        artifact_digest=ARTIFACT_A,
    )
    first.arm(
        _arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary=boundary,
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            run_id=private_scope["run_id"],
            artifact_id=private_scope["artifact_id"],
        )
    )
    replaced = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        artifact_digest=ARTIFACT_B,
    )
    assert replaced.consume(
        boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
    ) is None

    cleanup = replaced.cleanup(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )
    assert cleanup["artifactReplacementCleared"] == 1
    assert cleanup["removedControls"] == 1


def test_raw_token_and_scope_are_absent_from_database_receipts_and_audit(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-017"
    boundary = "artifact_unavailable_restart_recovery"
    plane = _plane(
        tmp_path,
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    arm = plane.arm(
        _arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary=boundary,
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            run_id=private_scope["run_id"],
            artifact_id=private_scope["artifact_id"],
        )
    )
    plane.consume(
        boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
    )
    query = plane.query(
        _private_base(case_id, private_scope["token"], private_scope["session_ref"])
    )

    _assert_private_values_absent(json.dumps(arm, sort_keys=True), private_scope)
    _assert_private_values_absent(json.dumps(query, sort_keys=True), private_scope)
    assert len(json.dumps(query)) < 32_768
    with sqlite3.connect(plane.db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM local_qa_fault_controls"
        ).fetchall() + connection.execute(
            "SELECT * FROM local_qa_fault_audit"
        ).fetchall()
    _assert_private_values_absent(repr(rows), private_scope)
    assert stat.S_IMODE(Path(plane.db_path).stat().st_mode) == 0o600


def test_private_input_accepts_only_explicit_owner_only_file_or_inherited_fd(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    request = _private_base(
        "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
    )
    private_file = tmp_path / "private.json"
    private_file.write_text(json.dumps(request), encoding="utf-8")
    private_file.chmod(0o600)

    assert read_private_request(input_file=private_file) == request
    descriptor = os.open(private_file, os.O_RDONLY)
    try:
        assert descriptor >= 3
        assert read_private_request(input_fd=descriptor) == request
    finally:
        os.close(descriptor)

    private_file.chmod(0o640)
    with pytest.raises(LocalQAControlError, match="regular owner-only"):
        read_private_request(input_file=private_file)

    symlink = tmp_path / "private-link.json"
    symlink.symlink_to(private_file)
    with pytest.raises(LocalQAControlError, match="regular"):
        read_private_request(input_file=symlink)
    with pytest.raises(LocalQAControlError, match="descriptor"):
        read_private_request(input_fd=0)
    with pytest.raises(LocalQAControlError, match="explicit private input"):
        read_private_request()


def test_phase1_private_input_fifo_rejects_within_a_bounded_time(tmp_path: Path) -> None:
    fifo_path = tmp_path / "private-input.fifo"
    os.mkfifo(fifo_path, mode=0o600)
    probe = """
import sys
from workers_projects_runtime.local_qa_control import (
    LocalQAControlError,
    read_private_request,
)
try:
    read_private_request(input_file=sys.argv[1])
except LocalQAControlError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(Path(local_qa_control.__file__).resolve().parents[1]),
    }
    started = time.monotonic()

    completed = subprocess.run(
        [sys.executable, "-c", probe, str(fifo_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=1,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert time.monotonic() - started < 0.75


def test_phase1_database_leaf_open_uses_nonblock_before_type_check(
    tmp_path: Path,
    private_scope: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo_path = tmp_path / "runtime.fifo"
    os.mkfifo(fifo_path, mode=0o600)
    original_open = local_qa_control.os.open
    leaf_flags: list[int] = []

    def recording_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == fifo_path.name and dir_fd is not None:
            leaf_flags.append(flags)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(local_qa_control.os, "open", recording_open)

    with pytest.raises(LocalQAControlError, match="database"):
        LocalQAControlPlane(
            fifo_path,
            environment=_authority(
                "PWK-UC-016",
                private_scope["token"],
                private_scope["session_ref"],
            ),
        )

    assert leaf_flags
    assert all(flags & os.O_NONBLOCK for flags in leaf_flags)


def test_private_input_is_size_bounded(tmp_path: Path) -> None:
    private_file = tmp_path / "oversized.json"
    private_file.write_bytes(b"{" + b" " * PRIVATE_INPUT_LIMIT_BYTES + b"}")
    private_file.chmod(0o600)
    with pytest.raises(LocalQAControlError, match="size limit"):
        read_private_request(input_file=private_file)


@pytest.mark.parametrize(
    "raw",
    (
        b'{"contractVersion":1,"contractVersion":1}',
        b'{"outer":{"scope":"a","scope":"b"}}',
    ),
)
def test_phase1_private_json_rejects_duplicate_keys_at_every_depth(
    tmp_path: Path, raw: bytes
) -> None:
    private_file = tmp_path / "duplicate.json"
    private_file.write_bytes(raw)
    private_file.chmod(0o600)

    with pytest.raises(LocalQAControlError, match="valid JSON"):
        read_private_request(input_file=private_file)


@pytest.mark.parametrize(
    "raw",
    (
        b"\xff{}",
        b'{"contractVersion":1}\xff',
        b'{"value":"\xed\xa0\x80"}',
    ),
)
def test_phase1_invalid_utf8_is_fatal_bounded_and_never_echoed(
    tmp_path: Path,
    raw: bytes,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_marker = _secret("private_path")
    private_file = tmp_path / f"{private_marker}.json"
    private_file.write_bytes(raw)
    private_file.chmod(0o600)
    monkeypatch.setenv("WPR_DB_PATH", str(tmp_path / "utf8.sqlite3"))

    assert main(["query", "--input-file", str(private_file)]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert private_marker not in captured.err
    assert raw.decode("utf-8", errors="replace") not in captured.err
    assert len(captured.err.encode("utf-8")) <= 512
    assert json.loads(captured.err)["status"] == "rejected"


def test_phase1_private_file_rejects_hardlinks_and_unsafe_parent_chain(
    tmp_path: Path,
) -> None:
    secure_parent = tmp_path / "secure"
    secure_parent.mkdir(mode=0o700)
    private_file = secure_parent / "private.json"
    private_file.write_text('{"contractVersion":1}', encoding="utf-8")
    private_file.chmod(0o600)

    hardlink = secure_parent / "hardlink.json"
    os.link(private_file, hardlink)
    with pytest.raises(LocalQAControlError, match="regular owner-only"):
        read_private_request(input_file=hardlink)
    hardlink.unlink()

    symlink_parent = tmp_path / "linked-parent"
    symlink_parent.symlink_to(secure_parent, target_is_directory=True)
    with pytest.raises(LocalQAControlError, match="parent chain"):
        read_private_request(input_file=symlink_parent / private_file.name)

    writable_parent = tmp_path / "writable-parent"
    writable_parent.mkdir(mode=0o700)
    writable_file = writable_parent / "private.json"
    writable_file.write_text('{"contractVersion":1}', encoding="utf-8")
    writable_file.chmod(0o600)
    writable_parent.chmod(0o733)
    with pytest.raises(LocalQAControlError, match="parent chain"):
        read_private_request(input_file=writable_file)


def test_phase1_private_file_rejects_leaf_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_file = tmp_path / "private.json"
    private_file.write_text('{"value":"original"}', encoding="utf-8")
    private_file.chmod(0o600)
    moved_file = tmp_path / "moved.json"
    original_reader = local_qa_control._read_descriptor
    replaced = False

    def replace_then_read(descriptor: int) -> dict[str, object]:
        nonlocal replaced
        if not replaced:
            private_file.rename(moved_file)
            private_file.write_text('{"value":"replacement"}', encoding="utf-8")
            private_file.chmod(0o600)
            replaced = True
        return original_reader(descriptor)

    monkeypatch.setattr(local_qa_control, "_read_descriptor", replace_then_read)

    with pytest.raises(LocalQAControlError, match="changed"):
        read_private_request(input_file=private_file)


def test_phase1_private_fd_rejects_descriptor_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_file = tmp_path / "original.json"
    replacement_file = tmp_path / "replacement.json"
    original_file.write_text('{"value":"original"}', encoding="utf-8")
    replacement_file.write_bytes(b"")
    original_file.chmod(0o600)
    replacement_file.chmod(0o600)
    descriptor = os.open(original_file, os.O_RDONLY)
    replacement = os.open(replacement_file, os.O_RDONLY)
    original_read = local_qa_control.os.read
    switched = False

    def replace_after_read(fd: int, size: int) -> bytes:
        nonlocal switched
        chunk = original_read(fd, size)
        if fd == descriptor and not switched:
            os.lseek(replacement, 0, os.SEEK_END)
            os.dup2(replacement, descriptor)
            switched = True
        return chunk

    monkeypatch.setattr(local_qa_control.os, "read", replace_after_read)
    try:
        with pytest.raises(LocalQAControlError, match="changed"):
            read_private_request(input_fd=descriptor)
    finally:
        os.close(descriptor)
        os.close(replacement)


def test_phase1_nonregular_and_wrong_owner_descriptors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_fd, write_fd = os.pipe()
    left, right = socket.socketpair()
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    device_fd = os.open("/dev/null", os.O_RDONLY)
    fifo_path = tmp_path / "private.fifo"
    os.mkfifo(fifo_path, mode=0o600)
    fifo_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        for descriptor in (
            read_fd,
            left.fileno(),
            directory_fd,
            device_fd,
            fifo_fd,
        ):
            with pytest.raises(LocalQAControlError, match="regular owner-only"):
                read_private_request(input_fd=descriptor)
    finally:
        os.close(read_fd)
        os.close(write_fd)
        left.close()
        right.close()
        os.close(directory_fd)
        os.close(device_fd)
        os.close(fifo_fd)

    private_file = tmp_path / "wrong-owner.json"
    private_file.write_text('{"contractVersion":1}', encoding="utf-8")
    private_file.chmod(0o600)
    descriptor = os.open(private_file, os.O_RDONLY)
    actual_uid = os.getuid()
    monkeypatch.setattr(local_qa_control.os, "getuid", lambda: actual_uid + 1)
    try:
        with pytest.raises(LocalQAControlError, match="regular owner-only"):
            read_private_request(input_fd=descriptor)
    finally:
        os.close(descriptor)


def test_phase1_all_persisted_timestamps_are_exact_canonical_milliseconds(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    exact_now = datetime(2026, 8, 23, 16, 0, 0, 123456, tzinfo=timezone.utc)
    plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        now=exact_now,
    )
    receipt = plane.arm(
        _arm_request(
            case_id="PWK-UC-016",
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary="low_disk",
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
        )
    )
    directive = plane.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    )

    assert CANONICAL_TIME.fullmatch(str(receipt["expiresAt"]))
    assert directive is not None
    assert CANONICAL_TIME.fullmatch(directive.consumed_at)
    with sqlite3.connect(plane.db_path) as connection:
        controls = connection.execute(
            "SELECT armed_at, expires_at, consumed_at, cleared_at FROM local_qa_fault_controls"
        ).fetchall()
        audit = connection.execute(
            "SELECT occurred_at FROM local_qa_fault_audit"
        ).fetchall()
    for row in [*controls, *audit]:
        for value in row:
            if value is not None:
                assert CANONICAL_TIME.fullmatch(str(value))


def test_phase1_root_measured_candidate_and_service_digests_bind_every_control(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-016"
    environment = {
        **_authority(case_id, private_scope["token"], private_scope["session_ref"]),
        "VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST": CANDIDATE_A,
        "VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST": ARTIFACT_A,
    }
    plane = LocalQAControlPlane(
        tmp_path / "bound.sqlite3", environment=environment, clock=lambda: NOW
    )
    request = {
        **_arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary="low_disk",
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            run_id=private_scope["run_id"],
        ),
        "candidateDigest": CANDIDATE_A,
        "componentArtifactDigest": ARTIFACT_A,
    }
    _seed_exact_fixture(
        plane.db_path,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
        idempotency_key="qa_idem_" + secrets.token_hex(32),
    )

    receipt = plane.arm(request)

    assert receipt["status"] == "armed"
    with sqlite3.connect(plane.db_path) as connection:
        stored = connection.execute(
            "SELECT candidate_digest, component_artifact_digest "
            "FROM local_qa_fault_controls"
        ).fetchone()
    assert stored == (CANDIDATE_A, ARTIFACT_A)
    for changed_field, changed_value in (
        ("candidateDigest", CANDIDATE_B),
        ("componentArtifactDigest", ARTIFACT_B),
    ):
        with pytest.raises(LocalQAAuthorityError, match="artifact binding"):
            plane.arm({**request, changed_field: changed_value})


def test_phase1_component_digest_cannot_be_self_attested_without_root_binding(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    canonical_only = {
        key: value
        for key, value in _authority(
            "PWK-UC-016",
            private_scope["token"],
            private_scope["session_ref"],
        ).items()
        if key
        not in {
            "VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST",
            "VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST",
        }
    }
    with pytest.raises(LocalQAAuthorityError, match="artifact binding"):
        LocalQAControlPlane(
            tmp_path / "self-attested.sqlite3",
            environment=canonical_only,
            artifact_digest=ARTIFACT_A,
        )


@pytest.mark.parametrize(
    "environment",
    (
        {},
        {"VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE": "pwk_uc_016"},
    ),
)
def test_phase1_management_requires_the_complete_canonical_tuple(
    tmp_path: Path,
    private_scope: dict[str, str],
    environment: dict[str, str],
) -> None:
    plane = LocalQAControlPlane(
        tmp_path / ("management-" + secrets.token_hex(4) + ".sqlite3"),
        environment=environment,
        clock=lambda: NOW,
    )
    request = _private_base(
        "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
    )

    with pytest.raises(LocalQAAuthorityError):
        plane.query(request)
    with pytest.raises(LocalQAAuthorityError):
        plane.cleanup(request)


def test_phase1_wrong_candidate_cleanup_cannot_remove_a_terminal_exact_control(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    plane.arm(
        _arm_request(
            case_id="PWK-UC-016",
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary="low_disk",
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
        )
    )
    assert plane.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    ) is not None

    cleanup = plane.cleanup(
        _private_base(
            "PWK-UC-016",
            private_scope["token"],
            private_scope["session_ref"],
            candidate_digest=CANDIDATE_B,
        )
    )

    assert cleanup["removedControls"] == 0
    assert plane.query(
        _private_base(
            "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
        )
    )["count"] == 1


@pytest.mark.parametrize("form", ("separate", "equals"))
def test_phase1_duplicate_cli_option_forms_are_typed_bounded_rejections(
    tmp_path: Path,
    private_scope: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    form: str,
) -> None:
    request = _private_base(
        "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
    )
    private_file = tmp_path / f"{private_scope['token']}.json"
    private_file.write_text(json.dumps(request), encoding="utf-8")
    private_file.chmod(0o600)
    monkeypatch.setenv("WPR_DB_PATH", str(tmp_path / "duplicates.sqlite3"))
    for key, value in _authority(
        "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
    ).items():
        monkeypatch.setenv(key, value)
    arguments = ["query", "--input-file", str(private_file)]
    arguments.extend(
        ["--input-file", str(private_file)]
        if form == "separate"
        else [f"--input-file={private_file}"]
    )

    assert main(arguments) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert private_scope["token"] not in captured.err
    assert len(captured.err.encode("utf-8")) <= 512
    assert json.loads(captured.err)["status"] == "rejected"


def test_phase1_mixed_unknown_and_nonregular_cli_inputs_never_exit_or_echo(
    tmp_path: Path,
    private_scope: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_file = tmp_path / f"{private_scope['token']}.json"
    private_file.write_text("{}", encoding="utf-8")
    private_file.chmod(0o600)
    read_fd, write_fd = os.pipe()
    attempts = (
        ["query", "--input-file", str(private_file), "--input-fd", str(read_fd)],
        ["query", f"--unknown={private_scope['token']}", "--input-fd", str(read_fd)],
        ["query", "--input-fd", str(read_fd)],
    )
    try:
        for arguments in attempts:
            assert main(arguments) == 2
            captured = capsys.readouterr()
            assert captured.out == ""
            assert private_scope["token"] not in captured.err
            assert str(private_file) not in captured.err
            assert len(captured.err.encode("utf-8")) <= 512
            assert json.loads(captured.err)["status"] == "rejected"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_phase1_arm_requires_one_exact_durable_synthetic_fixture(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    case_id = "PWK-UC-017"
    boundary = "artifact_link_expired"
    artifact_id = "artifact_sha256:" + secrets.token_hex(32)
    idempotency_key = _secret("qa_idem")
    plane = LocalQAControlPlane(
        tmp_path / "runtime.sqlite3",
        environment=_authority(
            case_id, private_scope["token"], private_scope["session_ref"]
        ),
        clock=lambda: NOW,
    )
    request = _arm_request(
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary=boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=artifact_id,
    )

    with pytest.raises(LocalQAControlError, match="synthetic fixture"):
        plane.arm(request)

    _seed_exact_fixture(
        plane.db_path,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=artifact_id,
        idempotency_key=idempotency_key,
    )
    assert plane.arm(request)["status"] == "armed"


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    (
        ("ownerId", "qa_owner_cross_tenant"),
        ("workId", "qa_work_nonexistent"),
        ("runId", "qa_run_nonexistent"),
        ("artifactId", "artifact_sha256:" + "f" * 64),
    ),
)
def test_phase1_arm_rejects_cross_owner_or_mismatched_fixture_relations(
    tmp_path: Path,
    private_scope: dict[str, str],
    changed_field: str,
    changed_value: str,
) -> None:
    artifact_id = "artifact_sha256:" + secrets.token_hex(32)
    idempotency_key = _secret("qa_idem")
    plane = LocalQAControlPlane(
        tmp_path / "runtime.sqlite3",
        environment=_authority(
            "PWK-UC-017", private_scope["token"], private_scope["session_ref"]
        ),
        clock=lambda: NOW,
    )
    _seed_exact_fixture(
        plane.db_path,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=artifact_id,
        idempotency_key=idempotency_key,
    )
    request = _arm_request(
        case_id="PWK-UC-017",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="artifact_unavailable_restart_recovery",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=artifact_id,
    )
    request[changed_field] = changed_value

    with pytest.raises(LocalQAControlError, match="synthetic fixture"):
        plane.arm(request)


def test_phase1_arm_rejects_a_non_synthetic_durable_idempotency_key(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    artifact_id = "artifact_sha256:" + secrets.token_hex(32)
    plane = LocalQAControlPlane(
        tmp_path / "runtime.sqlite3",
        environment=_authority(
            "PWK-UC-017", private_scope["token"], private_scope["session_ref"]
        ),
        clock=lambda: NOW,
    )
    _seed_exact_fixture(
        plane.db_path,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=artifact_id,
        idempotency_key="ordinary_idempotency_key",
    )

    with pytest.raises(LocalQAControlError, match="synthetic fixture"):
        plane.arm(
            _arm_request(
                case_id="PWK-UC-017",
                token=private_scope["token"],
                session_ref=private_scope["session_ref"],
                boundary="artifact_link_expired",
                owner_id=private_scope["owner_id"],
                work_id=private_scope["work_id"],
                run_id=private_scope["run_id"],
                artifact_id=artifact_id,
            )
        )


@pytest.mark.parametrize("terminal", ("consumed", "expired", "cleared"))
def test_phase1_terminal_control_replay_never_creates_a_second_armed_row(
    tmp_path: Path, private_scope: dict[str, str], terminal: str
) -> None:
    plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    request = _arm_request(
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        ttl_seconds=1 if terminal == "expired" else 60,
    )
    first = plane.arm(request)
    if terminal == "consumed":
        assert plane.consume(
            "low_disk",
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
        ) is not None
    elif terminal == "cleared":
        plane.clear(
            {
                **_private_base(
                    "PWK-UC-016",
                    private_scope["token"],
                    private_scope["session_ref"],
                ),
                "controlRef": first["controlRef"],
            }
        )
    else:
        plane = _plane(
            tmp_path,
            case_id="PWK-UC-016",
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            now=NOW + timedelta(seconds=2),
        )
        plane.query(
            _private_base(
                "PWK-UC-016",
                private_scope["token"],
                private_scope["session_ref"],
            )
        )

    replay = plane.arm(request)

    assert replay["controlRef"] == first["controlRef"]
    assert replay["expiresAt"] == first["expiresAt"]
    assert replay["status"] == terminal
    with sqlite3.connect(plane.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_qa_fault_controls"
        ).fetchone()[0] == 1


def test_phase1_terminal_replay_with_changed_ttl_is_rejected(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    request = _arm_request(
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        ttl_seconds=60,
    )
    plane.arm(request)
    assert plane.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    ) is not None

    with pytest.raises(LocalQAControlError, match="ttlSeconds"):
        plane.arm({**request, "ttlSeconds": 61})


def test_phase1_concurrent_restart_replay_after_consumption_keeps_one_control(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    request = _arm_request(
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
    )
    first_plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    first = first_plane.arm(request)
    assert first_plane.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    ) is not None
    restarted = [
        _plane(
            tmp_path,
            case_id="PWK-UC-016",
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
        )
        for _ in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda plane: plane.arm(request), restarted))

    assert {receipt["controlRef"] for receipt in receipts} == {first["controlRef"]}
    assert {receipt["status"] for receipt in receipts} == {"consumed"}
    with sqlite3.connect(first_plane.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_qa_fault_controls"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM local_qa_fault_arm_ledger"
        ).fetchone()[0] == 1


def test_phase1_cleanup_retains_only_the_durable_one_shot_tombstone(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    plane = _plane(
        tmp_path,
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
    )
    request = _arm_request(
        case_id="PWK-UC-016",
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary="low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
    )
    plane.arm(request)
    assert plane.consume(
        "low_disk",
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=_fixture_run_id(private_scope["owner_id"], private_scope["work_id"]),
    ) is not None
    base = _private_base(
        "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
    )

    assert plane.cleanup(base)["removedControls"] == 1
    with pytest.raises(LocalQAControlError, match="already finalized"):
        plane.arm(request)
    with sqlite3.connect(plane.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_qa_fault_controls"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM local_qa_fault_arm_ledger"
        ).fetchone()[0] == 1


def test_phase1_database_rejects_symlink_and_writable_parent_components(
    tmp_path: Path, private_scope: dict[str, str]
) -> None:
    real = tmp_path / "real.sqlite3"
    sqlite3.connect(real).close()
    real.chmod(0o600)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(real)
    with pytest.raises(LocalQAControlError, match="database"):
        LocalQAControlPlane(
            linked,
            environment=_authority(
                "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
            ),
        )

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_db = unsafe_parent / "runtime.sqlite3"
    unsafe_parent.chmod(0o733)
    try:
        with pytest.raises(LocalQAControlError, match="database"):
            LocalQAControlPlane(
                unsafe_db,
                environment=_authority(
                    "PWK-UC-016",
                    private_scope["token"],
                    private_scope["session_ref"],
                ),
            )
    finally:
        unsafe_parent.chmod(0o700)


def test_phase1_database_path_replacement_inside_connect_is_rejected(
    tmp_path: Path,
    private_scope: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    database.write_bytes(b"")
    database.chmod(0o600)
    displaced = tmp_path / "displaced.sqlite3"
    original_connect = local_qa_control.sqlite3.connect
    replaced = False

    def replacing_connect(*args: object, **kwargs: object):
        nonlocal replaced
        if not replaced:
            database.rename(displaced)
            database.write_bytes(b"")
            database.chmod(0o600)
            replaced = True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(local_qa_control.sqlite3, "connect", replacing_connect)

    with pytest.raises(LocalQAControlError, match="database"):
        LocalQAControlPlane(
            database,
            environment=_authority(
                "PWK-UC-016", private_scope["token"], private_scope["session_ref"]
            ),
        )


def test_phase1_database_open_allows_same_inode_to_change_during_concurrent_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    database.write_bytes(b"")
    database.chmod(0o600)
    original_stat = local_qa_control.os.stat
    mutated = False

    def changing_stat(path: object, *args: object, **kwargs: object):
        nonlocal mutated
        if path == database.name and kwargs.get("dir_fd") is not None and not mutated:
            with database.open("ab") as handle:
                handle.write(b"same-inode-write")
            mutated = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(local_qa_control.os, "stat", changing_stat)

    descriptor, directory_descriptor, _parents, identity = (
        local_qa_control._open_database_path(database, create=False)
    )
    try:
        current = original_stat(database)
        assert identity[:6] == local_qa_control._file_identity(current)[:6]
        assert identity[6:] != local_qa_control._file_identity(current)[6:]
    finally:
        local_qa_control._close_database_path(descriptor, directory_descriptor)


def test_cli_uses_private_input_and_prints_only_bounded_redacted_receipts(
    tmp_path: Path,
    private_scope: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case_id = "PWK-UC-016"
    boundary = "low_disk"
    request = _arm_request(
        case_id=case_id,
        token=private_scope["token"],
        session_ref=private_scope["session_ref"],
        boundary=boundary,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
    )
    private_file = tmp_path / "private-arm.json"
    private_file.write_text(json.dumps(request), encoding="utf-8")
    private_file.chmod(0o600)
    database = tmp_path / "cli.sqlite3"
    _seed_exact_fixture(
        database,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
        idempotency_key="qa_idem_" + secrets.token_hex(32),
    )
    database.chmod(0o600)
    monkeypatch.setenv("WPR_DB_PATH", str(database))
    for key, value in _authority(
        case_id, private_scope["token"], private_scope["session_ref"]
    ).items():
        monkeypatch.setenv(key, value)

    assert main(["arm", "--input-file", str(private_file)]) == 0
    output = capsys.readouterr()

    parsed = json.loads(output.out)
    assert parsed["operation"] == "arm"
    assert parsed["status"] == "armed"
    assert output.err == ""
    _assert_private_values_absent(output.out + output.err, private_scope)
    assert len(output.out) < 4096


def test_cli_query_clear_and_cleanup_use_the_same_private_session(
    tmp_path: Path,
    private_scope: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case_id = "PWK-UC-017"
    private_base = _private_base(
        case_id, private_scope["token"], private_scope["session_ref"]
    )
    private_payloads = {
        "arm": _arm_request(
            case_id=case_id,
            token=private_scope["token"],
            session_ref=private_scope["session_ref"],
            boundary="callback_transport_interruption",
            owner_id=private_scope["owner_id"],
            work_id=private_scope["work_id"],
            run_id=private_scope["run_id"],
        ),
        "query": private_base,
        "cleanup": private_base,
    }
    paths: dict[str, Path] = {}
    for operation, payload in private_payloads.items():
        path = tmp_path / f"private-{operation}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        paths[operation] = path
    database = tmp_path / "cli-workflow.sqlite3"
    _seed_exact_fixture(
        database,
        owner_id=private_scope["owner_id"],
        work_id=private_scope["work_id"],
        run_id=private_scope["run_id"],
        artifact_id=private_scope["artifact_id"],
        idempotency_key="qa_idem_" + secrets.token_hex(32),
    )
    database.chmod(0o600)
    monkeypatch.setenv("WPR_DB_PATH", str(database))
    for key, value in _authority(
        case_id, private_scope["token"], private_scope["session_ref"]
    ).items():
        monkeypatch.setenv(key, value)

    assert main(["arm", "--input-file", str(paths["arm"])]) == 0
    armed = json.loads(capsys.readouterr().out)
    assert main(["query", "--input-file", str(paths["query"])]) == 0
    queried = json.loads(capsys.readouterr().out)
    clear_path = tmp_path / "private-clear.json"
    clear_path.write_text(
        json.dumps({**private_base, "controlRef": armed["controlRef"]}),
        encoding="utf-8",
    )
    clear_path.chmod(0o600)
    assert main(["clear", "--input-file", str(clear_path)]) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert main(["cleanup", "--input-file", str(paths["cleanup"])]) == 0
    cleaned = json.loads(capsys.readouterr().out)

    assert queried["count"] == 1
    assert queried["controls"][0]["controlRef"] == armed["controlRef"]
    assert cleared["status"] == "cleared"
    assert cleaned["removedControls"] == 1
    serialized = json.dumps([armed, queried, cleared, cleaned], sort_keys=True)
    _assert_private_values_absent(serialized, private_scope)
