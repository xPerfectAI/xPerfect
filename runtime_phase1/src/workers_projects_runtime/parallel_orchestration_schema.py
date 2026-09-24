from __future__ import annotations

import re
import sqlite3

from .schema_version import execute_schema_script


PARALLEL_ORCHESTRATION_TABLES = r"""
CREATE TABLE IF NOT EXISTS active_work_action_uses (
                    action_use_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    work_ref TEXT NOT NULL,
                    source_run_id TEXT NOT NULL DEFAULT '',
                    effect_phase TEXT NOT NULL DEFAULT '',
                    lifecycle_operation_id TEXT NOT NULL DEFAULT '',
                    lifecycle_operation_kind TEXT NOT NULL DEFAULT '',
                    lifecycle_target_run_id TEXT NOT NULL DEFAULT '',
                    executor_id TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    idempotency_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    source_context_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, owner_id, work_ref, idempotency_key),
                    FOREIGN KEY(work_ref) REFERENCES delegations(work_ref)
                );

CREATE TABLE IF NOT EXISTS callback_trace_events (
                    callback_trace_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    callback_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    callback_sequence INTEGER NOT NULL,
                    run_sequence INTEGER NOT NULL,
                    mutation_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    authority_sha256 TEXT NOT NULL,
                    previous_event_sha256 TEXT NOT NULL DEFAULT '',
                    event_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(callback_id, callback_sequence),
                    UNIQUE(run_id, run_sequence),
                    FOREIGN KEY(callback_id) REFERENCES callback_outbox(callback_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

CREATE TABLE IF NOT EXISTS capability_grant_revocations (
                    revocation_id TEXT PRIMARY KEY,
                    authorization_ref TEXT NOT NULL,
                    origin_ref TEXT NOT NULL,
                    work_ref TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    container_generation_id TEXT NOT NULL,
                    host_startup_lease_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'armed',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT,
                    UNIQUE(grant_id, container_generation_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

CREATE TABLE IF NOT EXISTS capacity_attempts (
                    capacity_attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    attempt_id TEXT NOT NULL DEFAULT '',
                    capacity_class TEXT NOT NULL,
                    available_json TEXT NOT NULL DEFAULT '{}',
                    required_json TEXT NOT NULL DEFAULT '{}',
                    shortage_json TEXT NOT NULL DEFAULT '{}',
                    reservation_json TEXT NOT NULL DEFAULT '{}',
                    next_retry_at TEXT,
                    observed_at TEXT NOT NULL,
                    UNIQUE(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

CREATE TABLE IF NOT EXISTS delegations (
                    work_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    origin_ref TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    origin_surface TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    initial_run_id TEXT NOT NULL,
                    current_run_id TEXT NOT NULL,
                    dismissed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, owner_id, idempotency_key),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(initial_run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(current_run_id) REFERENCES runs(run_id)
                );

CREATE TABLE IF NOT EXISTS host_run_leases (
                    lease_id TEXT PRIMARY KEY,
                    runtime_family TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    pid INTEGER,
                    process_group INTEGER,
                    process_start_identity TEXT NOT NULL DEFAULT '',
                    startup_token TEXT NOT NULL DEFAULT '',
                    startup_state TEXT NOT NULL DEFAULT 'legacy_unknown',
                    startup_confirmed_at TEXT,
                    startup_identity_kind TEXT NOT NULL DEFAULT '',
                    startup_container_id TEXT NOT NULL DEFAULT '',
                    startup_session_id TEXT NOT NULL DEFAULT '',
                    mutation_scope TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    reserved_child_processes INTEGER NOT NULL DEFAULT 0,
                    reserved_threads INTEGER NOT NULL DEFAULT 0,
                    reserved_memory_bytes INTEGER NOT NULL DEFAULT 0,
                    reserved_disk_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    reconciled_at TEXT,
                    released_at TEXT,
                    release_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE (run_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

CREATE TABLE IF NOT EXISTS lifecycle_operation_effects (
                    effect_id TEXT PRIMARY KEY,
                    operation_digest TEXT NOT NULL,
                    operation_epoch INTEGER NOT NULL,
                    operation_kind TEXT NOT NULL,
                    effect_kind TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT,
                    UNIQUE(
                        operation_digest, operation_epoch, operation_kind,
                        effect_kind, worker_id, run_id
                    ),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                );

CREATE TABLE IF NOT EXISTS preflight_capacity_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    runtime_family TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    mutation_scope TEXT NOT NULL DEFAULT '',
                    reserved_child_processes INTEGER NOT NULL DEFAULT 0,
                    reserved_threads INTEGER NOT NULL DEFAULT 0,
                    reserved_memory_bytes INTEGER NOT NULL DEFAULT 0,
                    reserved_disk_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    release_reason TEXT NOT NULL DEFAULT ''
                );

CREATE TABLE IF NOT EXISTS provider_liveness_events (
                    event_ref TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    failure_class TEXT NOT NULL DEFAULT '',
                    runtime TEXT NOT NULL,
                    model TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL,
                    source_digest TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(attempt_id) REFERENCES run_attempts(attempt_id)
                );

CREATE TABLE IF NOT EXISTS provider_main_contexts (
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    continuity_domain_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, owner_id, agent_id),
                    UNIQUE (tenant_id, owner_id, continuity_domain_id)
                );

CREATE TABLE IF NOT EXISTS provider_route_health (
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    runtime TEXT NOT NULL,
                    model TEXT NOT NULL,
                    failure_class TEXT NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 1,
                    failure_generation INTEGER NOT NULL DEFAULT 1,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    cooldown_until TEXT NOT NULL,
                    cooldown_source TEXT NOT NULL,
                    last_run_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, owner_id, profile, runtime, model)
                );

CREATE TABLE IF NOT EXISTS provider_route_health_events (
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    runtime TEXT NOT NULL,
                    model TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    evidence_kind TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (
                        tenant_id, owner_id, profile, runtime, model, evidence_id
                    )
                );

CREATE TABLE IF NOT EXISTS provider_session_visible_admissions (
                    session_id TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    advancement_key TEXT NOT NULL DEFAULT '',
                    accepted_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, message_key),
                    FOREIGN KEY(session_id) REFERENCES provider_sessions(session_id)
                        ON UPDATE CASCADE ON DELETE CASCADE
                );

CREATE TABLE IF NOT EXISTS provider_stop_tombstones (
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    base_idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, owner_id, base_idempotency_key)
                );

CREATE TABLE IF NOT EXISTS run_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    admitted_at TEXT,
                    runtime_invoked_at TEXT,
                    provider_health_observed_last_failed_at TEXT,
                    provider_health_observed_generation INTEGER,
                    ended_at TEXT,
                    lease_id TEXT NOT NULL DEFAULT '',
                    capacity_class TEXT NOT NULL DEFAULT '',
                    capacity_available_json TEXT NOT NULL DEFAULT '{}',
                    capacity_required_json TEXT NOT NULL DEFAULT '{}',
                    capacity_shortage_json TEXT NOT NULL DEFAULT '{}',
                    capacity_reservation_json TEXT NOT NULL DEFAULT '{}',
                    capacity_next_retry_at TEXT,
                    terminal_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id, attempt_number),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS service_assertion_nonces (
                    audience TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    issued_at_epoch INTEGER NOT NULL,
                    expires_at_epoch INTEGER NOT NULL,
                    request_method TEXT NOT NULL,
                    request_path TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    PRIMARY KEY (audience, tenant_id, owner_id, nonce)
                );

CREATE TABLE IF NOT EXISTS terminal_callback_reconciliations (
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 0,
                    callback_contract_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (
                        run_id, state, ended_at, attempt_number,
                        callback_contract_digest
                    ),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

CREATE TABLE IF NOT EXISTS terminal_callback_result_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receiver_scope TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    callback_id TEXT NOT NULL,
                    result_revision INTEGER NOT NULL,
                    result_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_result_revision INTEGER NOT NULL,
                    current_result_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS terminal_callback_results (
                    receiver_scope TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    callback_id TEXT NOT NULL,
                    result_revision INTEGER NOT NULL,
                    result_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (receiver_scope, run_id)
                );

CREATE TABLE IF NOT EXISTS work_trace_events (
                    trace_event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    work_ref TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_sha256 TEXT NOT NULL DEFAULT '',
                    event_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(work_ref) REFERENCES delegations(work_ref)
                );
"""


