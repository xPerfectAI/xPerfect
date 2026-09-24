from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote


PRIVATE_INPUT_LIMIT_BYTES = 64 * 1024
MAX_QUERY_CONTROLS = 64
PRIVATE_ERROR_LIMIT_BYTES = 512
AUTHORITY_KEYS = (
    "VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE",
    "VIVENTIUM_LOCAL_QA_CASE_ID",
    "VIVENTIUM_LOCAL_QA_CASE_TOKEN",
    "VIVENTIUM_LOCAL_QA_SESSION_REF",
)
CANDIDATE_DIGEST_ENV = "VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST"
COMPONENT_ARTIFACT_DIGEST_ENV = "VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST"
SUPPORTED_FAULTS = {
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
CASE_MODES = {
    "PWK-UC-016": "pwk_uc_016",
    "PWK-UC-017": "pwk_uc_017",
}
RUN_SCOPED_FAULTS = frozenset(
    boundary
    for boundaries in SUPPORTED_FAULTS.values()
    for boundary in boundaries
)
ARTIFACT_SCOPED_FAULTS = frozenset(
    {"artifact_link_expired", "artifact_unavailable_restart_recovery"}
)
_ARM_FIELDS = frozenset(
    {
        "contractVersion",
        "caseId",
        "caseToken",
        "sessionRef",
        "candidateDigest",
        "componentArtifactDigest",
        "scopeKind",
        "boundary",
        "ownerId",
        "workId",
        "runId",
        "artifactId",
        "ttlSeconds",
        "parameters",
    }
)
_SELECTED_OWNER_ARM_FIELDS = _ARM_FIELDS | frozenset({"fixtureAttestation"})
_BASE_FIELDS = frozenset(
    {
        "contractVersion",
        "caseId",
        "caseToken",
        "sessionRef",
        "candidateDigest",
        "componentArtifactDigest",
    }
)


class LocalQAControlError(RuntimeError):
    """A local-QA request is invalid without echoing private input."""


class LocalQAAuthorityError(LocalQAControlError):
    """The canonical local-QA authority is absent or does not match."""


@dataclass(frozen=True)
class LocalQAAuthority:
    case_id: str
    case_mode: str
    case_token: str
    session_ref: str


@dataclass(frozen=True)
class LocalQAArtifactBinding:
    candidate_digest: str
    component_artifact_digest: str


@dataclass(frozen=True)
class LocalQAFaultDirective:
    control_ref: str
    case_id: str
    boundary: str
    parameters: dict[str, object]
    scope_hashes: dict[str, str]
    consumed_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _hash_private(domain: str, value: str) -> str:
    digest = hashlib.sha256(f"glasshive-local-qa-v1\0{domain}\0{value}".encode()).hexdigest()
    return f"sha256:{digest}"


def _clean_private_identity(value: object, field: str, *, required: bool) -> str:
    if not isinstance(value, str):
        raise LocalQAControlError(f"{field} is invalid")
    cleaned = value.strip()
    if required and not cleaned:
        raise LocalQAControlError(f"{field} is required")
    if len(cleaned) > 512 or any(ord(character) < 33 for character in cleaned):
        if cleaned:
            raise LocalQAControlError(f"{field} is invalid")
    return cleaned


def _authority_from_environment(
    environment: Mapping[str, str],
) -> LocalQAAuthority | None:
    values = {key: str(environment.get(key, "") or "").strip() for key in AUTHORITY_KEYS}
    populated = {key for key, value in values.items() if value}
    if not populated:
        return None
    if populated != set(AUTHORITY_KEYS):
        raise LocalQAAuthorityError("The canonical tuple for local-QA authority is incomplete")
    case_id = values["VIVENTIUM_LOCAL_QA_CASE_ID"]
    case_mode = values["VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE"]
    if case_id not in CASE_MODES or CASE_MODES[case_id] != case_mode:
        raise LocalQAAuthorityError("The canonical local-QA case and mode do not match")
    token = values["VIVENTIUM_LOCAL_QA_CASE_TOKEN"]
    session_ref = values["VIVENTIUM_LOCAL_QA_SESSION_REF"]
    if len(token) < 32 or len(token) > 1024 or len(session_ref) < 16 or len(session_ref) > 512:
        raise LocalQAAuthorityError("The canonical local-QA authority tuple is invalid")
    return LocalQAAuthority(case_id, case_mode, token, session_ref)


def _clean_digest(value: object) -> str:
    if not isinstance(value, str):
        raise LocalQAAuthorityError("The canonical local-QA artifact binding is invalid")
    if (
        len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise LocalQAAuthorityError("The canonical local-QA artifact binding is invalid")
    return value


def _artifact_binding_from_environment(
    environment: Mapping[str, str],
) -> LocalQAArtifactBinding | None:
    candidate = str(environment.get(CANDIDATE_DIGEST_ENV, "") or "").strip()
    component = str(environment.get(COMPONENT_ARTIFACT_DIGEST_ENV, "") or "").strip()
    if not candidate and not component:
        return None
    if not candidate or not component:
        raise LocalQAAuthorityError("The canonical local-QA artifact binding is incomplete")
    return LocalQAArtifactBinding(_clean_digest(candidate), _clean_digest(component))


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_private_file(descriptor: int) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise LocalQAControlError("The private input descriptor is invalid") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise LocalQAControlError("The private input must be a regular owner-only file")
    if metadata.st_size > PRIVATE_INPUT_LIMIT_BYTES:
        raise LocalQAControlError("The private input exceeds the size limit")
    return metadata


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_descriptor(descriptor: int) -> dict[str, object]:
    before = _validate_private_file(descriptor)
    try:
        if os.lseek(descriptor, 0, os.SEEK_CUR) != 0:
            raise LocalQAControlError("The private input descriptor position is invalid")
    except OSError as exc:
        raise LocalQAControlError("The private input descriptor is invalid") from exc
    chunks: list[bytes] = []
    remaining = PRIVATE_INPUT_LIMIT_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(8192, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > PRIVATE_INPUT_LIMIT_BYTES:
        raise LocalQAControlError("The private input exceeds the size limit")
    after = _validate_private_file(descriptor)
    if _file_identity(before) != _file_identity(after) or len(payload) != before.st_size:
        raise LocalQAControlError("The private input changed during validation")
    try:
        parsed = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LocalQAControlError("The private input is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LocalQAControlError("The private input must be a JSON object")
    return parsed


def _validate_private_parent(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise LocalQAControlError("The private input parent chain is unsafe")


def _open_private_path(
    path: Path,
) -> tuple[int, int, tuple[tuple[int, ...], ...], tuple[int, ...]]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise LocalQAControlError("The private input parent chain is unsafe")
    components = path.parts[1:]
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_fd = -1
    try:
        directory_fd = os.open("/", directory_flags)
        parent_identities: list[tuple[int, ...]] = []
        root_metadata = os.fstat(directory_fd)
        _validate_private_parent(root_metadata)
        parent_identities.append(_file_identity(root_metadata))
        for component in components[:-1]:
            if component in {"", ".", ".."}:
                raise LocalQAControlError("The private input parent chain is unsafe")
            try:
                child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise LocalQAControlError("The private input parent chain is unsafe") from exc
            os.close(directory_fd)
            directory_fd = child_fd
            metadata = os.fstat(directory_fd)
            _validate_private_parent(metadata)
            parent_identities.append(_file_identity(metadata))
        try:
            descriptor = os.open(components[-1], file_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise LocalQAControlError(
                "The private input must be a regular owner-only file"
            ) from exc
        try:
            file_metadata = _validate_private_file(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, directory_fd, tuple(parent_identities), _file_identity(file_metadata)
    except Exception:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise


def _open_database_path(
    path: Path, *, create: bool
) -> tuple[int, int, tuple[tuple[int, ...], ...], tuple[int, ...]]:
    if (
        not path.is_absolute()
        or path.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise LocalQAControlError("The local-QA database path is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_fd = -1
    try:
        directory_fd = os.open("/", directory_flags)
        parents: list[tuple[int, ...]] = []
        root_metadata = os.fstat(directory_fd)
        _validate_private_parent(root_metadata)
        parents.append(_file_identity(root_metadata))
        for component in path.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            metadata = os.fstat(directory_fd)
            _validate_private_parent(metadata)
            parents.append(_file_identity(metadata))
        try:
            descriptor = os.open(path.name, file_flags, dir_fd=directory_fd)
        except OSError as exc:
            if not create or exc.errno != errno.ENOENT:
                raise
            descriptor = os.open(
                path.name,
                file_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            os.close(descriptor)
            raise LocalQAControlError("The local-QA database permissions are invalid")
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_identity(metadata)[:6] != _file_identity(current)[:6]:
            os.close(descriptor)
            raise LocalQAControlError("The local-QA database identity changed")
        return descriptor, directory_fd, tuple(parents), _file_identity(metadata)
    except (OSError, RuntimeError, LocalQAControlError) as exc:
        if directory_fd >= 0:
            os.close(directory_fd)
        if isinstance(exc, LocalQAControlError):
            if "private input parent chain" not in str(exc):
                raise
            raise LocalQAControlError("The local-QA database path is invalid") from exc
        raise LocalQAControlError("The local-QA database path is invalid") from exc


def _close_database_path(descriptor: int, directory_fd: int) -> None:
    for opened in (descriptor, directory_fd):
        try:
            os.close(opened)
        except OSError:
            pass


def _close_private_path(descriptor: int, directory_fd: int) -> None:
    os.close(descriptor)
    os.close(directory_fd)


def read_private_request(
    *, input_fd: int | None = None, input_file: str | os.PathLike[str] | None = None
) -> dict[str, object]:
    """Read private control input without ever accepting redirected stdin."""

    if (input_fd is None) == (input_file is None):
        raise LocalQAControlError("Use exactly one explicit private input source")
    if input_fd is not None:
        if not isinstance(input_fd, int) or isinstance(input_fd, bool) or input_fd < 3:
            raise LocalQAControlError("The private input descriptor must be at least 3")
        return _read_descriptor(input_fd)

    try:
        path = Path(os.fspath(input_file))
    except (TypeError, ValueError) as exc:
        raise LocalQAControlError("The private input parent chain is unsafe") from exc
    descriptor, directory_fd, parents_before, file_before = _open_private_path(path)
    try:
        request = _read_descriptor(descriptor)
        second_descriptor, second_directory, parents_after, file_after = _open_private_path(path)
        try:
            if parents_before != parents_after or file_before != file_after:
                raise LocalQAControlError("The private input changed during validation")
        finally:
            _close_private_path(second_descriptor, second_directory)
        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise LocalQAControlError("The private input changed during validation") from exc
        if file_before != _file_identity(current):
            raise LocalQAControlError("The private input file changed during validation")
        return request
    finally:
        _close_private_path(descriptor, directory_fd)


def _fault_parameters(boundary: str) -> dict[str, object]:
    gib = 1024**3
    available_memory = int(4.3 * gib)
    fixed: dict[str, dict[str, object]] = {
        "provider_auth_missing": {"failureClass": "provider_auth_missing", "retryable": False},
        "provider_quota_cooldown_fallback": {
            "failureClass": "provider_quota_exhausted",
            "cooldownSeconds": 120,
            "fallbackRequiredHealthy": True,
        },
        "provider_unavailable": {"failureClass": "provider_unavailable", "retryable": True},
        "provider_internal_retry_threshold": {
            "failureClass": "provider_progress_stalled",
            "internalRetryCount": 3,
            "needsInput": True,
            "releaseCompute": True,
        },
        "declared_long_fresh_then_stale": {
            "failureClass": "provider_progress_stalled",
            "longMission": True,
            "freshProgressExtends": True,
            "staleProgressNeedsInput": True,
        },
        "maximum_capacity_overflow": {"capacityClass": "mission_slots", "forceAtLimit": True},
        "measured_memory_4_3_gib_vs_5_gib": {
            "availableMemoryBytes": available_memory,
            "requiredMemoryBytes": 5 * gib,
            "shortageMemoryBytes": 5 * gib - available_memory,
            "reservationMemoryBytes": 3 * gib,
            "nextRetrySeconds": 5,
        },
        "last_reservation_competition": {
            "availableMemoryBytes": int(7.5 * gib),
            "requiredMemoryBytes": 5 * gib,
            "reservationMemoryBytes": 3 * gib,
        },
        "low_disk": {"availableDiskBytes": 512 * 1024**2, "probeHealthy": True},
        "callback_transport_interruption": {"transportOutcome": "interrupted_once"},
        "claimed_queue_stall": {"stallState": "claimed", "deadlineOutcome": "past_due"},
        "admitted_queue_stall": {"stallState": "admitted", "deadlineOutcome": "past_due"},
        "status_refresh_timeout_race": {"ordering": "timeout_before_refresh_cas"},
        "expired_sender_lease_race": {"ordering": "newer_lease_before_old_settlement"},
        "duplicate_callback_replay": {"deliveryCount": 2, "sameCallbackIdentity": True},
        "artifact_link_expired": {"artifactOutcome": "link_expired_once"},
        "artifact_unavailable_restart_recovery": {
            "artifactOutcome": "unavailable_once",
            "restartSafe": True,
        },
    }
    return {"faultClass": boundary, **fixed[boundary]}


class LocalQAControlPlane:
    """Durable, exact-scope, one-shot fault authority for installed local QA."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        artifact_digest: str | None = None,
        candidate_digest: str | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self._environment = environment if environment is not None else os.environ
        self._clock = clock
        binding = _artifact_binding_from_environment(self._environment)
        active_tuple = all(
            str(self._environment.get(key, "") or "").strip() for key in AUTHORITY_KEYS
        )
        if binding is None and (
            active_tuple
            or artifact_digest is not None
            or candidate_digest is not None
        ):
            raise LocalQAAuthorityError(
                "The canonical local-QA artifact binding is required"
            )
        if binding is not None:
            if artifact_digest is not None and not hmac.compare_digest(
                _clean_digest(artifact_digest), binding.component_artifact_digest
            ):
                raise LocalQAAuthorityError(
                    "The canonical local-QA artifact binding does not match"
                )
            if candidate_digest is not None and not hmac.compare_digest(
                _clean_digest(candidate_digest), binding.candidate_digest
            ):
                raise LocalQAAuthorityError(
                    "The canonical local-QA artifact binding does not match"
                )
        self._candidate_digest = binding.candidate_digest if binding is not None else ""
        self._artifact_digest = (
            binding.component_artifact_digest if binding is not None else ""
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        descriptor = directory_descriptor = -1
        second_descriptor = second_directory = -1
        connection: sqlite3.Connection | None = None
        try:
            descriptor, directory_descriptor, parents_before, file_before = (
                _open_database_path(self.db_path, create=True)
            )
            uri = "file:" + quote(os.fspath(self.db_path), safe="/") + "?mode=rw&nofollow=1"
            connection = sqlite3.connect(
                uri,
                timeout=30,
                isolation_level=None,
                uri=True,
            )
            second_descriptor, second_directory, parents_after, file_after = (
                _open_database_path(self.db_path, create=False)
            )
            stable_parents_before = tuple(identity[:5] for identity in parents_before)
            stable_parents_after = tuple(identity[:5] for identity in parents_after)
            if (
                file_before[:6] != file_after[:6]
                or stable_parents_before != stable_parents_after
            ):
                raise LocalQAControlError("The local-QA database identity changed")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            return connection
        except (OSError, RuntimeError, sqlite3.Error, LocalQAControlError) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, LocalQAControlError):
                raise
            raise LocalQAControlError("The local-QA database is unavailable") from exc
        finally:
            if descriptor >= 0:
                _close_database_path(descriptor, directory_descriptor)
            if second_descriptor >= 0:
                _close_database_path(second_descriptor, second_directory)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_qa_fault_controls (
                    control_ref TEXT PRIMARY KEY,
                    contract_version INTEGER NOT NULL CHECK (contract_version = 1),
                    case_id TEXT NOT NULL,
                    case_mode TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    component_artifact_digest TEXT NOT NULL,
                    boundary TEXT NOT NULL,
                    owner_hash TEXT NOT NULL,
                    work_hash TEXT NOT NULL,
                    run_hash TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('armed', 'consumed', 'cleared', 'expired')),
                    armed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    cleared_at TEXT,
                    consumption_count INTEGER NOT NULL DEFAULT 0 CHECK (consumption_count IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS local_qa_fault_audit (
                    audit_ref TEXT PRIMARY KEY,
                    control_ref TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    boundary TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_qa_fault_arm_ledger (
                    arm_identity_hash TEXT PRIMARY KEY,
                    idempotency_hash TEXT NOT NULL,
                    control_ref TEXT NOT NULL UNIQUE,
                    ttl_seconds INTEGER NOT NULL CHECK (ttl_seconds BETWEEN 1 AND 3600),
                    status TEXT NOT NULL
                        CHECK (status IN ('armed', 'consumed', 'cleared', 'expired')),
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_local_qa_fault_audit_control
                    ON local_qa_fault_audit (control_ref, occurred_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(local_qa_fault_controls)"
                ).fetchall()
            }
            if "candidate_digest" not in columns:
                connection.execute(
                    "ALTER TABLE local_qa_fault_controls "
                    "ADD COLUMN candidate_digest TEXT NOT NULL DEFAULT ''"
                )
            connection.executescript(
                """
                DROP INDEX IF EXISTS idx_local_qa_fault_exact_consume;
                DROP INDEX IF EXISTS idx_local_qa_fault_one_live_exact;
                CREATE INDEX idx_local_qa_fault_exact_consume
                    ON local_qa_fault_controls (
                        case_id, case_mode, token_hash, session_hash,
                        candidate_digest, component_artifact_digest, boundary,
                        owner_hash, work_hash, run_hash, artifact_hash,
                        status, expires_at
                    );
                CREATE INDEX IF NOT EXISTS idx_local_qa_fault_session
                    ON local_qa_fault_controls (case_id, token_hash, session_hash, armed_at);
                CREATE UNIQUE INDEX idx_local_qa_fault_one_live_exact
                    ON local_qa_fault_controls (
                        case_id, case_mode, token_hash, session_hash,
                        candidate_digest, component_artifact_digest,
                        boundary, owner_hash, work_hash, run_hash, artifact_hash
                    ) WHERE status = 'armed';
                """
            )

    @staticmethod
    def _synthetic_fixture(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        work_id: str,
        run_id: str,
        artifact_id: str,
        boundary: str,
    ) -> tuple[str, str, str]:
        try:
            row = connection.execute(
                """
                SELECT d.idempotency_key, d.origin_ref, d.title,
                       d.origin_surface, d.current_run_id,
                       w.role, w.model, r.instruction
                FROM delegations AS d
                JOIN projects AS p
                  ON p.project_id = d.project_id
                 AND p.tenant_id = d.tenant_id
                 AND p.owner_id = d.owner_id
                JOIN workers AS w
                  ON w.worker_id = d.worker_id
                 AND w.project_id = d.project_id
                 AND w.tenant_id = d.tenant_id
                 AND w.owner_id = d.owner_id
                JOIN runs AS r
                  ON r.run_id = d.current_run_id
                 AND r.worker_id = d.worker_id
                 AND r.project_id = d.project_id
                 AND r.tenant_id = d.tenant_id
                WHERE d.work_ref = ? AND d.tenant_id = 'local'
                  AND d.owner_id = ?
                """,
                (work_id, owner_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise LocalQAControlError(
                "The exact synthetic fixture is unavailable"
            ) from exc
        if row is None:
            raise LocalQAControlError("The exact synthetic fixture is unavailable")
        idempotency_key = str(row["idempotency_key"] or "")
        current_run_id = str(row["current_run_id"] or "")
        if (
            not idempotency_key.startswith("qa_idem_")
            or not str(row["origin_ref"] or "").startswith("qa_origin_")
            or str(row["title"] or "")
            != "Synthetic Parallel Work local-QA fixture"
            or str(row["origin_surface"] or "") != "workbench"
            or str(row["role"] or "") != "deterministic fixture"
            or str(row["model"] or "") != "synthetic"
            or str(row["instruction"] or "")
            != "Synthetic local-QA fixture. Do not perform external work."
            or (boundary in RUN_SCOPED_FAULTS and run_id != current_run_id)
        ):
            raise LocalQAControlError("The exact synthetic fixture is unavailable")
        try:
            event = connection.execute(
                """
                SELECT payload_json FROM work_trace_events
                WHERE run_id = ? AND work_ref = ?
                  AND tenant_id = 'local' AND owner_id = ?
                  AND event_type = 'artifact.observed'
                ORDER BY sequence DESC LIMIT 1
                """,
                (current_run_id, work_id, owner_id),
            ).fetchone()
            payload = (
                json.loads(
                    str(event["payload_json"] or "{}"),
                    object_pairs_hook=_unique_json_object,
                )
                if event is not None
                else None
            )
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LocalQAControlError(
                "The exact synthetic fixture is unavailable"
            ) from exc
        refs = payload.get("artifactRefs") if isinstance(payload, dict) else None
        items = refs.get("refs") if isinstance(refs, dict) else None
        if (
            not isinstance(refs, dict)
            or set(refs) != {"available", "overflowCount", "refs"}
            or refs.get("available") is not True
            or refs.get("overflowCount") != 0
            or not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
        ):
            raise LocalQAControlError("The exact synthetic fixture is unavailable")
        artifact = items[0]
        fixture_artifact_id = str(artifact.get("artifactRef") or "")
        fingerprint = str(artifact.get("fingerprint") or "")
        if (
            set(artifact)
            != {"artifactRef", "fingerprint", "kind", "sizeBytes", "state"}
            or not fixture_artifact_id.startswith("artifact_sha256:")
            or len(fixture_artifact_id) != len("artifact_sha256:") + 64
            or fingerprint
            != "sha256:" + fixture_artifact_id.removeprefix("artifact_sha256:")
            or str(artifact.get("kind") or "") != "text"
            or str(artifact.get("state") or "") != "ready"
            or isinstance(artifact.get("sizeBytes"), bool)
            or not isinstance(artifact.get("sizeBytes"), int)
            or int(artifact["sizeBytes"]) < 0
            or (
                boundary in ARTIFACT_SCOPED_FAULTS
                and artifact_id != fixture_artifact_id
            )
        ):
            raise LocalQAControlError("The exact synthetic fixture is unavailable")
        return idempotency_key, current_run_id, fixture_artifact_id

    @staticmethod
    def _arm_identity(
        *,
        authority: LocalQAAuthority,
        token_hash: str,
        session_hash: str,
        candidate_digest: str,
        component_artifact_digest: str,
        boundary: str,
        scope: Mapping[str, str],
        idempotency_key: str,
        fixture_run_id: str,
        fixture_artifact_id: str,
    ) -> tuple[str, str]:
        idempotency_hash = _hash_private("fixture-idempotency", idempotency_key)
        canonical = json.dumps(
            {
                "artifact": scope["artifact"],
                "boundary": boundary,
                "candidate": candidate_digest,
                "case": authority.case_id,
                "component": component_artifact_digest,
                "fixtureArtifact": _hash_private("fixture-artifact", fixture_artifact_id),
                "fixtureRun": _hash_private("fixture-run", fixture_run_id),
                "idempotency": idempotency_hash,
                "mode": authority.case_mode,
                "owner": scope["owner"],
                "run": scope["run"],
                "session": session_hash,
                "token": token_hash,
                "work": scope["work"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _hash_private("arm-identity", canonical), idempotency_hash

    def _runtime_authority(self) -> LocalQAAuthority | None:
        return _authority_from_environment(self._environment)

    @staticmethod
    def _base_request(
        request: Mapping[str, object], *, exact_fields: frozenset[str]
    ) -> LocalQAAuthority:
        if set(request) != set(exact_fields):
            raise LocalQAControlError("The private request fields are invalid")
        if request.get("contractVersion") != 1 or isinstance(request.get("contractVersion"), bool):
            raise LocalQAControlError("contractVersion is invalid")
        case_id = _clean_private_identity(request.get("caseId"), "caseId", required=True)
        token = _clean_private_identity(request.get("caseToken"), "caseToken", required=True)
        session_ref = _clean_private_identity(
            request.get("sessionRef"), "sessionRef", required=True
        )
        if case_id not in CASE_MODES or len(token) < 32 or len(session_ref) < 16:
            raise LocalQAAuthorityError("The private local-QA authority is invalid")
        return LocalQAAuthority(case_id, CASE_MODES[case_id], token, session_ref)

    @staticmethod
    def _authority_hashes(authority: LocalQAAuthority) -> tuple[str, str]:
        return (
            _hash_private("case-token", authority.case_token),
            _hash_private("session", authority.session_ref),
        )

    def _require_active_authority(self, request_authority: LocalQAAuthority) -> LocalQAAuthority:
        runtime = self._runtime_authority()
        if runtime is None:
            raise LocalQAAuthorityError("The canonical local-QA authority is inactive")
        exact = bool(
            runtime.case_id == request_authority.case_id
            and runtime.case_mode == request_authority.case_mode
            and hmac.compare_digest(runtime.case_token, request_authority.case_token)
            and hmac.compare_digest(runtime.session_ref, request_authority.session_ref)
        )
        if not exact:
            raise LocalQAAuthorityError(
                "The private request does not match the canonical local-QA authority"
            )
        return runtime

    @staticmethod
    def _request_artifact_binding(
        request: Mapping[str, object],
    ) -> LocalQAArtifactBinding:
        return LocalQAArtifactBinding(
            _clean_digest(request.get("candidateDigest")),
            _clean_digest(request.get("componentArtifactDigest")),
        )

    def _require_current_artifact_binding(
        self, request: Mapping[str, object]
    ) -> LocalQAArtifactBinding:
        requested = self._request_artifact_binding(request)
        if (
            not self._candidate_digest
            or not self._artifact_digest
            or not hmac.compare_digest(
                requested.candidate_digest, self._candidate_digest
            )
            or not hmac.compare_digest(
                requested.component_artifact_digest, self._artifact_digest
            )
        ):
            raise LocalQAAuthorityError(
                "The private request artifact binding does not match the canonical binding"
            )
        return requested

    @staticmethod
    def _scope_hashes(
        *, owner_id: str, work_id: str, run_id: str, artifact_id: str
    ) -> dict[str, str]:
        return {
            "owner": _hash_private("owner", owner_id),
            "work": _hash_private("work", work_id),
            "run": _hash_private("run", run_id) if run_id else "",
            "artifact": _hash_private("artifact", artifact_id) if artifact_id else "",
        }

    @staticmethod
    def _receipt(row: Mapping[str, object], *, operation: str = "arm") -> dict[str, object]:
        return {
            "contractVersion": 1,
            "operation": operation,
            "status": str(row["status"]),
            "controlRef": str(row["control_ref"]),
            "caseId": str(row["case_id"]),
            "boundary": str(row["boundary"]),
            "expiresAt": str(row["expires_at"]),
            "scopeHashes": {
                "owner": str(row["owner_hash"]),
                "work": str(row["work_hash"]),
                "run": str(row["run_hash"]),
                "artifact": str(row["artifact_hash"]),
            },
        }

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        control_ref: str,
        case_id: str,
        boundary: str,
        action: str,
        status: str,
        occurred_at: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO local_qa_fault_audit (
                audit_ref, control_ref, case_id, boundary, action,
                status, occurred_at, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "qaa_sha256:" + secrets.token_hex(32),
                control_ref,
                case_id,
                boundary,
                action,
                status,
                occurred_at,
                json.dumps(dict(detail or {}), sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _set_ledger_status(
        connection: sqlite3.Connection, *, control_ref: str, status: str
    ) -> None:
        connection.execute(
            """
            UPDATE local_qa_fault_arm_ledger
            SET status = ? WHERE control_ref = ?
            """,
            (status, control_ref),
        )

    def _apply_selected_liveness_boundary(
        self,
        connection: sqlite3.Connection,
        *,
        authority: LocalQAAuthority,
        boundary: str,
        control_ref: str,
        owner_id: str,
        work_id: str,
        run_id: str,
        now: str,
    ) -> None:
        if boundary not in SUPPORTED_FAULTS["PWK-UC-016"]:
            return
        worker_columns = {
            str(item[1]) for item in connection.execute("PRAGMA table_info(workers)")
        }
        run_columns = {
            str(item[1]) for item in connection.execute("PRAGMA table_info(runs)")
        }
        required_worker_columns = {
            "profile", "runtime", "model", "workspace_dir", "workspace_root",
            "state", "compute_released_at", "compute_release_kind",
            "compute_release_target_run_id", "compute_release_runtime_confirmed_at",
            "last_run_id", "last_error",
        }
        required_run_columns = {
            "native_session_id", "state", "started_at", "runtime_invoked_at",
            "active_attempt_id", "ended_at", "failure_class", "failure_retryable",
            "failure_structured", "failure_user_message",
            "failure_recommended_recovery", "failure_diagnostic_summary",
            "retry_after", "liveness_started_at", "meaningful_progress_at",
            "meaningful_progress_sequence", "internal_retry_count",
            "last_internal_retry_class", "liveness_mode",
            "provider_liveness_route_locked", "provider_route_profile",
            "provider_route_runtime", "provider_route_model",
            "provider_route_decision", "provider_route_from_profile",
            "provider_route_from_runtime", "provider_route_from_model",
            "provider_route_failure_class", "provider_route_cooldown_until",
        }
        if not required_worker_columns.issubset(worker_columns) or not required_run_columns.issubset(run_columns):
            return
        row = connection.execute(
            """
            SELECT d.project_id, d.worker_id, w.profile, w.runtime, w.model,
                   w.workspace_dir, w.workspace_root, r.native_session_id
            FROM delegations AS d
            JOIN workers AS w ON w.worker_id = d.worker_id
            JOIN runs AS r ON r.run_id = d.current_run_id
            WHERE d.work_ref = ? AND d.owner_id = ? AND d.current_run_id = ?
            """,
            (work_id, owner_id, run_id),
        ).fetchone()
        if row is None:
            raise LocalQAControlError("The exact synthetic fixture is unavailable")
        current = datetime.fromisoformat(now)
        identity = hashlib.sha256(f"{control_ref}\0{boundary}".encode()).hexdigest()
        if boundary not in {
            "provider_internal_retry_threshold",
            "declared_long_fresh_then_stale",
        }:
            parameters = _fault_parameters(boundary)
            if boundary in {"provider_auth_missing", "provider_unavailable"}:
                failure_class = str(parameters["failureClass"])
                connection.execute(
                    """
                    INSERT INTO events (
                        event_id, project_id, worker_id, tenant_id, run_id,
                        event_type, message, payload_json, created_at
                    ) VALUES (?, ?, ?, 'local', ?, 'run.provider_failure', ?, ?, ?)
                    """,
                    (
                        "evt_" + identity,
                        row["project_id"],
                        row["worker_id"],
                        run_id,
                        "Synthetic provider failure boundary",
                        json.dumps(
                            {"failureClass": failure_class},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )
            elif boundary == "provider_quota_cooldown_fallback":
                cooldown_until = _iso(current + timedelta(seconds=120))
                fallback_profile = str(row["profile"] or "synthetic") + "-fallback"
                connection.execute(
                    """
                    INSERT INTO provider_route_health (
                        tenant_id, owner_id, profile, runtime, model,
                        failure_class, failure_count, failure_generation,
                        first_failed_at, last_failed_at, cooldown_until,
                        cooldown_source, last_run_id, updated_at
                    ) VALUES (
                        'local', ?, ?, ?, ?, 'provider_quota_exhausted',
                        1, 1, ?, ?, ?, 'retry_after', ?, ?
                    )
                    ON CONFLICT (tenant_id, owner_id, profile, runtime, model)
                    DO UPDATE SET failure_class = excluded.failure_class,
                                  cooldown_until = excluded.cooldown_until,
                                  last_run_id = excluded.last_run_id,
                                  updated_at = excluded.updated_at
                    """,
                    (
                        owner_id,
                        row["profile"],
                        row["runtime"],
                        row["model"],
                        now,
                        now,
                        cooldown_until,
                        run_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE runs SET provider_route_decision = 'fallback_selected',
                        provider_route_from_profile = ?,
                        provider_route_from_runtime = ?,
                        provider_route_from_model = ?,
                        provider_route_profile = ?, provider_route_runtime = ?,
                        provider_route_model = ?,
                        provider_route_failure_class = 'provider_quota_exhausted',
                        provider_route_cooldown_until = ?
                    WHERE run_id = ?
                    """,
                    (
                        row["profile"],
                        row["runtime"],
                        row["model"],
                        fallback_profile,
                        row["runtime"],
                        row["model"],
                        cooldown_until,
                        run_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        event_id, project_id, worker_id, tenant_id, run_id,
                        event_type, message, payload_json, created_at
                    ) VALUES (?, ?, ?, 'local', ?, 'run.provider_route_skipped', ?, ?, ?)
                    """,
                    (
                        "evt_" + identity,
                        row["project_id"],
                        row["worker_id"],
                        run_id,
                        "Synthetic cooldown skipped the unavailable route",
                        json.dumps(
                            {"failureClass": "provider_quota_exhausted"},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )
            else:
                gib = 1024**3
                if boundary == "maximum_capacity_overflow":
                    capacity_class = "mission_slots"
                    available = {"missionSlots": 0}
                    required = {"missionSlots": 1}
                    shortage = {"missionSlots": 1}
                    reservation = {}
                elif boundary == "measured_memory_4_3_gib_vs_5_gib":
                    capacity_class = "resource_pressure"
                    available = {"memoryBytes": int(parameters["availableMemoryBytes"])}
                    required = {"memoryBytes": int(parameters["requiredMemoryBytes"])}
                    shortage = {"memoryBytes": int(parameters["shortageMemoryBytes"])}
                    reservation = {"memoryBytes": int(parameters["reservationMemoryBytes"])}
                elif boundary == "last_reservation_competition":
                    capacity_class = "admission_reserved"
                    available = {"memoryBytes": int(parameters["availableMemoryBytes"])}
                    required = {"memoryBytes": int(parameters["requiredMemoryBytes"])}
                    shortage = {}
                    reservation = {"memoryBytes": int(parameters["reservationMemoryBytes"])}
                else:
                    capacity_class = "resource_pressure"
                    available = {"diskBytes": int(parameters["availableDiskBytes"])}
                    required = {"diskBytes": 1024**3}
                    shortage = {"diskBytes": 512 * 1024**2}
                    reservation = {}
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM capacity_attempts WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                ) + 1
                connection.execute(
                    """
                    INSERT INTO capacity_attempts (
                        capacity_attempt_id, run_id, sequence, capacity_class,
                        available_json, required_json, shortage_json,
                        reservation_json, next_retry_at, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "qa_capacity_" + identity,
                        run_id,
                        sequence,
                        capacity_class,
                        json.dumps(available, sort_keys=True, separators=(",", ":")),
                        json.dumps(required, sort_keys=True, separators=(",", ":")),
                        json.dumps(shortage, sort_keys=True, separators=(",", ":")),
                        json.dumps(reservation, sort_keys=True, separators=(",", ":")),
                        _iso(current + timedelta(seconds=5)),
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE local_qa_fault_controls
                SET status = 'consumed', consumed_at = ?, consumption_count = 1
                WHERE control_ref = ? AND status = 'armed'
                """,
                (now, control_ref),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LocalQAControlError("The exact one-shot control was already finalized")
            self._set_ledger_status(connection, control_ref=control_ref, status="consumed")
            for action, status in (("consumed", "consumed"), ("effect_applied", "applied")):
                self._audit(
                    connection,
                    control_ref=control_ref,
                    case_id=authority.case_id,
                    boundary=boundary,
                    action=action,
                    status=status,
                    occurred_at=now,
                    detail={"effect": "selected_fixture_deterministic_boundary"},
                )
            return
        attempt_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) FROM run_attempts WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        ) + 1
        attempt_id = f"qa_attempt_{identity}"
        lease_id = f"qa_lease_{identity}"
        session_id = str(row["native_session_id"] or f"qa_session_{identity[:32]}")
        if boundary == "provider_internal_retry_threshold":
            started = current - timedelta(seconds=3)
            progress_at = None
            progress_sequence = 0
            retry_count = 3
            liveness_mode = "standard"
            liveness_events = [
                ("internal_retry", index, current - timedelta(seconds=3 - index))
                for index in range(1, 4)
            ]
        else:
            started = current - timedelta(seconds=1802)
            progress = current - timedelta(seconds=901)
            progress_at = _iso(progress)
            progress_sequence = 1
            retry_count = 0
            liveness_mode = "declared_long"
            liveness_events = [
                ("meaningful_progress", 1, progress),
                ("progress_stalled", 2, current),
            ]
        connection.execute("DELETE FROM host_run_leases WHERE run_id = ?", (run_id,))
        connection.execute(
            """
            INSERT INTO run_attempts (
                attempt_id, run_id, attempt_number, state, claimed_at,
                admitted_at, runtime_invoked_at, ended_at, lease_id,
                terminal_reason
            ) VALUES (?, ?, ?, 'needs_input', ?, ?, ?, ?, ?, 'provider_progress_stalled')
            """,
            (
                attempt_id,
                run_id,
                attempt_number,
                _iso(started),
                _iso(started),
                _iso(started),
                now,
                lease_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO host_run_leases (
                lease_id, runtime_family, lane, tenant_id, owner_id,
                worker_id, run_id, executor_id, startup_state,
                startup_confirmed_at, startup_identity_kind,
                startup_session_id, attempt_id, status, acquired_at,
                heartbeat_at, expires_at, released_at, release_reason
            ) VALUES (
                ?, 'synthetic', 'mission', 'local', ?, ?, ?,
                'qa_fixture_executor', 'confirmed', ?, 'synthetic', ?, ?,
                'released', ?, ?, ?, ?, 'provider_progress_stalled'
            )
            """,
            (
                lease_id,
                owner_id,
                row["worker_id"],
                run_id,
                _iso(started),
                session_id,
                attempt_id,
                _iso(started),
                now,
                now,
                now,
            ),
        )
        for kind, sequence, observed in liveness_events:
            event_ref = "qa_liveness_" + hashlib.sha256(
                f"{control_ref}\0{kind}\0{sequence}".encode()
            ).hexdigest()
            source_digest = "sha256:" + hashlib.sha256(
                f"{boundary}\0{kind}\0{sequence}".encode()
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO provider_liveness_events (
                    event_ref, run_id, attempt_id, kind, failure_class,
                    runtime, model, source_sequence, source_digest,
                    observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_ref,
                    run_id,
                    attempt_id,
                    kind,
                    "provider_progress_stalled" if kind == "progress_stalled" else "",
                    row["runtime"],
                    row["model"],
                    sequence,
                    source_digest,
                    _iso(observed),
                    now,
                ),
            )
        connection.execute(
            """
            UPDATE runs
            SET state = 'needs_input', started_at = COALESCE(started_at, ?),
                runtime_invoked_at = ?, active_attempt_id = ?, ended_at = ?,
                failure_class = 'provider_progress_stalled', failure_retryable = 1,
                failure_structured = 1,
                failure_user_message = 'Provider progress stalled. Resume this exact work when ready.',
                failure_recommended_recovery = 'resume',
                failure_diagnostic_summary = 'Synthetic local-QA provider liveness boundary.',
                retry_after = NULL, native_session_id = ?,
                liveness_started_at = ?, meaningful_progress_at = ?,
                meaningful_progress_sequence = ?, internal_retry_count = ?,
                last_internal_retry_class = ?, liveness_mode = ?,
                provider_liveness_route_locked = 1,
                provider_route_profile = ?, provider_route_runtime = ?,
                provider_route_model = ?, provider_route_decision = 'locked'
            WHERE run_id = ? AND worker_id = ? AND project_id = ?
            """,
            (
                _iso(started),
                _iso(started),
                attempt_id,
                now,
                session_id,
                _iso(started),
                progress_at,
                progress_sequence,
                retry_count,
                "provider_internal_retry" if retry_count else "",
                liveness_mode,
                row["profile"],
                row["runtime"],
                row["model"],
                run_id,
                row["worker_id"],
                row["project_id"],
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LocalQAControlError("The exact synthetic fixture is unavailable")
        connection.execute(
            """
            UPDATE workers
            SET state = 'needs_input', compute_released_at = ?,
                compute_release_kind = 'needs_input',
                compute_release_target_run_id = ?,
                compute_release_runtime_confirmed_at = ?,
                last_run_id = ?, last_error = 'Provider progress stalled'
            WHERE worker_id = ? AND project_id = ? AND owner_id = ?
            """,
            (now, run_id, now, run_id, row["worker_id"], row["project_id"], owner_id),
        )
        connection.execute(
            """
            INSERT INTO events (
                event_id, project_id, worker_id, tenant_id, run_id,
                event_type, message, payload_json, created_at
            ) VALUES (?, ?, ?, 'local', ?, 'run.needs_input', ?, ?, ?)
            """,
            (
                "evt_" + identity,
                row["project_id"],
                row["worker_id"],
                run_id,
                "Provider progress stalled; compute released",
                json.dumps(
                    {
                        "failureClass": "provider_progress_stalled",
                        "internalRetryCount": retry_count,
                        "livenessMode": liveness_mode,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                now,
            ),
        )
        connection.execute(
            """
            UPDATE local_qa_fault_controls
            SET status = 'consumed', consumed_at = ?, consumption_count = 1
            WHERE control_ref = ? AND status = 'armed'
            """,
            (now, control_ref),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LocalQAControlError("The exact one-shot control was already finalized")
        self._set_ledger_status(connection, control_ref=control_ref, status="consumed")
        for action, status in (("consumed", "consumed"), ("effect_applied", "applied")):
            self._audit(
                connection,
                control_ref=control_ref,
                case_id=authority.case_id,
                boundary=boundary,
                action=action,
                status=status,
                occurred_at=now,
                detail={"effect": "selected_fixture_provider_liveness"},
            )

    def arm(self, request: Mapping[str, object]) -> dict[str, object]:
        selected_owner_scope = request.get("scopeKind") == "selected_synthetic_account_qa"
        request_authority = self._base_request(
            request,
            exact_fields=(
                _SELECTED_OWNER_ARM_FIELDS if selected_owner_scope else _ARM_FIELDS
            ),
        )
        authority = self._require_active_authority(request_authority)
        self._require_current_artifact_binding(request)
        if request.get("scopeKind") not in {
            "synthetic_local_qa",
            "selected_synthetic_account_qa",
        }:
            raise LocalQAControlError("scopeKind must identify exact synthetic local QA")
        boundary = _clean_private_identity(request.get("boundary"), "boundary", required=True)
        if boundary not in SUPPORTED_FAULTS[authority.case_id]:
            raise LocalQAControlError("The fault boundary is not valid for this case")
        owner_id = _clean_private_identity(request.get("ownerId"), "ownerId", required=True)
        work_id = _clean_private_identity(request.get("workId"), "workId", required=True)
        run_id = _clean_private_identity(
            request.get("runId"), "runId", required=boundary in RUN_SCOPED_FAULTS
        )
        artifact_id = _clean_private_identity(
            request.get("artifactId"),
            "artifactId",
            required=boundary in ARTIFACT_SCOPED_FAULTS,
        )
        if boundary not in RUN_SCOPED_FAULTS and run_id:
            raise LocalQAControlError("runId is not valid for this fault boundary")
        if boundary not in ARTIFACT_SCOPED_FAULTS and artifact_id:
            raise LocalQAControlError("artifactId is not valid for this fault boundary")
        if request.get("parameters") != {}:
            raise LocalQAControlError("parameters must be the exact empty typed contract")
        if selected_owner_scope == owner_id.startswith("qa_owner_"):
            raise LocalQAControlError("The selected fixture owner scope is invalid")
        ttl_seconds = request.get("ttlSeconds")
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 1 <= ttl_seconds <= 3600
        ):
            raise LocalQAControlError("ttlSeconds must be an integer from 1 through 3600")
        current = self._clock()
        now = _iso(current)
        expires_at = _iso(current + timedelta(seconds=ttl_seconds))
        token_hash, session_hash = self._authority_hashes(authority)
        scope = self._scope_hashes(
            owner_id=owner_id,
            work_id=work_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_session_controls(
                connection,
                authority=authority,
                token_hash=token_hash,
                session_hash=session_hash,
                binding=LocalQAArtifactBinding(
                    self._candidate_digest, self._artifact_digest
                ),
                now=now,
            )
            idempotency_key, fixture_run_id, fixture_artifact_id = (
                self._synthetic_fixture(
                    connection,
                    owner_id=owner_id,
                    work_id=work_id,
                    run_id=run_id,
                    artifact_id=artifact_id,
                    boundary=boundary,
                )
            )
            if selected_owner_scope:
                attested_scope = {
                    "artifactId": fixture_artifact_id,
                    "candidateDigest": self._candidate_digest,
                    "caseId": authority.case_id,
                    "componentArtifactDigest": self._artifact_digest,
                    "ownerId": owner_id,
                    "runId": fixture_run_id,
                    "sessionRef": authority.session_ref,
                    "workId": work_id,
                }
                canonical = json.dumps(
                    attested_scope, sort_keys=True, separators=(",", ":")
                )
                expected_attestation = "sha256:" + hmac.new(
                    authority.case_token.encode("utf-8"),
                    ("glasshive-fixture-control-v1\0" + canonical).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(
                    str(request.get("fixtureAttestation") or ""),
                    expected_attestation,
                ):
                    raise LocalQAControlError(
                        "The selected fixture owner attestation is invalid"
                    )
            arm_identity, idempotency_hash = self._arm_identity(
                authority=authority,
                token_hash=token_hash,
                session_hash=session_hash,
                candidate_digest=self._candidate_digest,
                component_artifact_digest=self._artifact_digest,
                boundary=boundary,
                scope=scope,
                idempotency_key=idempotency_key,
                fixture_run_id=fixture_run_id,
                fixture_artifact_id=fixture_artifact_id,
            )
            ledger = connection.execute(
                """
                SELECT * FROM local_qa_fault_arm_ledger
                WHERE arm_identity_hash = ?
                """,
                (arm_identity,),
            ).fetchone()
            if ledger is not None:
                if int(ledger["ttl_seconds"]) != ttl_seconds:
                    connection.execute("ROLLBACK")
                    raise LocalQAControlError(
                        "ttlSeconds conflicts with the existing exact control"
                    )
                existing = connection.execute(
                    "SELECT * FROM local_qa_fault_controls WHERE control_ref = ?",
                    (ledger["control_ref"],),
                ).fetchone()
                if existing is None:
                    connection.execute("ROLLBACK")
                    raise LocalQAControlError(
                        "The exact one-shot control was already finalized"
                    )
                connection.execute("COMMIT")
                result = self._receipt(existing)
                if result["status"] == "armed":
                    result["status"] = "already_armed"
                return result
            existing = connection.execute(
                """
                SELECT * FROM local_qa_fault_controls
                WHERE case_id = ? AND case_mode = ?
                  AND token_hash = ? AND session_hash = ?
                  AND candidate_digest = ?
                  AND component_artifact_digest = ? AND boundary = ?
                  AND owner_hash = ? AND work_hash = ?
                  AND run_hash = ? AND artifact_hash = ?
                ORDER BY armed_at ASC, control_ref ASC
                LIMIT 1
                """,
                (
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    self._candidate_digest,
                    self._artifact_digest,
                    boundary,
                    scope["owner"],
                    scope["work"],
                    scope["run"],
                    scope["artifact"],
                ),
            ).fetchone()
            if existing is not None:
                original_ttl = int(
                    (
                        datetime.fromisoformat(str(existing["expires_at"]))
                        - datetime.fromisoformat(str(existing["armed_at"]))
                    ).total_seconds()
                )
                if original_ttl != ttl_seconds:
                    connection.execute("ROLLBACK")
                    raise LocalQAControlError(
                        "ttlSeconds conflicts with the existing exact armed control"
                    )
                connection.execute(
                    """
                    INSERT INTO local_qa_fault_arm_ledger (
                        arm_identity_hash, idempotency_hash, control_ref,
                        ttl_seconds, status, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        arm_identity,
                        idempotency_hash,
                        existing["control_ref"],
                        ttl_seconds,
                        existing["status"],
                        existing["expires_at"],
                    ),
                )
                connection.execute("COMMIT")
                result = self._receipt(existing)
                if result["status"] == "armed":
                    result["status"] = "already_armed"
                return result
            control_ref = "qac_sha256:" + secrets.token_hex(32)
            connection.execute(
                """
                INSERT INTO local_qa_fault_controls (
                    control_ref, contract_version, case_id, case_mode,
                    token_hash, session_hash, candidate_digest,
                    component_artifact_digest,
                    boundary, owner_hash, work_hash, run_hash, artifact_hash,
                    parameters_json, status, armed_at, expires_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'armed', ?, ?)
                """,
                (
                    control_ref,
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    self._candidate_digest,
                    self._artifact_digest,
                    boundary,
                    scope["owner"],
                    scope["work"],
                    scope["run"],
                    scope["artifact"],
                    json.dumps(_fault_parameters(boundary), sort_keys=True, separators=(",", ":")),
                    now,
                    expires_at,
                ),
            )
            self._audit(
                connection,
                control_ref=control_ref,
                case_id=authority.case_id,
                boundary=boundary,
                action="armed",
                status="armed",
                occurred_at=now,
            )
            connection.execute(
                """
                INSERT INTO local_qa_fault_arm_ledger (
                    arm_identity_hash, idempotency_hash, control_ref,
                    ttl_seconds, status, expires_at
                ) VALUES (?, ?, ?, ?, 'armed', ?)
                """,
                (
                    arm_identity,
                    idempotency_hash,
                    control_ref,
                    ttl_seconds,
                    expires_at,
                ),
            )
            if selected_owner_scope and authority.case_id == "PWK-UC-016":
                self._apply_selected_liveness_boundary(
                    connection,
                    authority=authority,
                    boundary=boundary,
                    control_ref=control_ref,
                    owner_id=owner_id,
                    work_id=work_id,
                    run_id=fixture_run_id,
                    now=now,
                )
            row = connection.execute(
                "SELECT * FROM local_qa_fault_controls WHERE control_ref = ?", (control_ref,)
            ).fetchone()
            connection.execute("COMMIT")
        return self._receipt(row)

    def consume(
        self,
        boundary: str,
        *,
        owner_id: str,
        work_id: str,
        run_id: str = "",
        artifact_id: str = "",
    ) -> LocalQAFaultDirective | None:
        """Atomically consume the one exact current control, or do nothing."""

        try:
            authority = self._runtime_authority()
        except LocalQAAuthorityError:
            return None
        if (
            authority is None
            or not self._candidate_digest
            or not self._artifact_digest
            or boundary not in SUPPORTED_FAULTS.get(authority.case_id, ())
        ):
            return None
        try:
            clean_owner = _clean_private_identity(owner_id, "ownerId", required=True)
            clean_work = _clean_private_identity(work_id, "workId", required=True)
            clean_run = _clean_private_identity(
                run_id, "runId", required=boundary in RUN_SCOPED_FAULTS
            )
            clean_artifact = _clean_private_identity(
                artifact_id,
                "artifactId",
                required=boundary in ARTIFACT_SCOPED_FAULTS,
            )
        except LocalQAControlError:
            return None
        scope = self._scope_hashes(
            owner_id=clean_owner,
            work_id=clean_work,
            run_id=clean_run,
            artifact_id=clean_artifact,
        )
        token_hash, session_hash = self._authority_hashes(authority)
        now = _iso(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """
                SELECT control_ref, case_id, boundary
                FROM local_qa_fault_controls
                WHERE case_id = ? AND case_mode = ?
                  AND token_hash = ? AND session_hash = ?
                  AND candidate_digest = ? AND component_artifact_digest = ?
                  AND status = 'armed' AND expires_at <= ?
                """,
                (
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    self._candidate_digest,
                    self._artifact_digest,
                    now,
                ),
            ).fetchall()
            for row in expired:
                connection.execute(
                    """
                    UPDATE local_qa_fault_controls
                    SET status = 'expired', cleared_at = ?
                    WHERE control_ref = ? AND status = 'armed' AND expires_at <= ?
                    """,
                    (now, row["control_ref"], now),
                )
                if connection.execute("SELECT changes()").fetchone()[0]:
                    self._set_ledger_status(
                        connection,
                        control_ref=str(row["control_ref"]),
                        status="expired",
                    )
                    self._audit(
                        connection,
                        control_ref=str(row["control_ref"]),
                        case_id=str(row["case_id"]),
                        boundary=str(row["boundary"]),
                        action="expired",
                        status="expired",
                        occurred_at=now,
                    )
            row = connection.execute(
                """
                SELECT * FROM local_qa_fault_controls
                WHERE case_id = ? AND case_mode = ?
                  AND token_hash = ? AND session_hash = ?
                  AND candidate_digest = ?
                  AND component_artifact_digest = ? AND boundary = ?
                  AND owner_hash = ? AND work_hash = ?
                  AND run_hash = ? AND artifact_hash = ?
                  AND status = 'armed' AND expires_at > ?
                ORDER BY armed_at ASC, control_ref ASC
                LIMIT 1
                """,
                (
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    self._candidate_digest,
                    self._artifact_digest,
                    boundary,
                    scope["owner"],
                    scope["work"],
                    scope["run"],
                    scope["artifact"],
                    now,
                ),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            cursor = connection.execute(
                """
                UPDATE local_qa_fault_controls
                SET status = 'consumed', consumed_at = ?, consumption_count = 1
                WHERE control_ref = ? AND status = 'armed'
                  AND candidate_digest = ? AND component_artifact_digest = ?
                  AND consumption_count = 0 AND expires_at > ?
                """,
                (
                    now,
                    row["control_ref"],
                    self._candidate_digest,
                    self._artifact_digest,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                connection.execute("COMMIT")
                return None
            self._set_ledger_status(
                connection,
                control_ref=str(row["control_ref"]),
                status="consumed",
            )
            self._audit(
                connection,
                control_ref=str(row["control_ref"]),
                case_id=authority.case_id,
                boundary=boundary,
                action="consumed",
                status="consumed",
                occurred_at=now,
            )
            connection.execute("COMMIT")
        return LocalQAFaultDirective(
            control_ref=str(row["control_ref"]),
            case_id=authority.case_id,
            boundary=boundary,
            parameters=json.loads(str(row["parameters_json"])),
            scope_hashes=scope,
            consumed_at=now,
        )

    def record_effect(
        self,
        directive: LocalQAFaultDirective,
        *,
        outcome: str,
    ) -> None:
        """Append one hash-only receipt for the effect of a consumed control."""

        clean_outcome = _clean_private_identity(
            outcome, "outcome", required=True
        )
        if len(clean_outcome) > 128:
            raise LocalQAControlError("outcome is invalid")
        now = _iso(self._clock())
        effect_hash = _hash_private(
            "effect",
            json.dumps(
                {
                    "boundary": directive.boundary,
                    "controlRef": directive.control_ref,
                    "outcome": clean_outcome,
                    "scopeHashes": directive.scope_hashes,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT case_id, boundary, status, consumed_at,
                       owner_hash, work_hash, run_hash, artifact_hash
                FROM local_qa_fault_controls
                WHERE control_ref = ?
                """,
                (directive.control_ref,),
            ).fetchone()
            expected_scope = {
                "owner": str(row["owner_hash"] or "") if row else "",
                "work": str(row["work_hash"] or "") if row else "",
                "run": str(row["run_hash"] or "") if row else "",
                "artifact": str(row["artifact_hash"] or "") if row else "",
            }
            if (
                row is None
                or str(row["status"] or "") != "consumed"
                or str(row["case_id"] or "") != directive.case_id
                or str(row["boundary"] or "") != directive.boundary
                or str(row["consumed_at"] or "") != directive.consumed_at
                or expected_scope != directive.scope_hashes
            ):
                connection.execute("ROLLBACK")
                raise LocalQAControlError("The consumed local-QA control is invalid")
            existing = connection.execute(
                """
                SELECT 1 FROM local_qa_fault_audit
                WHERE control_ref = ? AND action = 'effect_applied'
                """,
                (directive.control_ref,),
            ).fetchone()
            if existing is None:
                self._audit(
                    connection,
                    control_ref=directive.control_ref,
                    case_id=directive.case_id,
                    boundary=directive.boundary,
                    action="effect_applied",
                    status="applied",
                    occurred_at=now,
                    detail={
                        "effectHash": effect_hash,
                        "scopeHashes": dict(directive.scope_hashes),
                    },
                )
            connection.execute("COMMIT")

    def audit(self, request: Mapping[str, object]) -> dict[str, object]:
        """Return bounded redacted consumption/effect evidence for one session."""

        authority, token_hash, session_hash, binding = self._management_authority(
            request
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT audit.control_ref, audit.boundary, audit.action,
                       audit.status, audit.occurred_at, audit.detail_json
                FROM local_qa_fault_audit AS audit
                JOIN local_qa_fault_controls AS controls
                  ON controls.control_ref = audit.control_ref
                WHERE controls.case_id = ? AND controls.case_mode = ?
                  AND controls.token_hash = ? AND controls.session_hash = ?
                  AND controls.candidate_digest = ?
                  AND controls.component_artifact_digest = ?
                ORDER BY audit.occurred_at ASC, audit.audit_ref ASC
                LIMIT ?
                """,
                (
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    binding.candidate_digest,
                    binding.component_artifact_digest,
                    MAX_QUERY_CONTROLS,
                ),
            ).fetchall()
        events: list[dict[str, object]] = []
        for row in rows:
            detail = json.loads(str(row["detail_json"] or "{}"))
            redacted = {
                key: value
                for key, value in detail.items()
                if key in {"effectHash", "scopeHashes"}
            }
            events.append(
                {
                    "controlRef": str(row["control_ref"]),
                    "boundary": str(row["boundary"]),
                    "action": str(row["action"]),
                    "status": str(row["status"]),
                    "occurredAt": str(row["occurred_at"]),
                    "evidence": redacted,
                }
            )
        return {
            "contractVersion": 1,
            "operation": "audit",
            "status": "ok",
            "caseId": authority.case_id,
            "count": len(events),
            "truncated": len(events) == MAX_QUERY_CONTROLS,
            "events": events,
        }

    def _management_authority(
        self,
        request: Mapping[str, object],
        *,
        extra_fields: frozenset[str] = frozenset(),
        allow_replaced_binding: bool = False,
    ) -> tuple[LocalQAAuthority, str, str, LocalQAArtifactBinding]:
        authority = self._base_request(request, exact_fields=_BASE_FIELDS | extra_fields)
        requested_binding = self._request_artifact_binding(request)
        runtime = self._runtime_authority()
        if runtime is None:
            raise LocalQAAuthorityError("The canonical local-QA authority is inactive")
        self._require_active_authority(authority)
        if not allow_replaced_binding:
            self._require_current_artifact_binding(request)
        token_hash, session_hash = self._authority_hashes(authority)
        return authority, token_hash, session_hash, requested_binding

    def _expire_session_controls(
        self,
        connection: sqlite3.Connection,
        *,
        authority: LocalQAAuthority,
        token_hash: str,
        session_hash: str,
        binding: LocalQAArtifactBinding,
        now: str,
    ) -> int:
        rows = connection.execute(
            """
            SELECT control_ref, boundary FROM local_qa_fault_controls
            WHERE case_id = ? AND token_hash = ? AND session_hash = ?
              AND candidate_digest = ? AND component_artifact_digest = ?
              AND status = 'armed' AND expires_at <= ?
            """,
            (
                authority.case_id,
                token_hash,
                session_hash,
                binding.candidate_digest,
                binding.component_artifact_digest,
                now,
            ),
        ).fetchall()
        changed = 0
        for row in rows:
            cursor = connection.execute(
                """
                UPDATE local_qa_fault_controls
                SET status = 'expired', cleared_at = ?
                WHERE control_ref = ? AND status = 'armed' AND expires_at <= ?
                """,
                (now, row["control_ref"], now),
            )
            if cursor.rowcount:
                changed += 1
                self._set_ledger_status(
                    connection,
                    control_ref=str(row["control_ref"]),
                    status="expired",
                )
                self._audit(
                    connection,
                    control_ref=str(row["control_ref"]),
                    case_id=authority.case_id,
                    boundary=str(row["boundary"]),
                    action="expired",
                    status="expired",
                    occurred_at=now,
                )
        return changed

    def query(self, request: Mapping[str, object]) -> dict[str, object]:
        authority, token_hash, session_hash, binding = self._management_authority(
            request
        )
        now = _iso(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_session_controls(
                connection,
                authority=authority,
                token_hash=token_hash,
                session_hash=session_hash,
                binding=binding,
                now=now,
            )
            rows = connection.execute(
                """
                SELECT * FROM local_qa_fault_controls
                WHERE case_id = ? AND case_mode = ?
                  AND token_hash = ? AND session_hash = ?
                  AND candidate_digest = ? AND component_artifact_digest = ?
                ORDER BY armed_at ASC, control_ref ASC
                LIMIT ?
                """,
                (
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    binding.candidate_digest,
                    binding.component_artifact_digest,
                    MAX_QUERY_CONTROLS,
                ),
            ).fetchall()
            connection.execute("COMMIT")
        controls = []
        for row in rows:
            receipt = self._receipt(row)
            receipt["consumedAt"] = str(row["consumed_at"] or "") or None
            receipt["clearedAt"] = str(row["cleared_at"] or "") or None
            controls.append(receipt)
        return {
            "contractVersion": 1,
            "operation": "query",
            "status": "ok",
            "caseId": authority.case_id,
            "count": len(controls),
            "truncated": len(controls) == MAX_QUERY_CONTROLS,
            "controls": controls,
        }

    def clear(self, request: Mapping[str, object]) -> dict[str, object]:
        authority, token_hash, session_hash, binding = self._management_authority(
            request, extra_fields=frozenset({"controlRef"})
        )
        control_ref = _clean_private_identity(
            request.get("controlRef"), "controlRef", required=True
        )
        if not control_ref.startswith("qac_sha256:") or len(control_ref) != 75:
            raise LocalQAControlError("The exact control was not found")
        now = _iso(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM local_qa_fault_controls
                WHERE control_ref = ? AND case_id = ? AND case_mode = ?
                  AND token_hash = ? AND session_hash = ?
                  AND candidate_digest = ? AND component_artifact_digest = ?
                """,
                (
                    control_ref,
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    binding.candidate_digest,
                    binding.component_artifact_digest,
                ),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                raise LocalQAControlError("The exact control was not found")
            if str(row["status"]) == "armed":
                connection.execute(
                    """
                    UPDATE local_qa_fault_controls
                    SET status = 'cleared', cleared_at = ?
                    WHERE control_ref = ? AND status = 'armed'
                    """,
                    (now, control_ref),
                )
                self._set_ledger_status(
                    connection, control_ref=control_ref, status="cleared"
                )
                self._audit(
                    connection,
                    control_ref=control_ref,
                    case_id=authority.case_id,
                    boundary=str(row["boundary"]),
                    action="cleared",
                    status="cleared",
                    occurred_at=now,
                )
            current = connection.execute(
                "SELECT * FROM local_qa_fault_controls WHERE control_ref = ?", (control_ref,)
            ).fetchone()
            connection.execute("COMMIT")
        receipt = self._receipt(current, operation="clear")
        receipt["clearedAt"] = str(current["cleared_at"] or "") or None
        return receipt

    def cleanup(self, request: Mapping[str, object]) -> dict[str, object]:
        authority, token_hash, session_hash, binding = self._management_authority(
            request, allow_replaced_binding=True
        )
        now = _iso(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired_count = self._expire_session_controls(
                connection,
                authority=authority,
                token_hash=token_hash,
                session_hash=session_hash,
                binding=binding,
                now=now,
            )
            replaced: list[sqlite3.Row] = []
            if self._candidate_digest and self._artifact_digest:
                replaced = connection.execute(
                    """
                    SELECT control_ref, boundary FROM local_qa_fault_controls
                    WHERE case_id = ? AND case_mode = ?
                      AND token_hash = ? AND session_hash = ?
                      AND candidate_digest = ? AND component_artifact_digest = ?
                      AND status = 'armed'
                      AND (candidate_digest != ? OR component_artifact_digest != ?)
                    """,
                    (
                        authority.case_id,
                        authority.case_mode,
                        token_hash,
                        session_hash,
                        binding.candidate_digest,
                        binding.component_artifact_digest,
                        self._candidate_digest,
                        self._artifact_digest,
                    ),
                ).fetchall()
            for row in replaced:
                cursor = connection.execute(
                    """
                    UPDATE local_qa_fault_controls
                    SET status = 'cleared', cleared_at = ?
                    WHERE control_ref = ? AND status = 'armed'
                      AND candidate_digest = ? AND component_artifact_digest = ?
                      AND (candidate_digest != ? OR component_artifact_digest != ?)
                    """,
                    (
                        now,
                        row["control_ref"],
                        binding.candidate_digest,
                        binding.component_artifact_digest,
                        self._candidate_digest,
                        self._artifact_digest,
                    ),
                )
                if cursor.rowcount:
                    self._set_ledger_status(
                        connection,
                        control_ref=str(row["control_ref"]),
                        status="cleared",
                    )
                    self._audit(
                        connection,
                        control_ref=str(row["control_ref"]),
                        case_id=authority.case_id,
                        boundary=str(row["boundary"]),
                        action="artifact_replacement_cleared",
                        status="cleared",
                        occurred_at=now,
                    )
            active = connection.execute(
                """
                SELECT COUNT(*) FROM local_qa_fault_controls
                WHERE case_id = ? AND case_mode = ?
                  AND token_hash = ? AND session_hash = ?
                  AND candidate_digest = ? AND component_artifact_digest = ?
                  AND status = 'armed'
                """,
                (
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    binding.candidate_digest,
                    binding.component_artifact_digest,
                ),
            ).fetchone()[0]
            if active:
                connection.execute("ROLLBACK")
                raise LocalQAControlError("Live armed controls must be cleared before cleanup")
            refs = [
                str(row["control_ref"])
                for row in connection.execute(
                    """
                    SELECT control_ref FROM local_qa_fault_controls
                    WHERE case_id = ? AND case_mode = ?
                      AND token_hash = ? AND session_hash = ?
                      AND candidate_digest = ? AND component_artifact_digest = ?
                    """,
                    (
                        authority.case_id,
                        authority.case_mode,
                        token_hash,
                        session_hash,
                        binding.candidate_digest,
                        binding.component_artifact_digest,
                    ),
                ).fetchall()
            ]
            removed_audit = 0
            if refs:
                placeholders = ",".join("?" for _ in refs)
                removed_audit = connection.execute(
                    f"DELETE FROM local_qa_fault_audit WHERE control_ref IN ({placeholders})",
                    refs,
                ).rowcount
            removed_controls = connection.execute(
                """
                DELETE FROM local_qa_fault_controls
                WHERE case_id = ? AND case_mode = ?
                  AND token_hash = ? AND session_hash = ?
                  AND candidate_digest = ? AND component_artifact_digest = ?
                  AND status != 'armed'
                """,
                (
                    authority.case_id,
                    authority.case_mode,
                    token_hash,
                    session_hash,
                    binding.candidate_digest,
                    binding.component_artifact_digest,
                ),
            ).rowcount
            connection.execute("COMMIT")
        return {
            "contractVersion": 1,
            "operation": "cleanup",
            "status": "clean",
            "caseId": authority.case_id,
            "expiredControls": expired_count,
            "artifactReplacementCleared": len(replaced),
            "removedControls": removed_controls,
            "removedAuditEvents": removed_audit,
        }


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise LocalQAControlError("The local-QA CLI arguments are invalid")


def _reject_duplicate_cli_options(arguments: Sequence[str]) -> None:
    seen: set[str] = set()
    for argument in arguments:
        if not isinstance(argument, str):
            raise LocalQAControlError("The local-QA CLI arguments are invalid")
        if not argument.startswith("--"):
            continue
        option = argument.split("=", 1)[0]
        if option in seen:
            raise LocalQAControlError("The local-QA CLI arguments are invalid")
        seen.add(option)


def _cli_parser() -> argparse.ArgumentParser:
    parser = _PrivateArgumentParser(
        prog="glasshive-local-qa-control",
        description="Manage exact synthetic GlassHive local-QA fault controls.",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("operation", choices=("arm", "query", "audit", "clear", "cleanup"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-fd", type=int)
    source.add_argument("--input-file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else list(sys.argv[1:])
    safe_operation = (
        arguments[0]
        if arguments and arguments[0] in {"arm", "query", "audit", "clear", "cleanup"}
        else "unknown"
    )
    try:
        _reject_duplicate_cli_options(arguments)
        args = _cli_parser().parse_args(arguments)
        request = read_private_request(input_fd=args.input_fd, input_file=args.input_file)
        db_path = str(os.environ.get("WPR_DB_PATH", "") or "").strip()
        if not db_path:
            raise LocalQAControlError("The GlassHive runtime database is unavailable")
        plane = LocalQAControlPlane(db_path)
        operation = getattr(plane, str(args.operation))
        receipt = operation(request)
    except Exception:
        error = {
            "contractVersion": 1,
            "operation": safe_operation,
            "status": "rejected",
            "code": "local_qa_control_rejected",
            "message": "The local-QA control request was rejected",
        }
        serialized = json.dumps(error, sort_keys=True, separators=(",", ":")) + "\n"
        if len(serialized.encode("utf-8")) > PRIVATE_ERROR_LIMIT_BYTES:
            serialized = '{"code":"local_qa_control_rejected","status":"rejected"}\n'
        sys.stderr.write(serialized)
        return 2
    sys.stdout.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