COMMON_TABLE_COLUMN_ADDITIONS: dict[str, tuple[str, ...]] = {'callback_outbox': ('attempt_number INTEGER NOT NULL DEFAULT 0', 'result_revision INTEGER NOT NULL DEFAULT 0', "result_digest TEXT NOT NULL DEFAULT ''", 'http_accepted_at TEXT', "delivery_lease_token TEXT NOT NULL DEFAULT ''", 'delivery_generation INTEGER NOT NULL DEFAULT 0', 'delivery_lease_expires_at TEXT'), 'events': ("payload_json TEXT NOT NULL DEFAULT '{}'",), 'provider_requests': ("replay_decision_json TEXT NOT NULL DEFAULT '{}'", "admitted_instruction TEXT NOT NULL DEFAULT ''", "fallback_model_id TEXT NOT NULL DEFAULT ''", "fallback_reasoning_effort TEXT NOT NULL DEFAULT ''", "fallback_instruction TEXT NOT NULL DEFAULT ''", "fallback_state TEXT NOT NULL DEFAULT ''", "fallback_from_run_id TEXT NOT NULL DEFAULT ''", 'response_timeout_s REAL', "response_deadline_at TEXT NOT NULL DEFAULT ''"), 'runs': ('first_queued_at TEXT NOT NULL', 'queue_deadline_at TEXT NOT NULL', "queue_blocker_class TEXT NOT NULL DEFAULT 'admission_pending'", 'queue_next_status_at TEXT', 'queue_wait_episode INTEGER NOT NULL DEFAULT 1', 'queue_wait_open INTEGER NOT NULL DEFAULT 1', 'queue_wait_generation INTEGER NOT NULL DEFAULT 1', 'queue_wait_started_at TEXT NOT NULL', 'queue_wait_closed_at TEXT', 'queue_wait_duration_seconds INTEGER', 'queue_transition_emitted INTEGER NOT NULL DEFAULT 0', 'queue_status_sequence INTEGER NOT NULL DEFAULT 0', "queue_callback_state TEXT NOT NULL DEFAULT 'unknown'", "queue_terminal_callback_id TEXT NOT NULL DEFAULT ''", 'claimed_at TEXT', 'admitted_at TEXT', 'runtime_invoked_at TEXT', "active_attempt_id TEXT NOT NULL DEFAULT ''", 'terminal_result_revision INTEGER NOT NULL DEFAULT 0', 'failure_structured INTEGER NOT NULL DEFAULT 0', 'capacity_retry_count INTEGER NOT NULL DEFAULT 0', "native_session_id TEXT NOT NULL DEFAULT ''", "native_capabilities_json TEXT NOT NULL DEFAULT '{}'", "native_child_summary_json TEXT NOT NULL DEFAULT '{}'", 'liveness_started_at TEXT', 'meaningful_progress_at TEXT', 'meaningful_progress_sequence INTEGER NOT NULL DEFAULT 0', 'internal_retry_count INTEGER NOT NULL DEFAULT 0', "last_internal_retry_class TEXT NOT NULL DEFAULT ''", "liveness_mode TEXT NOT NULL DEFAULT 'standard'", 'provider_liveness_route_locked INTEGER NOT NULL DEFAULT 0', "capacity_class TEXT NOT NULL DEFAULT ''", "capacity_available_json TEXT NOT NULL DEFAULT '{}'", "capacity_required_json TEXT NOT NULL DEFAULT '{}'", "capacity_shortage_json TEXT NOT NULL DEFAULT '{}'", "capacity_reservation_json TEXT NOT NULL DEFAULT '{}'", 'capacity_next_retry_at TEXT', "provider_route_profile TEXT NOT NULL DEFAULT ''", "provider_route_runtime TEXT NOT NULL DEFAULT ''", "provider_route_model TEXT NOT NULL DEFAULT ''", "provider_route_decision TEXT NOT NULL DEFAULT ''", "provider_route_from_profile TEXT NOT NULL DEFAULT ''", "provider_route_from_runtime TEXT NOT NULL DEFAULT ''", "provider_route_from_model TEXT NOT NULL DEFAULT ''", "provider_route_failure_class TEXT NOT NULL DEFAULT ''", 'provider_route_cooldown_until TEXT', "continuation_contract_json TEXT NOT NULL DEFAULT '{}'", "continuation_context_json TEXT NOT NULL DEFAULT '{}'"), 'workers': ("trusted_run_lane TEXT NOT NULL DEFAULT 'mission'", "resource_class TEXT NOT NULL DEFAULT 'standard'", 'resource_memory_bytes INTEGER NOT NULL DEFAULT 3221225472', "compute_release_token TEXT NOT NULL DEFAULT ''", "compute_release_owner TEXT NOT NULL DEFAULT ''", 'compute_release_claimed_at TEXT', 'compute_release_expires_at TEXT', 'compute_release_epoch INTEGER NOT NULL DEFAULT 0', "compute_release_kind TEXT NOT NULL DEFAULT ''", "compute_release_scope TEXT NOT NULL DEFAULT 'compute_only'", "compute_release_container_id TEXT NOT NULL DEFAULT ''", "compute_release_session_fingerprint TEXT NOT NULL DEFAULT ''", "compute_release_target_run_id TEXT NOT NULL DEFAULT ''", "compute_release_target_started_at TEXT NOT NULL DEFAULT ''", "compute_release_terminal_run_id TEXT NOT NULL DEFAULT ''", "compute_release_replacement_run_id TEXT NOT NULL DEFAULT ''", 'compute_release_runtime_confirmed_at TEXT', "compute_release_runtime_proof_digest TEXT NOT NULL DEFAULT ''", "compute_release_operation_id TEXT NOT NULL DEFAULT ''", "work_stop_id TEXT NOT NULL DEFAULT ''", 'work_stop_requested_at TEXT', 'work_stop_settled_at TEXT', "work_stop_outcome TEXT NOT NULL DEFAULT ''")}

PARALLEL_TABLE_COLUMN_ADDITIONS: dict[str, tuple[str, ...]] = {
    "capability_grant_revocations": ("host_startup_lease_id TEXT NOT NULL DEFAULT ''",),
    "active_work_action_uses": (
        "source_run_id TEXT NOT NULL DEFAULT ''",
        "effect_phase TEXT NOT NULL DEFAULT ''",
        "lifecycle_operation_id TEXT NOT NULL DEFAULT ''",
        "lifecycle_operation_kind TEXT NOT NULL DEFAULT ''",
        "lifecycle_target_run_id TEXT NOT NULL DEFAULT ''",
        "executor_id TEXT NOT NULL DEFAULT ''",
        "lease_expires_at TEXT",
        "source_context_json TEXT NOT NULL DEFAULT '{}'",
    ),
    "delegations": (
        "dismissed_at TEXT",
        "origin_ref TEXT NOT NULL DEFAULT ''",
    ),
    "host_run_leases": (
        "mutation_scope TEXT NOT NULL DEFAULT ''",
        "startup_token TEXT NOT NULL DEFAULT ''",
        "startup_state TEXT NOT NULL DEFAULT 'legacy_unknown'",
        "startup_confirmed_at TEXT",
        "startup_identity_kind TEXT NOT NULL DEFAULT ''",
        "startup_container_id TEXT NOT NULL DEFAULT ''",
        "startup_session_id TEXT NOT NULL DEFAULT ''",
        "attempt_id TEXT NOT NULL DEFAULT ''",
        "reserved_child_processes INTEGER NOT NULL DEFAULT 0",
        "reserved_threads INTEGER NOT NULL DEFAULT 0",
        "reserved_memory_bytes INTEGER NOT NULL DEFAULT 0",
        "reserved_disk_bytes INTEGER NOT NULL DEFAULT 0",
    ),
    "lifecycle_operation_effects": (
        "lease_epoch INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at TEXT",
    ),
    "preflight_capacity_reservations": (
        "heartbeat_at TEXT NOT NULL DEFAULT ''",
        "mutation_scope TEXT NOT NULL DEFAULT ''",
        "reserved_child_processes INTEGER NOT NULL DEFAULT 0",
        "reserved_threads INTEGER NOT NULL DEFAULT 0",
        "reserved_memory_bytes INTEGER NOT NULL DEFAULT 0",
        "reserved_disk_bytes INTEGER NOT NULL DEFAULT 0",
    ),
    "provider_liveness_events": (
        "source_sequence INTEGER NOT NULL DEFAULT 0",
        "source_digest TEXT NOT NULL DEFAULT ''",
    ),
    "provider_route_health": (
        "failure_generation INTEGER NOT NULL DEFAULT 1",
    ),
    "provider_session_visible_admissions": (
        "advancement_key TEXT NOT NULL DEFAULT ''",
    ),
    "run_attempts": (
        "provider_health_observed_last_failed_at TEXT",
        "provider_health_observed_generation INTEGER",
        "capacity_class TEXT NOT NULL DEFAULT ''",
        "capacity_available_json TEXT NOT NULL DEFAULT '{}'",
        "capacity_required_json TEXT NOT NULL DEFAULT '{}'",
        "capacity_shortage_json TEXT NOT NULL DEFAULT '{}'",
        "capacity_reservation_json TEXT NOT NULL DEFAULT '{}'",
        "capacity_next_retry_at TEXT",
    ),
}


PARALLEL_ORCHESTRATION_INDEXES_AND_TRIGGERS = r"""
CREATE TRIGGER IF NOT EXISTS callback_outbox_authority_immutable
                BEFORE UPDATE ON callback_outbox
                WHEN OLD.callback_id IS NOT NEW.callback_id
                  OR OLD.project_id IS NOT NEW.project_id
                  OR OLD.worker_id IS NOT NEW.worker_id
                  OR OLD.tenant_id IS NOT NEW.tenant_id
                  OR OLD.run_id IS NOT NEW.run_id
                  OR OLD.attempt_number IS NOT NEW.attempt_number
                  OR OLD.event_type IS NOT NEW.event_type
                  OR OLD.url IS NOT NEW.url
                  OR OLD.result_revision IS NOT NEW.result_revision
                  OR OLD.result_digest IS NOT NEW.result_digest
                  OR OLD.created_at IS NOT NEW.created_at
                BEGIN
                    SELECT RAISE(ABORT, 'callback authority is immutable');
                END;

CREATE TRIGGER IF NOT EXISTS callback_outbox_delete_forbidden
                BEFORE DELETE ON callback_outbox
                BEGIN
                    SELECT RAISE(ABORT, 'callback authority is immutable');
                END;

CREATE TRIGGER IF NOT EXISTS callback_outbox_trace_insert
                AFTER INSERT ON callback_outbox
                BEGIN
                    INSERT INTO callback_trace_events (
                        callback_id, run_id, callback_sequence, run_sequence,
                        mutation_kind, status, snapshot_json, payload_sha256,
                        authority_sha256, previous_event_sha256, event_sha256,
                        created_at
                    )
                    SELECT
                        callback_id, run_id, callback_sequence, run_sequence,
                        'insert', status, snapshot_json, payload_sha256,
                        authority_sha256, previous_event_sha256,
                        glasshive_callback_trace_sha256(
                            callback_id, callback_sequence, run_sequence,
                            snapshot_json, previous_event_sha256
                        ),
                        observed_at
                    FROM (
                        SELECT
                            NEW.callback_id AS callback_id,
                            NEW.run_id AS run_id,
                            COALESCE((
                                SELECT MAX(callback_sequence)
                                FROM callback_trace_events
                                WHERE callback_id = NEW.callback_id
                            ), 0) + 1 AS callback_sequence,
                            COALESCE((
                                SELECT MAX(run_sequence)
                                FROM callback_trace_events
                                WHERE run_id = NEW.run_id
                            ), 0) + 1 AS run_sequence,
                            NEW.status AS status,
                            glasshive_callback_trace_snapshot(
                                NEW.callback_id, NEW.project_id, NEW.worker_id,
                                NEW.tenant_id, NEW.run_id, NEW.attempt_number,
                                NEW.event_type, NEW.url, NEW.payload_json,
                                NEW.result_revision, NEW.result_digest,
                                NEW.status, NEW.attempts, NEW.last_error,
                                NEW.created_at, NEW.updated_at, NEW.delivered_at,
                                NEW.http_accepted_at, NEW.delivery_lease_token,
                                NEW.delivery_generation,
                                NEW.delivery_lease_expires_at
                            ) AS snapshot_json,
                            'sha256:' || glasshive_sha256(NEW.payload_json)
                                AS payload_sha256,
                            'sha256:' || glasshive_sha256(
                                glasshive_callback_trace_authority(
                                    NEW.callback_id, NEW.project_id,
                                    NEW.worker_id, NEW.tenant_id, NEW.run_id,
                                    NEW.attempt_number, NEW.event_type, NEW.url,
                                    NEW.result_revision, NEW.result_digest,
                                    NEW.delivery_lease_token,
                                    NEW.delivery_generation,
                                    NEW.delivery_lease_expires_at
                                )
                            ) AS authority_sha256,
                            COALESCE((
                                SELECT event_sha256
                                FROM callback_trace_events
                                WHERE run_id = NEW.run_id
                                ORDER BY run_sequence DESC LIMIT 1
                            ), '') AS previous_event_sha256,
                            COALESCE(NEW.updated_at, NEW.created_at, CURRENT_TIMESTAMP)
                                AS observed_at
                    )
                    WHERE NEW.run_id IS NOT NULL AND NEW.run_id <> '';
                END;

CREATE TRIGGER IF NOT EXISTS callback_outbox_trace_update
                AFTER UPDATE ON callback_outbox
                BEGIN
                    INSERT INTO callback_trace_events (
                        callback_id, run_id, callback_sequence, run_sequence,
                        mutation_kind, status, snapshot_json, payload_sha256,
                        authority_sha256, previous_event_sha256, event_sha256,
                        created_at
                    )
                    SELECT
                        callback_id, run_id, callback_sequence, run_sequence,
                        'update', status, snapshot_json, payload_sha256,
                        authority_sha256, previous_event_sha256,
                        glasshive_callback_trace_sha256(
                            callback_id, callback_sequence, run_sequence,
                            snapshot_json, previous_event_sha256
                        ),
                        observed_at
                    FROM (
                        SELECT
                            NEW.callback_id AS callback_id,
                            NEW.run_id AS run_id,
                            COALESCE((
                                SELECT MAX(callback_sequence)
                                FROM callback_trace_events
                                WHERE callback_id = NEW.callback_id
                            ), 0) + 1 AS callback_sequence,
                            COALESCE((
                                SELECT MAX(run_sequence)
                                FROM callback_trace_events
                                WHERE run_id = NEW.run_id
                            ), 0) + 1 AS run_sequence,
                            NEW.status AS status,
                            glasshive_callback_trace_snapshot(
                                NEW.callback_id, NEW.project_id, NEW.worker_id,
                                NEW.tenant_id, NEW.run_id, NEW.attempt_number,
                                NEW.event_type, NEW.url, NEW.payload_json,
                                NEW.result_revision, NEW.result_digest,
                                NEW.status, NEW.attempts, NEW.last_error,
                                NEW.created_at, NEW.updated_at, NEW.delivered_at,
                                NEW.http_accepted_at, NEW.delivery_lease_token,
                                NEW.delivery_generation,
                                NEW.delivery_lease_expires_at
                            ) AS snapshot_json,
                            'sha256:' || glasshive_sha256(NEW.payload_json)
                                AS payload_sha256,
                            'sha256:' || glasshive_sha256(
                                glasshive_callback_trace_authority(
                                    NEW.callback_id, NEW.project_id,
                                    NEW.worker_id, NEW.tenant_id, NEW.run_id,
                                    NEW.attempt_number, NEW.event_type, NEW.url,
                                    NEW.result_revision, NEW.result_digest,
                                    NEW.delivery_lease_token,
                                    NEW.delivery_generation,
                                    NEW.delivery_lease_expires_at
                                )
                            ) AS authority_sha256,
                            COALESCE((
                                SELECT event_sha256
                                FROM callback_trace_events
                                WHERE run_id = NEW.run_id
                                ORDER BY run_sequence DESC LIMIT 1
                            ), '') AS previous_event_sha256,
                            COALESCE(NEW.updated_at, NEW.created_at, CURRENT_TIMESTAMP)
                                AS observed_at
                    )
                    WHERE NEW.run_id IS NOT NULL AND NEW.run_id <> '';
                END;

CREATE TRIGGER IF NOT EXISTS callback_trace_events_append_only_delete
                BEFORE DELETE ON callback_trace_events
                BEGIN
                    SELECT RAISE(ABORT, 'callback trace is append-only');
                END;

CREATE TRIGGER IF NOT EXISTS callback_trace_events_append_only_update
                BEFORE UPDATE ON callback_trace_events
                BEGIN
                    SELECT RAISE(ABORT, 'callback trace is append-only');
                END;

CREATE INDEX IF NOT EXISTS callback_trace_events_run_idx
                    ON callback_trace_events(run_id, run_sequence);

CREATE INDEX IF NOT EXISTS capability_grant_revocations_pending_idx
                    ON capability_grant_revocations(
                        status, next_attempt_at, lease_expires_at, created_at
                    );

CREATE INDEX IF NOT EXISTS idx_active_work_action_uses_work ON active_work_action_uses(work_ref, created_at);

CREATE INDEX IF NOT EXISTS idx_callback_outbox_terminal_result ON callback_outbox(run_id, event_type, attempt_number, result_digest);

CREATE INDEX IF NOT EXISTS idx_callback_outbox_terminal_revision ON callback_outbox(run_id, event_type, attempt_number, result_revision, result_digest);

CREATE INDEX IF NOT EXISTS idx_capacity_attempts_run_sequence
                    ON capacity_attempts(run_id, sequence);

CREATE INDEX IF NOT EXISTS idx_delegations_owner_active_order ON delegations(tenant_id, owner_id, dismissed_at, updated_at DESC, created_at DESC, work_ref DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_delegations_owner_origin_ref ON delegations(tenant_id, owner_id, origin_ref) WHERE origin_ref <> '';

CREATE INDEX IF NOT EXISTS idx_delegations_owner_updated ON delegations(tenant_id, owner_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_host_run_leases_active_family_lane
                    ON host_run_leases(status, runtime_family, lane, heartbeat_at);

CREATE INDEX IF NOT EXISTS idx_host_run_leases_active_mutation_scope ON host_run_leases(status, mutation_scope);

CREATE INDEX IF NOT EXISTS idx_host_run_leases_active_owner
                    ON host_run_leases(status, tenant_id, owner_id, lane);

CREATE INDEX IF NOT EXISTS idx_lifecycle_effects_status_lease
                    ON lifecycle_operation_effects(status, lease_expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_preflight_capacity_active_family_lane
                    ON preflight_capacity_reservations(
                        status, runtime_family, lane, expires_at
                    );

CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_liveness_attempt_sequence ON provider_liveness_events(run_id, attempt_id, source_sequence) WHERE source_sequence > 0;

CREATE INDEX IF NOT EXISTS idx_provider_liveness_run_observed
                    ON provider_liveness_events(run_id, observed_at, event_ref);

CREATE INDEX IF NOT EXISTS idx_provider_requests_state_updated
                    ON provider_requests(state, updated_at);

CREATE INDEX IF NOT EXISTS idx_provider_route_health_events_attempt
                    ON provider_route_health_events(run_id, attempt_id);

CREATE INDEX IF NOT EXISTS idx_provider_route_health_expiry
                    ON provider_route_health(cooldown_until);

CREATE INDEX IF NOT EXISTS idx_provider_sessions_worker ON provider_sessions(worker_id);

CREATE INDEX IF NOT EXISTS idx_provider_stop_tombstones_expiry ON provider_stop_tombstones(expires_at);

CREATE INDEX IF NOT EXISTS idx_provider_visible_admissions_advancement
                    ON provider_session_visible_admissions(session_id, advancement_key)
                    WHERE advancement_key <> '';

CREATE INDEX IF NOT EXISTS idx_run_attempts_run_number
                    ON run_attempts(run_id, attempt_number);

CREATE INDEX IF NOT EXISTS idx_run_attempts_state
                    ON run_attempts(state, claimed_at);

CREATE INDEX IF NOT EXISTS idx_runs_queue_status_due ON runs(queue_wait_open, state, queue_next_status_at, queue_deadline_at);

CREATE INDEX IF NOT EXISTS idx_runs_state_retry_after_worker ON runs(state, retry_after, worker_id);

CREATE INDEX IF NOT EXISTS idx_service_assertion_nonces_expiry ON service_assertion_nonces(expires_at_epoch);

CREATE INDEX IF NOT EXISTS idx_terminal_callback_result_attempts ON terminal_callback_result_attempts(receiver_scope, run_id, attempt_id);

CREATE INDEX IF NOT EXISTS idx_workers_execution_lane_state ON workers(execution_mode, trusted_run_lane, state);

CREATE TRIGGER IF NOT EXISTS trg_runs_terminal_lifecycle_fence
                AFTER UPDATE OF state ON runs
                WHEN NEW.state IN ('completed', 'failed', 'cancelled', 'interrupted')
                 AND OLD.state NOT IN ('completed', 'failed', 'cancelled', 'interrupted')
                BEGIN
                    UPDATE host_run_leases
                    SET status = 'released',
                        released_at = COALESCE(NEW.ended_at, CURRENT_TIMESTAMP),
                        release_reason = 'run_terminal:' || CASE
                            WHEN NEW.failure_class <> '' THEN NEW.failure_class
                            ELSE NEW.state
                        END,
                        reconciled_at = COALESCE(
                            reconciled_at, COALESCE(NEW.ended_at, CURRENT_TIMESTAMP)
                        )
                    WHERE run_id = NEW.run_id AND status = 'active';
                    UPDATE runs
                    SET queue_wait_open = 0,
                        queue_wait_closed_at = COALESCE(
                            queue_wait_closed_at, NEW.ended_at, CURRENT_TIMESTAMP
                        ),
                        queue_wait_duration_seconds = COALESCE(
                            queue_wait_duration_seconds,
                            MAX(
                                0,
                                CAST(
                                    (
                                        julianday(COALESCE(NEW.ended_at, CURRENT_TIMESTAMP))
                                        - julianday(COALESCE(
                                            NEW.queue_wait_started_at,
                                            NEW.first_queued_at,
                                            NEW.queued_at
                                        ))
                                    ) * 86400 AS INTEGER
                                )
                            )
                        ),
                        queue_next_status_at = NULL
                    WHERE run_id = NEW.run_id AND queue_wait_open = 1;
                END;

CREATE TRIGGER IF NOT EXISTS trg_runs_terminal_result_revision_insert
                AFTER INSERT ON runs
                WHEN NEW.state IN ('completed', 'failed', 'cancelled', 'interrupted')
                 AND NEW.terminal_result_revision < 1
                BEGIN
                    UPDATE runs
                    SET terminal_result_revision = 1
                    WHERE run_id = NEW.run_id;
                END;

CREATE TRIGGER IF NOT EXISTS trg_runs_terminal_result_revision_update
                AFTER UPDATE OF
                    state, ended_at, active_attempt_id, output_text, error_text,
                    failure_class, failure_retryable, failure_structured,
                    failure_user_message, failure_recommended_recovery,
                    failure_diagnostic_summary
                ON runs
                WHEN NEW.state IN ('completed', 'failed', 'cancelled', 'interrupted')
                 AND (
                    OLD.state NOT IN ('completed', 'failed', 'cancelled', 'interrupted')
                    OR OLD.state IS NOT NEW.state
                    OR OLD.ended_at IS NOT NEW.ended_at
                    OR OLD.active_attempt_id IS NOT NEW.active_attempt_id
                    OR OLD.output_text IS NOT NEW.output_text
                    OR OLD.error_text IS NOT NEW.error_text
                    OR OLD.failure_class IS NOT NEW.failure_class
                    OR OLD.failure_retryable IS NOT NEW.failure_retryable
                    OR OLD.failure_structured IS NOT NEW.failure_structured
                    OR OLD.failure_user_message IS NOT NEW.failure_user_message
                    OR OLD.failure_recommended_recovery
                        IS NOT NEW.failure_recommended_recovery
                    OR OLD.failure_diagnostic_summary
                        IS NOT NEW.failure_diagnostic_summary
                 )
                BEGIN
                    UPDATE runs
                    SET terminal_result_revision = CASE
                        WHEN OLD.terminal_result_revision < 1 THEN 1
                        ELSE OLD.terminal_result_revision + 1
                    END
                    WHERE run_id = NEW.run_id;
                END;

CREATE TRIGGER IF NOT EXISTS work_trace_events_append_only_delete
                BEFORE DELETE ON work_trace_events
                BEGIN
                    SELECT RAISE(ABORT, 'work trace is append-only');
                END;

CREATE TRIGGER IF NOT EXISTS work_trace_events_append_only_update
                BEFORE UPDATE ON work_trace_events
                BEGIN
                    SELECT RAISE(ABORT, 'work trace is append-only');
                END;

CREATE INDEX IF NOT EXISTS work_trace_events_scope_idx
                    ON work_trace_events(run_id, tenant_id, owner_id, sequence);
"""


# `CREATE TRIGGER IF NOT EXISTS` never replaces a trigger that an older runtime installed. A
# database created before the callback trace gained its run-id guard therefore kept the pre-guard
# `callback_outbox_trace_*` bodies, and every runless lifecycle callback (for example
# `worker.resumed_by_alias` on pre-run alias reuse) tripped `callback_trace_events.run_id NOT NULL`
# from inside the trigger. Only these two triggers are reconciled against this module's text.
RECONCILED_TRIGGERS: tuple[str, ...] = (
    "callback_outbox_trace_insert",
    "callback_outbox_trace_update",
)


def _schema_statements(script: str) -> list[str]:
    statements: list[str] = []
    pending: list[str] = []
    for line in str(script or "").splitlines():
        pending.append(line)
        candidate = "\n".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            pending = []
    return statements


def expected_trigger_statement(name: str) -> str:
    """Return this module's CREATE TRIGGER statement for one reconciled trigger name."""

    header = re.compile(
        rf"^\s*CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+{re.escape(name)}\b",
        re.IGNORECASE,
    )
    for statement in _schema_statements(PARALLEL_ORCHESTRATION_INDEXES_AND_TRIGGERS):
        if header.match(statement):
            return statement
    raise KeyError(name)


def normalized_trigger_sql(sql: str) -> str:
    """Compare trigger bodies the way sqlite_master stores them (no IF NOT EXISTS, no ';')."""

    text = re.sub(r"(?i)\bIF\s+NOT\s+EXISTS\b", "", str(sql or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(";").strip()


def reconcile_stale_triggers(connection: sqlite3.Connection) -> list[str]:
    """Replace only the reconciled triggers whose installed body differs from this module."""

    replaced: list[str] = []
    for name in RECONCILED_TRIGGERS:
        expected = expected_trigger_statement(name)
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        installed = str(row[0] if row is not None else "")
        if normalized_trigger_sql(installed) == normalized_trigger_sql(expected):
            continue
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
        connection.execute(expected)
        replaced.append(name)
    return replaced


def ensure_parallel_orchestration_schema(connection: sqlite3.Connection) -> None:
    """Add the durable orchestration schema without replacing newer core tables."""

    execute_schema_script(connection, PARALLEL_ORCHESTRATION_TABLES)
    for table, definitions in {
        **COMMON_TABLE_COLUMN_ADDITIONS,
        **PARALLEL_TABLE_COLUMN_ADDITIONS,
    }.items():
        present = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for definition in definitions:
            if definition in {
                "first_queued_at TEXT NOT NULL",
                "queue_deadline_at TEXT NOT NULL",
                "queue_wait_started_at TEXT NOT NULL",
            }:
                definition = f"{definition} DEFAULT ''"
            name = definition.split(None, 1)[0]
            if name not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
                present.add(name)
    execute_schema_script(connection, PARALLEL_ORCHESTRATION_INDEXES_AND_TRIGGERS)
    reconcile_stale_triggers(connection)
