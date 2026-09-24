from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .library_registry import (
    LIBRARY_STATUSES,
    LibraryManifestError,
    activation_bundle_for_profile,
    compare_semantic_versions,
    library_content_hash,
    probe_activation,
    validate_library_manifest,
)
from .schema_version import (
    begin_schema_migration,
    execute_schema_script,
    record_schema_version,
    require_compatible_schema,
)
from .state_permissions import ensure_state_directory, secure_state_file


CONTROL_PLANE_SCHEMA_VERSION = 4


PROVIDERS = {"codex", "claude", "openai", "anthropic", "grok", "xai", "custom"}
AUTH_METHODS = {"subscription", "api_key", "enterprise_route"}
ACCOUNT_STATUSES = {"disconnected", "connecting", "ready", "action_required", "unavailable", "error"}
PROVIDER_ACCOUNT_RECOVERY_CODES = {"", "credential_cleanup_failed"}
LEGACY_CREDENTIAL_CLEANUP_REASON = (
    "Provider credential cleanup failed; operator cleanup is required"
)
LOCATOR_PREFIXES = ("native-home://", "keychain://", "broker://", "secret-store://")
PROFILE_ACCOUNT_PROVIDERS = {
    "codex-cli": {"codex", "openai"},
    "claude-code": {"claude", "anthropic"},
    "grok-build": {"grok", "xai"},
}
WORKSPACE_ACCOUNT_POLICIES = {"legacy", "personal_preferred", "personal_required"}
ACCOUNT_SWITCH_BLOCKED_WORKER_STATES = {
    "starting",
    "running",
    "terminating",
    "termination_failed",
    "terminated",
}


class ControlPlaneError(RuntimeError):
    pass


class ControlPlaneConflict(ControlPlaneError):
    pass


class ProviderProjectionPending(ControlPlaneConflict):
    """An unfinished native projection blocks every alternative account route."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_json(value: object, fallback: object) -> object:
    try:
        return json.loads(str(value or ""))
    except json.JSONDecodeError:
        return fallback


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _worker_updated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _grant_scope_union(grant: sqlite3.Row | dict[str, Any]) -> set[str]:
    scopes = {str(value) for value in _parse_json(grant["scopes_json"], []) if str(value)}
    plan = _parse_json(grant["installation_plan_json"], []) if "installation_plan_json" in grant.keys() else []
    if isinstance(plan, list):
        for item in plan:
            if isinstance(item, dict):
                scopes.update(str(value) for value in item.get("scopes", []) if str(value))
    return scopes


def _merge_bootstrap(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if key == "files" and isinstance(value, list):
            prior = list(current) if isinstance(current, list) else []
            by_path = {
                str(item.get("path") or "").strip(): index
                for index, item in enumerate(prior)
                if isinstance(item, dict) and str(item.get("path") or "").strip()
            }
            for item in value:
                path = str(item.get("path") or "").strip() if isinstance(item, dict) else ""
                if path and path in by_path:
                    prior[by_path[path]] = item
                else:
                    if path:
                        by_path[path] = len(prior)
                    prior.append(item)
            merged[key] = prior
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_bootstrap(current, value)
        else:
            merged[key] = value
    return merged


def _library_activation(manifest: object, *, profile: str | None = None) -> dict[str, Any] | None:
    try:
        normalized = validate_library_manifest(manifest)
        selected_profile = profile or str(normalized["supported_profiles"][0])
        return activation_bundle_for_profile(normalized, selected_profile)
    except LibraryManifestError:
        return None


def _library_confirmation_snapshot(library: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    manifest = _parse_json(library["manifest_json"], {})
    try:
        normalized = validate_library_manifest(manifest)
    except LibraryManifestError as exc:
        raise ControlPlaneConflict(f"Library manifest integrity check failed: {exc}") from exc
    if not hmac.compare_digest(str(library["content_hash"]), str(normalized["content_hash"])):
        raise ControlPlaneConflict("Library content integrity check failed")
    stored_provenance = _parse_json(library["provenance"], {})
    stored_profiles = sorted(
        {str(value).strip() for value in _parse_json(library["supported_profiles_json"], []) if str(value).strip()}
    )
    stored_scopes = sorted(
        {str(value).strip() for value in _parse_json(library["scopes_json"], []) if str(value).strip()}
    )
    if (
        str(library["stable_id"]) != normalized["stable_id"]
        or str(library["version"]) != normalized["version"]
        or stored_provenance != normalized["provenance"]
        or stored_profiles != normalized["supported_profiles"]
        or stored_scopes != normalized["requested_scopes"]
    ):
        raise ControlPlaneConflict("Library registry metadata integrity check failed")
    supported_profiles = sorted(
        {str(value).strip() for value in _parse_json(library["supported_profiles_json"], []) if str(value).strip()}
    )
    scopes = sorted(
        {str(value).strip() for value in _parse_json(library["scopes_json"], []) if str(value).strip()}
    )
    manifest_dict = normalized
    return {
        "library_id": str(library["library_id"]),
        "stable_id": str(library["stable_id"]),
        "version": str(library["version"]),
        "content_hash": str(library["content_hash"]),
        "provenance": normalized["provenance"],
        "supported_profiles": supported_profiles,
        "allowed_scopes": scopes,
        "display_label": str(
            manifest_dict.get("label")
            or manifest_dict.get("name")
            or library["stable_id"]
        )[:200],
        "manifest_sha256": "sha256:" + hashlib.sha256(_json(manifest_dict).encode("utf-8")).hexdigest(),
        "activation_sha256": library_content_hash(normalized["activation"]),
    }


def _validate_locator(value: str) -> str:
    locator = str(value or "").strip()
    if len(locator) > 512 or not locator.startswith(LOCATOR_PREFIXES) or any(character in locator for character in "\r\n\0"):
        raise ControlPlaneError("A provider-owned opaque secret locator is required; raw credentials are not accepted")
    return locator


class ControlPlaneStore:
    """Metadata-only user control plane. Credential bytes never enter this store."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        ensure_state_directory(self.db_path.parent)
        self._initialize()
        secure_state_file(self.db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if os.name != "nt":
                for path in (
                    self.db_path,
                    Path(f"{self.db_path}-wal"),
                    Path(f"{self.db_path}-shm"),
                ):
                    secure_state_file(path)
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            begin_schema_migration(conn)
            require_compatible_schema(
                conn,
                component="control_plane",
                target_version=CONTROL_PLANE_SCHEMA_VERSION,
            )
            execute_schema_script(
                conn,
                """
                CREATE TABLE IF NOT EXISTS provider_accounts (
                    account_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    label TEXT NOT NULL,
                    auth_method TEXT NOT NULL,
                    platform_support TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    last_verified_at REAL,
                    last_used_at REAL,
                    reconnect_reason TEXT NOT NULL DEFAULT '',
                    recovery_code TEXT NOT NULL DEFAULT '',
                    secret_locator TEXT NOT NULL,
                    observed_runs INTEGER NOT NULL DEFAULT 0,
                    observed_failures INTEGER NOT NULL DEFAULT 0,
                    observed_duration_seconds REAL NOT NULL DEFAULT 0,
                    observed_input_tokens INTEGER,
                    observed_output_tokens INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_provider_accounts_owner
                    ON provider_accounts(tenant_id, owner_id, provider, created_at, account_id);
                CREATE TABLE IF NOT EXISTS provider_account_leases (
                    lease_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES provider_accounts(account_id) ON DELETE CASCADE,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    released_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_provider_account_leases_active
                    ON provider_account_leases(account_id, lane, released_at, expires_at);
                CREATE TABLE IF NOT EXISTS provider_account_projections (
                    lease_id TEXT PRIMARY KEY REFERENCES provider_account_leases(lease_id) ON DELETE CASCADE,
                    account_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    recovery_token TEXT NOT NULL DEFAULT '',
                    recovery_expires_at REAL,
                    receipt_hash TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_provider_projection
                    ON provider_account_projections(account_id) WHERE state != 'complete';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_provider_projection_worker
                    ON provider_account_projections(worker_id) WHERE state != 'complete';
                CREATE TABLE IF NOT EXISTS control_plane_connections (
                    connection_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    secret_locator TEXT NOT NULL,
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    last_verified_at REAL,
                    last_used_at REAL,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_control_plane_connections_owner
                    ON control_plane_connections(tenant_id, owner_id, created_at, connection_id);
                CREATE TABLE IF NOT EXISTS control_plane_library (
                    library_id TEXT PRIMARY KEY,
                    stable_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    supported_profiles_json TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    status_reason TEXT NOT NULL DEFAULT '',
                    published_by TEXT NOT NULL DEFAULT '',
                    status_updated_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(stable_id, version, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_control_plane_library_status
                    ON control_plane_library(status, stable_id, version, library_id);
                CREATE TABLE IF NOT EXISTS control_plane_library_events (
                    event_id TEXT PRIMARY KEY,
                    library_id TEXT NOT NULL REFERENCES control_plane_library(library_id) ON DELETE RESTRICT,
                    actor_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    prior_status TEXT,
                    next_status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_control_plane_library_events_item
                    ON control_plane_library_events(library_id, created_at, event_id);
                CREATE TABLE IF NOT EXISTS control_plane_library_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    library_id TEXT REFERENCES control_plane_library(library_id),
                    resolution_reason TEXT NOT NULL DEFAULT '',
                    resolved_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    resolved_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_control_plane_library_proposals_review
                    ON control_plane_library_proposals(tenant_id, status, created_at, proposal_id);
                CREATE TABLE IF NOT EXISTS control_plane_pending_changes (
                    change_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    confirmation_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    resolved_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_control_plane_pending_owner
                    ON control_plane_pending_changes(tenant_id, owner_id, status, expires_at);
                CREATE TABLE IF NOT EXISTS workspace_capability_grants (
                    grant_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    library_id TEXT REFERENCES control_plane_library(library_id),
                    connection_id TEXT REFERENCES control_plane_connections(connection_id),
                    account_id TEXT REFERENCES provider_accounts(account_id),
                    scopes_json TEXT NOT NULL,
                    prior_bootstrap_bundle_json TEXT NOT NULL DEFAULT '{}',
                    applied_bootstrap_bundle_json TEXT NOT NULL DEFAULT '{}',
                    installation_plan_json TEXT NOT NULL DEFAULT '[]',
                    probe_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS workspace_templates (
                    template_id TEXT PRIMARY KEY,
                    lineage_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    parent_template_id TEXT,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(tenant_id, owner_id, lineage_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_templates_owner
                    ON workspace_templates(tenant_id, owner_id, created_at, template_id);
                CREATE TABLE IF NOT EXISTS workspace_template_instantiations (
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    project_id TEXT,
                    worker_id TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    PRIMARY KEY(tenant_id, owner_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS workspace_duplications (
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    source_worker_id TEXT NOT NULL,
                    requested_name TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    project_id TEXT,
                    worker_id TEXT,
                    response_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    failed_at REAL,
                    PRIMARY KEY(tenant_id, owner_id, idempotency_key)
                );
                """
            )
            provider_account_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(provider_accounts)").fetchall()
            }
            if "observed_failures" not in provider_account_columns:
                conn.execute(
                    "ALTER TABLE provider_accounts "
                    "ADD COLUMN observed_failures INTEGER NOT NULL DEFAULT 0"
                )
            if "observed_duration_seconds" not in provider_account_columns:
                conn.execute(
                    "ALTER TABLE provider_accounts "
                    "ADD COLUMN observed_duration_seconds REAL NOT NULL DEFAULT 0"
                )
            if "recovery_code" not in provider_account_columns:
                conn.execute(
                    "ALTER TABLE provider_accounts "
                    "ADD COLUMN recovery_code TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    """
                    UPDATE provider_accounts
                    SET recovery_code = 'credential_cleanup_failed'
                    WHERE auth_method = 'subscription'
                      AND status = 'action_required'
                      AND recovery_code = ''
                      AND reconnect_reason = ?
                    """,
                    (LEGACY_CREDENTIAL_CLEANUP_REASON,),
                )
            grant_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(workspace_capability_grants)").fetchall()
            }
            if "prior_bootstrap_bundle_json" not in grant_columns:
                conn.execute(
                    "ALTER TABLE workspace_capability_grants "
                    "ADD COLUMN prior_bootstrap_bundle_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "applied_bootstrap_bundle_json" not in grant_columns:
                conn.execute(
                    "ALTER TABLE workspace_capability_grants "
                    "ADD COLUMN applied_bootstrap_bundle_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "installation_plan_json" not in grant_columns:
                conn.execute(
                    "ALTER TABLE workspace_capability_grants "
                    "ADD COLUMN installation_plan_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "probe_json" not in grant_columns:
                conn.execute(
                    "ALTER TABLE workspace_capability_grants "
                    "ADD COLUMN probe_json TEXT NOT NULL DEFAULT '{}'"
                )
            library_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(control_plane_library)").fetchall()
            }
            if "status_reason" not in library_columns:
                conn.execute(
                    "ALTER TABLE control_plane_library ADD COLUMN status_reason TEXT NOT NULL DEFAULT ''"
                )
            if "published_by" not in library_columns:
                conn.execute(
                    "ALTER TABLE control_plane_library ADD COLUMN published_by TEXT NOT NULL DEFAULT ''"
                )
            if "status_updated_at" not in library_columns:
                conn.execute(
                    "ALTER TABLE control_plane_library ADD COLUMN status_updated_at REAL"
                )
            record_schema_version(
                conn,
                component="control_plane",
                version=CONTROL_PLANE_SCHEMA_VERSION,
            )

    def _row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for source, target in (
            ("scopes_json", "scopes"),
            ("supported_profiles_json", "supported_profiles"),
            ("manifest_json", "manifest"),
            ("payload_json", "payload"),
            ("installation_plan_json", "installation_plan"),
            ("probe_json", "health_probe"),
        ):
            if source in result:
                result[target] = _parse_json(
                    result.pop(source),
                    [] if target in {"scopes", "supported_profiles", "installation_plan"} else {},
                )
        if "provenance" in result:
            parsed_provenance = _parse_json(result["provenance"], None)
            if isinstance(parsed_provenance, dict):
                result["provenance"] = parsed_provenance
        for name in ("is_default",):
            if name in result:
                result[name] = bool(result[name])
        result.pop("confirmation_hash", None)
        if str(result.get("secret_locator") or "").startswith("native-home://current-claude/"):
            result["auth_source"] = "current_os_claude_subscription"
            result["connection_action"] = "verify"
        if "secret_locator" in result:
            result["credential_route"] = "native" if result.get("auth_method") == "subscription" or result["secret_locator"] == "native-home://api-key" else "broker"
        result.pop("secret_locator", None)
        result.pop("prior_bootstrap_bundle_json", None)
        result.pop("applied_bootstrap_bundle_json", None)
        return result

    def create_provider_account(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        provider: str,
        label: str,
        auth_method: str,
        platform_support: str,
        secret_locator: str,
        make_default: bool = False,
        status: str = "disconnected",
    ) -> dict[str, Any]:
        provider = str(provider).strip().lower()
        auth_method = str(auth_method).strip().lower()
        status = str(status).strip().lower()
        if provider not in PROVIDERS:
            raise ControlPlaneError("Unsupported provider")
        if auth_method not in AUTH_METHODS:
            raise ControlPlaneError("Unsupported provider authentication method")
        if status not in ACCOUNT_STATUSES:
            raise ControlPlaneError("Unsupported provider account status")
        account_id = _id("acct")
        requested_locator = str(secret_locator or "").strip()
        if requested_locator in {"native-home://auto", "secret-store://auto"}:
            requested_locator = requested_locator.removesuffix("auto") + account_id
        locator = _validate_locator(requested_locator)
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if locator.startswith("native-home://current-claude/"):
                if provider != "claude" or auth_method != "subscription":
                    raise ControlPlaneError("Current native account route is invalid")
                current = conn.execute("SELECT * FROM provider_accounts WHERE secret_locator LIKE 'native-home://current-claude/%' LIMIT 1").fetchone()
                if current is not None:
                    if (current["tenant_id"], current["owner_id"], current["secret_locator"]) != (tenant_id, owner_id, locator):
                        raise ControlPlaneConflict("Another OS sign-in is already connected. Disconnect and forget it before connecting a different account")
                    return self._row(current) or {}
            try:
                account_limit = max(
                    1,
                    min(100, int(os.environ.get("GLASSHIVE_MAX_PROVIDER_ACCOUNTS_PER_USER", "8"))),
                )
            except ValueError:
                account_limit = 8
            account_count = conn.execute(
                """
                SELECT COUNT(*) FROM provider_accounts
                WHERE tenant_id = ? AND owner_id = ? AND status != 'disconnected'
                """,
                (tenant_id, owner_id),
            ).fetchone()[0]
            if int(account_count) >= account_limit:
                raise ControlPlaneConflict("Provider account limit reached for this user")
            has_default = conn.execute(
                """
                SELECT 1 FROM provider_accounts
                WHERE tenant_id = ? AND owner_id = ? AND provider = ? AND is_default = 1
                LIMIT 1
                """,
                (tenant_id, owner_id, provider),
            ).fetchone()
            make_default = bool(make_default or has_default is None)
            if make_default:
                conn.execute(
                    "UPDATE provider_accounts SET is_default = 0, updated_at = ? WHERE tenant_id = ? AND owner_id = ? AND provider = ?",
                    (now, tenant_id, owner_id, provider),
                )
            conn.execute(
                """
                INSERT INTO provider_accounts
                    (account_id, tenant_id, owner_id, provider, label, auth_method, platform_support,
                     is_default, status, secret_locator, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    tenant_id,
                    owner_id,
                    provider,
                    str(label).strip()[:160],
                    auth_method,
                    str(platform_support).strip()[:80],
                    1 if make_default else 0,
                    status,
                    locator,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM provider_accounts WHERE account_id = ?", (account_id,)).fetchone()
        assert row is not None
        return self._row(row) or {}

    def list_provider_accounts(self, *, tenant_id: str, owner_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_accounts WHERE tenant_id = ? AND owner_id = ? ORDER BY created_at, rowid",
                (tenant_id, owner_id),
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def get_provider_account(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provider_accounts WHERE account_id = ? AND tenant_id = ? AND owner_id = ?",
                (account_id, tenant_id, owner_id),
            ).fetchone()
        return self._row(row)

    def get_provider_account_record(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, Any] | None:
        """Internal credential-locator lookup. Never return this record through an API response."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provider_accounts WHERE account_id = ? AND tenant_id = ? AND owner_id = ?",
                (account_id, tenant_id, owner_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_provider_account_status(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        status: str,
        reconnect_reason: str = "",
        verified: bool = False,
        recovery_code: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized not in ACCOUNT_STATUSES:
            raise ControlPlaneError("Unsupported provider account status")
        normalized_recovery = None if recovery_code is None else str(recovery_code or "").strip()
        if normalized_recovery is not None and normalized_recovery not in PROVIDER_ACCOUNT_RECOVERY_CODES:
            raise ControlPlaneError("Unsupported provider account recovery code")
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_accounts
                SET status = ?, reconnect_reason = ?, recovery_code = COALESCE(?, recovery_code),
                    last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END,
                    updated_at = ?
                WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (
                    normalized,
                    str(reconnect_reason or "")[:500],
                    normalized_recovery,
                    1 if verified else 0,
                    now,
                    now,
                    account_id,
                    tenant_id,
                    owner_id,
                ),
            )
            if not cursor.rowcount:
                raise ControlPlaneError("Provider account not found for this user")
            row = conn.execute("SELECT * FROM provider_accounts WHERE account_id = ?", (account_id,)).fetchone()
        return self._row(row) or {}

    def disconnect_provider_account(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        reconnect_reason: str = "Disconnected by user",
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._deny_pending_projection(conn, account_id)
            row = conn.execute(
                "SELECT * FROM provider_accounts WHERE account_id = ? AND tenant_id = ? AND owner_id = ?",
                (account_id, tenant_id, owner_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneError("Provider account not found for this user")
            if str(row["secret_locator"]).startswith("native-home://current-claude/"):
                unfinished = conn.execute("SELECT 1 FROM provider_account_leases WHERE account_id = ? AND released_at IS NULL LIMIT 1", (account_id,)).fetchone()
                if unfinished:
                    raise ControlPlaneConflict("Current native account is in use or needs confirmed process recovery")
            conn.execute(
                """
                UPDATE provider_account_leases SET released_at = ?
                WHERE account_id = ? AND tenant_id = ? AND owner_id = ? AND released_at IS NULL
                """,
                (now, account_id, tenant_id, owner_id),
            )
            conn.execute(
                """
                UPDATE provider_accounts
                SET status = 'disconnected', reconnect_reason = ?, recovery_code = '',
                    is_default = 0, updated_at = ?
                WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (str(reconnect_reason or "")[:500], now, account_id, tenant_id, owner_id),
            )
            updated = conn.execute(
                "SELECT * FROM provider_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return self._row(updated) or {}

    def forget_provider_account(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, str]:
        """Remove disconnected account metadata after native credentials are gone."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._deny_pending_projection(conn, account_id)
            row = conn.execute(
                """
                SELECT status FROM provider_accounts
                WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (account_id, tenant_id, owner_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneError("Provider account not found for this user")
            if str(row["status"] or "").strip().lower() != "disconnected":
                raise ControlPlaneConflict("Disconnect the provider account before forgetting it")
            active_lease = conn.execute(
                """
                SELECT 1 FROM provider_account_leases
                WHERE account_id = ? AND released_at IS NULL AND expires_at > ?
                LIMIT 1
                """,
                (account_id, time.time()),
            ).fetchone()
            if active_lease is not None:
                raise ControlPlaneConflict("Provider account is still in use")
            conn.execute(
                """
                DELETE FROM workspace_capability_grants
                WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (account_id, tenant_id, owner_id),
            )
            conn.execute(
                """
                DELETE FROM provider_accounts
                WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (account_id, tenant_id, owner_id),
            )
        return {"account_id": account_id, "status": "forgotten"}

    @staticmethod
    def _projection_fence(conn, binding: dict, *, now: float, recovery_token: str = ""):
        encoded = json.dumps(binding, sort_keys=True)
        row = conn.execute("SELECT * FROM provider_account_projections WHERE lease_id = ? AND state != 'complete'", (binding["lease_id"],)).fetchone()
        if row is None or row["binding_json"] != encoded:
            raise ProviderProjectionPending("Credential projection identity changed")
        lease = conn.execute("SELECT * FROM provider_account_leases WHERE lease_id = ?", (binding["lease_id"],)).fetchone()
        if lease is None or lease["released_at"] is not None or any(str(lease[key]) != binding[key] for key in ("tenant_id", "owner_id", "account_id", "worker_id", "run_id")):
            raise ProviderProjectionPending("Credential projection lease is no longer exclusive")
        if recovery_token:
            if row["recovery_token"] != recovery_token or float(row["recovery_expires_at"] or 0) <= now:
                raise ProviderProjectionPending("Credential recovery authority expired or changed")
        elif row["recovery_token"] or float(lease["expires_at"]) <= now:
            raise ProviderProjectionPending("Credential projection requires fenced recovery")
        return row

    @staticmethod
    def _deny_pending_projection(conn, account_id: str) -> None:
        if conn.execute("SELECT 1 FROM provider_account_projections WHERE account_id = ? AND state != 'complete'", (account_id,)).fetchone():
            raise ProviderProjectionPending("Provider account has unfinished credential recovery")

    def begin_provider_projection(self, *, binding: dict, metadata: dict) -> dict:
        from .credential_projection import ProjectionBinding
        from dataclasses import asdict
        exact = asdict(ProjectionBinding(**binding))
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._deny_pending_projection(conn, exact["account_id"])
            if conn.execute("SELECT 1 FROM provider_account_projections WHERE worker_id = ? AND state != 'complete'", (exact["worker_id"],)).fetchone():
                raise ProviderProjectionPending("Worker has unfinished native credential recovery")
            lease = conn.execute("SELECT * FROM provider_account_leases WHERE lease_id = ?", (exact["lease_id"],)).fetchone()
            if lease is None or lease["released_at"] is not None or float(lease["expires_at"]) <= now or any(str(lease[k]) != exact[k] for k in ("account_id", "tenant_id", "owner_id", "worker_id", "run_id")):
                raise ProviderProjectionPending("Projection requires the exact active account lease")
            conn.execute("INSERT INTO provider_account_projections (lease_id, account_id, tenant_id, owner_id, worker_id, binding_json, metadata_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                         (exact["lease_id"], exact["account_id"], exact["tenant_id"], exact["owner_id"], exact["worker_id"], json.dumps(exact, sort_keys=True), json.dumps(metadata, sort_keys=True), now, now))
        return {"binding": exact, "metadata": metadata, "state": "pending"}

    def pending_provider_projections(self, *, account_id: str | None = None, worker_id: str | None = None) -> list[dict]:
        # Internal recovery inventory; never expose private paths or fences on a public API.
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM provider_account_projections WHERE state != 'complete' AND (? IS NULL OR account_id = ?) AND (? IS NULL OR worker_id = ?) ORDER BY created_at, lease_id", (account_id, account_id, worker_id, worker_id)).fetchall()
        return [{"binding": json.loads(row["binding_json"]), "metadata": json.loads(row["metadata_json"]), "state": row["state"]} for row in rows]

    def assert_provider_projection(self, *, binding: dict, recovery_token: str = "") -> None:
        with self._connect() as conn:
            self._projection_fence(conn, binding, now=time.time(), recovery_token=recovery_token)

    def quarantine_provider_projection(self, *, binding: dict, recovery_token: str = "") -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT recovery_token FROM provider_account_projections WHERE lease_id = ? AND binding_json = ? AND state != 'complete'", (binding["lease_id"], json.dumps(binding, sort_keys=True))).fetchone()
            if row is None or str(row["recovery_token"] or "") != recovery_token:
                raise ProviderProjectionPending("Credential recovery authority changed")
            cursor = conn.execute("UPDATE provider_account_projections SET state = 'quarantined', recovery_token = '', recovery_expires_at = NULL, updated_at = ? WHERE lease_id = ? AND binding_json = ? AND state != 'complete'", (time.time(), binding["lease_id"], json.dumps(binding, sort_keys=True)))
            if not cursor.rowcount: raise ProviderProjectionPending("Credential projection identity changed")
            conn.execute("UPDATE provider_accounts SET status = 'action_required', recovery_code = 'credential_cleanup_failed', reconnect_reason = 'Credential recovery must finish before this account can be used', updated_at = ? WHERE account_id = ? AND tenant_id = ? AND owner_id = ?", (time.time(), binding["account_id"], binding["tenant_id"], binding["owner_id"]))

    def claim_provider_projection_recovery(self, *, binding: dict, ttl_seconds: int = 180) -> str:
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM provider_account_projections WHERE lease_id = ? AND binding_json = ? AND state != 'complete'", (binding["lease_id"], json.dumps(binding, sort_keys=True))).fetchone()
            if row is None: raise ProviderProjectionPending("Credential recovery binding changed")
            lease = conn.execute("SELECT * FROM provider_account_leases WHERE lease_id = ? AND released_at IS NULL", (binding["lease_id"],)).fetchone()
            if lease is None: raise ProviderProjectionPending("Credential recovery lease changed")
            if row["state"] == "pending" and float(lease["expires_at"]) > now:
                raise ProviderProjectionPending("Credential projection is still active")
            if row["recovery_token"] and float(row["recovery_expires_at"] or 0) > now:
                raise ProviderProjectionPending("Credential recovery is already in progress")
            newer = conn.execute("SELECT 1 FROM provider_account_leases WHERE account_id = ? AND lease_id != ? AND released_at IS NULL AND expires_at > ?", (binding["account_id"], binding["lease_id"], now)).fetchone()
            if newer: raise ProviderProjectionPending("Another account lease prevents recovery")
            conn.execute("UPDATE provider_account_projections SET state = 'recovering', recovery_token = ?, recovery_expires_at = ?, updated_at = ? WHERE lease_id = ?", (token, now + max(15, min(ttl_seconds, 3600)), now, binding["lease_id"]))
        return token

    def complete_provider_projection(self, *, binding: dict, receipt_hash: str, recovery_token: str = "") -> None:
        if len(receipt_hash) != 64 or any(c not in "0123456789abcdef" for c in receipt_hash):
            raise ControlPlaneError("Completed projection receipt hash is required")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._projection_fence(conn, binding, now=now, recovery_token=recovery_token)
            metadata = json.loads(row["metadata_json"])
            try:
                descriptor = os.open(metadata["receipt"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                        raise ProviderProjectionPending("Completed credential receipt is not private")
                    content = stream.read(65537)
                receipt = json.loads(content)
                if (len(content) > 65536 or hashlib.sha256(content).hexdigest() != receipt_hash
                        or receipt.get("phase") != "complete" or receipt.get("binding") != binding
                        or receipt.get("artifacts") != metadata["artifacts"]):
                    raise ProviderProjectionPending("Credential receipt does not prove completed cleanup")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ProviderProjectionPending("Completed credential receipt is unavailable") from exc
            conn.execute("UPDATE provider_account_projections SET state = 'complete', receipt_hash = ?, recovery_token = '', recovery_expires_at = NULL, updated_at = ? WHERE lease_id = ?", (receipt_hash, now, binding["lease_id"]))
            conn.execute("UPDATE provider_account_leases SET released_at = ? WHERE lease_id = ? AND released_at IS NULL", (now, binding["lease_id"]))
            # Cleanup resolution does not assert provider authentication/model entitlement.
            conn.execute("UPDATE provider_accounts SET recovery_code = '', updated_at = ? WHERE account_id = ? AND recovery_code = 'credential_cleanup_failed'", (now, binding["account_id"]))

    def acquire_provider_lease(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        lane: str,
        worker_id: str,
        run_id: str,
        ttl_seconds: int,
        now: float | None = None,
        allowed_statuses: tuple[str, ...] = ("ready",),
        required_recovery_code: str | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        ttl = max(15, min(int(ttl_seconds), 24 * 60 * 60))
        normalized_statuses = {
            str(status or "").strip().lower() for status in allowed_statuses
        }
        if not normalized_statuses or not normalized_statuses.issubset(ACCOUNT_STATUSES):
            raise ControlPlaneError("Provider lease allowed statuses are invalid")
        normalized_recovery = (
            None if required_recovery_code is None else str(required_recovery_code or "").strip()
        )
        if normalized_recovery is not None and normalized_recovery not in PROVIDER_ACCOUNT_RECOVERY_CODES:
            raise ControlPlaneError("Provider lease recovery code is invalid")
        lease_id = _id("lease")
        normalized_lane = str(lane or "default")[:80]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._deny_pending_projection(conn, account_id)
            account = conn.execute(
                "SELECT account_id, status, recovery_code, secret_locator FROM provider_accounts WHERE account_id = ? AND tenant_id = ? AND owner_id = ?",
                (account_id, tenant_id, owner_id),
            ).fetchone()
            if account is None:
                raise ControlPlaneError("Provider account not found for this user")
            if normalized_lane.endswith(":interactive"):
                runs_table = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'runs'
                    """
                ).fetchone()
                if runs_table is not None:
                    workspace_run = conn.execute(
                        """
                        SELECT 1 FROM runs
                        WHERE worker_id = ? AND state IN ('queued', 'running')
                        LIMIT 1
                        """,
                        (worker_id,),
                    ).fetchone()
                    if workspace_run is not None:
                        raise ControlPlaneConflict(
                            "Provider account work is starting or running"
                        )
                fence_table = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'provider_account_run_fences'
                    """
                ).fetchone()
                if fence_table is not None:
                    active_run = conn.execute(
                        """
                        SELECT 1
                        FROM provider_account_run_fences AS fences
                        JOIN runs ON runs.run_id = fences.run_id
                        WHERE fences.account_id = ?
                          AND runs.state IN ('queued', 'running')
                        LIMIT 1
                        """,
                        (account_id,),
                    ).fetchone()
                    if active_run is not None:
                        raise ControlPlaneConflict(
                            "Provider account work is starting or running"
                        )
            active = conn.execute(
                """
                SELECT lease_id FROM provider_account_leases
                WHERE account_id = ? AND released_at IS NULL AND (expires_at > ? OR ?)
                ORDER BY acquired_at DESC LIMIT 1
                """,
                (account_id, timestamp, str(account["secret_locator"]).startswith("native-home://current-claude/")),
            ).fetchone()
            if active is not None:
                raise ControlPlaneConflict("Provider account is already in use")
            if str(account["status"] or "").strip().lower() not in normalized_statuses:
                raise ControlPlaneConflict("Provider account is not ready for mission use")
            if normalized_recovery is not None and str(account["recovery_code"] or "") != normalized_recovery:
                raise ControlPlaneConflict("Provider account recovery state changed; refresh and try again")
            conn.execute(
                """
                INSERT INTO provider_account_leases
                    (lease_id, account_id, tenant_id, owner_id, lane, worker_id, run_id,
                     acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    account_id,
                    tenant_id,
                    owner_id,
                    normalized_lane,
                    worker_id,
                    run_id,
                    timestamp,
                    timestamp,
                    timestamp + ttl,
                ),
            )
            row = conn.execute("SELECT * FROM provider_account_leases WHERE lease_id = ?", (lease_id,)).fetchone()
        return dict(row) if row is not None else {}

    def record_provider_account_usage(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        succeeded: bool,
        duration_seconds: float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically add usage that GlassHive directly observed for one account-backed run.

        These counters are local run telemetry, never provider-reported balance, quota, or billing.
        Token totals remain unknown until the selected worker runtime returns explicit token fields.
        """

        if not isinstance(succeeded, bool):
            raise ControlPlaneError("Provider account observed outcome is invalid")
        if isinstance(duration_seconds, bool):
            raise ControlPlaneError("Provider account observed duration is invalid")
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ControlPlaneError("Provider account observed duration is invalid") from exc
        if not math.isfinite(duration) or duration < 0:
            raise ControlPlaneError("Provider account observed duration is invalid")

        def observed_token(value: int | None) -> int | None:
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ControlPlaneError("Provider account observed token count is invalid")
            return value

        input_count = observed_token(input_tokens)
        output_count = observed_token(output_tokens)
        timestamp = float(now if now is not None else time.time())
        if not math.isfinite(timestamp):
            raise ControlPlaneError("Provider account observed timestamp is invalid")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE provider_accounts
                SET last_used_at = ?,
                    observed_runs = observed_runs + 1,
                    observed_failures = observed_failures + ?,
                    observed_duration_seconds = observed_duration_seconds + ?,
                    observed_input_tokens = CASE
                        WHEN ? IS NULL THEN observed_input_tokens
                        ELSE COALESCE(observed_input_tokens, 0) + ?
                    END,
                    observed_output_tokens = CASE
                        WHEN ? IS NULL THEN observed_output_tokens
                        ELSE COALESCE(observed_output_tokens, 0) + ?
                    END,
                    updated_at = ?
                WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (
                    timestamp,
                    0 if succeeded else 1,
                    duration,
                    input_count,
                    input_count,
                    output_count,
                    output_count,
                    timestamp,
                    account_id,
                    tenant_id,
                    owner_id,
                ),
            )
            if not cursor.rowcount:
                raise ControlPlaneError("Provider account not found for this user")
            row = conn.execute(
                "SELECT * FROM provider_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return self._row(row) or {}

    def active_provider_lease(self, account_id: str, lane: str, *, now: float | None = None) -> dict[str, Any] | None:
        timestamp = float(now if now is not None else time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM provider_account_leases
                WHERE account_id = ? AND lane = ? AND released_at IS NULL AND expires_at > ?
                ORDER BY acquired_at DESC LIMIT 1
                """,
                (account_id, lane, timestamp),
            ).fetchone()
        return dict(row) if row is not None else None

    def active_provider_account_lease(
        self, account_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Return the current lease that would block any new use of this account."""

        timestamp = float(now if now is not None else time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM provider_account_leases
                WHERE account_id = ? AND released_at IS NULL AND (expires_at > ? OR lease_id IN (SELECT lease_id FROM provider_account_projections WHERE state != 'complete') OR account_id IN (SELECT account_id FROM provider_accounts WHERE secret_locator LIKE 'native-home://current-claude/%'))
                ORDER BY acquired_at DESC LIMIT 1
                """,
                (account_id, timestamp),
            ).fetchone()
        return dict(row) if row is not None else None

    def unreleased_interactive_provider_leases(self) -> list[dict[str, Any]]:
        """Return setup leases that require credential-container cleanup after restart."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM provider_account_leases
                WHERE released_at IS NULL
                  AND lane IN ('codex-cli:interactive', 'claude-code:interactive', 'grok-build:interactive')
                ORDER BY acquired_at, lease_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def heartbeat_provider_lease(
        self,
        *,
        lease_id: str,
        tenant_id: str,
        owner_id: str,
        ttl_seconds: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        ttl = max(15, min(int(ttl_seconds), 24 * 60 * 60))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_account_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE lease_id = ? AND tenant_id = ? AND owner_id = ?
                  AND released_at IS NULL AND expires_at > ?
                """,
                (
                    timestamp,
                    timestamp + ttl,
                    lease_id,
                    tenant_id,
                    owner_id,
                    timestamp,
                ),
            )
            if not cursor.rowcount:
                raise ControlPlaneError("Provider account mission lease is no longer active")
            row = conn.execute(
                "SELECT * FROM provider_account_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def release_provider_lease(self, *, lease_id: str, tenant_id: str, owner_id: str, now: float | None = None) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM provider_account_projections WHERE lease_id = ? AND state != 'complete'", (lease_id,)).fetchone():
                raise ProviderProjectionPending("Credential recovery must complete before releasing its lease")
            cursor = conn.execute(
                """
                UPDATE provider_account_leases SET released_at = ?
                WHERE lease_id = ? AND tenant_id = ? AND owner_id = ? AND released_at IS NULL
                """,
                (float(now if now is not None else time.time()), lease_id, tenant_id, owner_id),
            )
        if not cursor.rowcount:
            raise ControlPlaneError("Provider account lease not found for this user")

    def create_connection(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        kind: str,
        adapter: str,
        label: str,
        status: str,
        secret_locator: str,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        connection_id = _id("conn")
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO control_plane_connections
                    (connection_id, tenant_id, owner_id, kind, adapter, label, status,
                     secret_locator, scopes_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    connection_id,
                    tenant_id,
                    owner_id,
                    str(kind).strip()[:80],
                    str(adapter).strip()[:160],
                    str(label).strip()[:160],
                    str(status).strip()[:80],
                    _validate_locator(secret_locator),
                    _json(scopes or []),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM control_plane_connections WHERE connection_id = ?", (connection_id,)).fetchone()
        return self._row(row) or {}

    def list_connections(self, *, tenant_id: str, owner_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM control_plane_connections WHERE tenant_id = ? AND owner_id = ? ORDER BY created_at, rowid",
                (tenant_id, owner_id),
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def register_library_item(
        self,
        *,
        stable_id: str,
        version: str,
        content_hash: str,
        provenance: str,
        supported_profiles: list[str],
        scopes: list[str],
        manifest: dict[str, Any],
        published_by: str = "deployment-registry",
    ) -> dict[str, Any]:
        try:
            normalized = validate_library_manifest(manifest)
        except LibraryManifestError as exc:
            raise ControlPlaneError(str(exc)) from exc
        if str(stable_id).strip().lower() != normalized["stable_id"]:
            raise ControlPlaneError("Library stable_id does not match its manifest")
        if str(version).strip() != normalized["version"]:
            raise ControlPlaneError("Library version does not match its manifest")
        if not hmac.compare_digest(str(content_hash).strip().lower(), normalized["content_hash"]):
            raise ControlPlaneError("Library content hash does not match its manifest")
        if sorted(set(supported_profiles)) != normalized["supported_profiles"]:
            raise ControlPlaneError("Library supported profiles do not match its manifest")
        if sorted(set(scopes)) != normalized["requested_scopes"]:
            raise ControlPlaneError("Library requested scopes do not match its manifest")
        normalized_provenance = normalized["provenance"]
        supplied_provenance = str(provenance or "").strip()
        if supplied_provenance not in {
            str(normalized_provenance["source"]),
            _json(normalized_provenance),
        }:
            raise ControlPlaneError("Library provenance does not match its manifest")
        library_id = _id("lib")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM control_plane_library WHERE stable_id = ? AND version = ?",
                (normalized["stable_id"], normalized["version"]),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["content_hash"]), normalized["content_hash"]):
                    raise ControlPlaneConflict(
                        "Library stable_id and version are already published with different content"
                    )
                if not hmac.compare_digest(str(existing["manifest_json"]), _json(normalized)):
                    raise ControlPlaneConflict(
                        "Library stable_id and version are already published with different metadata"
                    )
                return self._row(existing) or {}
            for dependency in normalized["dependencies"]:
                row = conn.execute(
                    """
                    SELECT * FROM control_plane_library
                    WHERE stable_id = ? AND version = ? AND content_hash = ? AND status = 'available'
                    """,
                    (
                        dependency["stable_id"],
                        dependency["version"],
                        dependency["content_hash"],
                    ),
                ).fetchone()
                if row is None:
                    raise ControlPlaneError(
                        f"Library dependency {dependency['stable_id']} {dependency['version']} is not available"
                    )
                dependency_manifest = _parse_json(row["manifest_json"], {})
                try:
                    dependency_normalized = validate_library_manifest(dependency_manifest)
                except LibraryManifestError as exc:
                    raise ControlPlaneError("Library dependency manifest is invalid") from exc
                if not set(normalized["supported_profiles"]).issubset(
                    set(dependency_normalized["supported_profiles"])
                ):
                    raise ControlPlaneError("Library dependency does not support every declared profile")
                if not set(dependency["scopes"]).issubset(
                    set(dependency_normalized["requested_scopes"])
                ):
                    raise ControlPlaneError("Library dependency scopes exceed its published manifest")
            conn.execute(
                """
                INSERT INTO control_plane_library
                    (library_id, stable_id, version, content_hash, provenance, supported_profiles_json,
                     scopes_json, manifest_json, status, status_reason, published_by,
                     status_updated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'available', '', ?, ?, ?, ?)
                """,
                (
                    library_id,
                    normalized["stable_id"],
                    normalized["version"],
                    normalized["content_hash"],
                    _json(normalized_provenance),
                    _json(normalized["supported_profiles"]),
                    _json(normalized["requested_scopes"]),
                    _json(normalized),
                    str(published_by or "deployment-registry").strip()[:200],
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO control_plane_library_events
                    (event_id, library_id, actor_id, event_type, prior_status, next_status, reason, created_at)
                VALUES (?, ?, ?, 'published', NULL, 'available', '', ?)
                """,
                (_id("levt"), library_id, str(published_by or "deployment-registry")[:200], now),
            )
            row = conn.execute("SELECT * FROM control_plane_library WHERE library_id = ?", (library_id,)).fetchone()
        return self._row(row) or {}

    def publish_library_manifest(self, *, manifest: dict[str, Any], published_by: str) -> dict[str, Any]:
        try:
            normalized = validate_library_manifest(manifest)
        except LibraryManifestError as exc:
            raise ControlPlaneError(str(exc)) from exc
        return self.register_library_item(
            stable_id=normalized["stable_id"],
            version=normalized["version"],
            content_hash=normalized["content_hash"],
            provenance=normalized["provenance"]["source"],
            supported_profiles=normalized["supported_profiles"],
            scopes=normalized["requested_scopes"],
            manifest=normalized,
            published_by=published_by,
        )

    def create_library_proposal(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            normalized = validate_library_manifest(manifest)
        except LibraryManifestError as exc:
            raise ControlPlaneError(str(exc)) from exc
        proposal_id = _id("lprop")
        now = time.time()
        with self._connect() as conn:
            pending_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM control_plane_library_proposals
                    WHERE tenant_id = ? AND owner_id = ? AND status = 'pending'
                    """,
                    (tenant_id, owner_id),
                ).fetchone()[0]
            )
            if pending_count >= 20:
                raise ControlPlaneConflict("Library proposal limit reached for this user")
            conn.execute(
                """
                INSERT INTO control_plane_library_proposals
                    (proposal_id, tenant_id, owner_id, manifest_json, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (proposal_id, tenant_id, owner_id, _json(normalized), now),
            )
            row = conn.execute(
                "SELECT * FROM control_plane_library_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return self._row(row) or {}

    def list_library_proposals(
        self,
        *,
        tenant_id: str,
        owner_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = ?"]
        values: list[object] = [tenant_id]
        if owner_id is not None:
            clauses.append("owner_id = ?")
            values.append(owner_id)
        if status:
            if status not in {"pending", "published", "rejected"}:
                raise ControlPlaneError("Unsupported Library proposal status")
            clauses.append("status = ?")
            values.append(status)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM control_plane_library_proposals WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, proposal_id DESC LIMIT 100",
                tuple(values),
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def review_library_proposal(
        self,
        *,
        proposal_id: str,
        tenant_id: str,
        action: str,
        reason: str,
        actor_id: str,
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"publish", "reject"}:
            raise ControlPlaneError("Unsupported Library proposal review action")
        with self._connect() as conn:
            proposal = conn.execute(
                """
                SELECT * FROM control_plane_library_proposals
                WHERE proposal_id = ? AND tenant_id = ?
                """,
                (proposal_id, tenant_id),
            ).fetchone()
        if proposal is None:
            raise ControlPlaneError("Library proposal not found")
        if str(proposal["status"]) != "pending":
            raise ControlPlaneConflict("Library proposal is already resolved")
        manifest = _parse_json(proposal["manifest_json"], {})
        library: dict[str, Any] | None = None
        next_status = "rejected"
        if normalized_action == "publish":
            if not isinstance(manifest, dict):
                raise ControlPlaneConflict("Library proposal manifest is invalid")
            library = self.publish_library_manifest(manifest=manifest, published_by=actor_id)
            next_status = "published"
        elif not str(reason or "").strip():
            raise ControlPlaneError("A rejection reason is required")
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE control_plane_library_proposals
                SET status = ?, library_id = ?, resolution_reason = ?, resolved_by = ?, resolved_at = ?
                WHERE proposal_id = ? AND tenant_id = ? AND status = 'pending'
                """,
                (
                    next_status,
                    str((library or {}).get("library_id") or "") or None,
                    str(reason or "").strip()[:1000],
                    str(actor_id or "operator")[:200],
                    now,
                    proposal_id,
                    tenant_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("Library proposal changed concurrently")
            reviewed = conn.execute(
                "SELECT * FROM control_plane_library_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        result = self._row(reviewed) or {}
        if library is not None:
            result["library"] = library
        return result

    def update_library_status(
        self,
        *,
        library_id: str,
        status: str,
        reason: str,
        actor_id: str,
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in LIBRARY_STATUSES:
            raise ControlPlaneError("Unsupported Library status")
        if normalized_status != "available" and not str(reason or "").strip():
            raise ControlPlaneError("A reason is required when disabling or removing a Library item")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM control_plane_library WHERE library_id = ?",
                (library_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneError("Library item not found")
            prior_status = str(row["status"])
            if prior_status == "removed" and normalized_status != "removed":
                raise ControlPlaneConflict("A removed Library item cannot be restored; publish a new version")
            if prior_status == normalized_status:
                return self._row(row) or {}
            if normalized_status == "available":
                _library_confirmation_snapshot(row)
                root_manifest = _parse_json(row["manifest_json"], {})
                pending_dependencies = list(
                    root_manifest.get("dependencies", []) if isinstance(root_manifest, dict) else []
                )
                restored_dependencies: set[tuple[str, str, str]] = set()
                while pending_dependencies:
                    dependency = pending_dependencies.pop(0)
                    if not isinstance(dependency, dict):
                        raise ControlPlaneConflict("Library dependency metadata is invalid")
                    identity = (
                        str(dependency.get("stable_id") or ""),
                        str(dependency.get("version") or ""),
                        str(dependency.get("content_hash") or ""),
                    )
                    if identity in restored_dependencies:
                        continue
                    dependency_row = conn.execute(
                        """
                        SELECT * FROM control_plane_library
                        WHERE stable_id = ? AND version = ? AND content_hash = ? AND status = 'available'
                        """,
                        identity,
                    ).fetchone()
                    if dependency_row is None:
                        raise ControlPlaneConflict(
                            "Library item cannot be restored until every pinned dependency is available"
                        )
                    _library_confirmation_snapshot(dependency_row)
                    restored_dependencies.add(identity)
                    dependency_manifest = _parse_json(dependency_row["manifest_json"], {})
                    if isinstance(dependency_manifest, dict):
                        pending_dependencies.extend(dependency_manifest.get("dependencies", []))
            if normalized_status in {"disabled", "removed"}:
                active_grants = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM workspace_capability_grants WHERE library_id = ? AND revoked_at IS NULL",
                        (library_id,),
                    ).fetchone()[0]
                )
                if active_grants:
                    raise ControlPlaneConflict(
                        f"Library item still has {active_grants} active workspace grant(s); remove them first"
                    )
                dependents = conn.execute(
                    "SELECT library_id, manifest_json FROM control_plane_library WHERE status = 'available' AND library_id != ?",
                    (library_id,),
                ).fetchall()
                identity = (str(row["stable_id"]), str(row["version"]), str(row["content_hash"]))
                for dependent in dependents:
                    manifest = _parse_json(dependent["manifest_json"], {})
                    dependencies = manifest.get("dependencies", []) if isinstance(manifest, dict) else []
                    if any(
                        isinstance(item, dict)
                        and (
                            str(item.get("stable_id")),
                            str(item.get("version")),
                            str(item.get("content_hash")),
                        )
                        == identity
                        for item in dependencies
                    ):
                        raise ControlPlaneConflict(
                            "Library item is required by an available dependent version; disable that item first"
                        )
            cursor = conn.execute(
                """
                UPDATE control_plane_library
                SET status = ?, status_reason = ?, status_updated_at = ?, updated_at = ?
                WHERE library_id = ? AND status = ?
                """,
                (
                    normalized_status,
                    str(reason or "").strip()[:1000],
                    now,
                    now,
                    library_id,
                    prior_status,
                ),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("Library status changed concurrently")
            conn.execute(
                """
                INSERT INTO control_plane_library_events
                    (event_id, library_id, actor_id, event_type, prior_status, next_status, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id("levt"),
                    library_id,
                    str(actor_id or "operator")[:200],
                    "status_changed",
                    prior_status,
                    normalized_status,
                    str(reason or "").strip()[:1000],
                    now,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM control_plane_library WHERE library_id = ?",
                (library_id,),
            ).fetchone()
        return self._row(updated) or {}

    def list_library_events(self, *, library_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, library_id, actor_id, event_type, prior_status,
                       next_status, reason, created_at
                FROM control_plane_library_events WHERE library_id = ?
                ORDER BY created_at, event_id
                """,
                (library_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_library(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM control_plane_library ORDER BY stable_id, version, rowid").fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._row(row) or {}
            item.pop("published_by", None)
            manifest = dict(item.get("manifest") or {})
            try:
                _library_confirmation_snapshot(row)
                activatable = True
            except ControlPlaneError:
                activatable = False
            manifest.pop("activation", None)
            manifest["activatable"] = activatable
            item["manifest"] = manifest
            item["activation_status"] = (
                "ready" if activatable and item.get("status") == "available" else "unavailable"
            )
            items.append(item)
        return items

    @staticmethod
    def _workspace_template_summary(row: sqlite3.Row, content: dict[str, Any]) -> dict[str, Any]:
        worker = content.get("worker") if isinstance(content.get("worker"), dict) else {}
        library_refs = content.get("library_refs") if isinstance(content.get("library_refs"), list) else []
        provider_account_ref = (
            content.get("worker", {}).get("provider_account_ref")
            if isinstance(content.get("worker"), dict)
            and isinstance(content.get("worker", {}).get("provider_account_ref"), dict)
            else None
        )
        return {
            "template_id": str(row["template_id"]),
            "lineage_id": str(row["lineage_id"]),
            "version": int(row["version"]),
            "parent_template_id": str(row["parent_template_id"] or "") or None,
            "name": str(row["name"]),
            "description": str(row["description"] or ""),
            "profile": str(row["profile"]),
            "execution_mode": str(row["execution_mode"]),
            "role": str(worker.get("role") or "main"),
            "tags": list(worker.get("tags") or []),
            "library_refs": library_refs,
            **({"provider_account_ref": dict(provider_account_ref)} if provider_account_ref else {}),
            "content_hash": str(row["content_hash"]),
            "created_at": float(row["created_at"]),
        }

    def create_workspace_template(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        name: str,
        description: str,
        content: dict[str, Any],
        lineage_id: str | None = None,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ControlPlaneError("Template name is required")
        encoded = _json(content)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ControlPlaneError("Workspace template exceeds the safe snapshot size limit")
        worker = content.get("worker") if isinstance(content.get("worker"), dict) else {}
        profile = str(worker.get("profile") or "").strip()
        execution_mode = str(worker.get("execution_mode") or "").strip()
        if not profile or execution_mode not in {"docker", "host"}:
            raise ControlPlaneError("Workspace template worker profile is invalid")
        template_id = _id("wst")
        now = time.time()
        requested_lineage = str(lineage_id or "").strip()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM workspace_templates WHERE tenant_id = ? AND owner_id = ?",
                    (tenant_id, owner_id),
                ).fetchone()[0]
            )
            if count >= 100:
                raise ControlPlaneConflict("Workspace template limit reached for this user")
            parent_template_id: str | None = None
            version = 1
            if requested_lineage:
                previous = conn.execute(
                    """
                    SELECT template_id, version FROM workspace_templates
                    WHERE tenant_id = ? AND owner_id = ? AND lineage_id = ?
                    ORDER BY version DESC LIMIT 1
                    """,
                    (tenant_id, owner_id, requested_lineage),
                ).fetchone()
                if previous is None:
                    raise ControlPlaneError("Template lineage not found for this user")
                parent_template_id = str(previous["template_id"])
                version = int(previous["version"]) + 1
            else:
                requested_lineage = _id("wsl")
            conn.execute(
                """
                INSERT INTO workspace_templates
                    (template_id, lineage_id, version, parent_template_id, tenant_id, owner_id,
                     name, description, profile, execution_mode, content_json, content_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    template_id,
                    requested_lineage,
                    version,
                    parent_template_id,
                    tenant_id,
                    owner_id,
                    clean_name[:160],
                    str(description or "").strip()[:1000],
                    profile,
                    execution_mode,
                    encoded,
                    _content_hash(content),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM workspace_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        assert row is not None
        return self._workspace_template_summary(row, content)

    def list_workspace_templates(self, *, tenant_id: str, owner_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workspace_templates
                WHERE tenant_id = ? AND owner_id = ?
                ORDER BY created_at DESC, template_id DESC LIMIT 100
                """,
                (tenant_id, owner_id),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            content = _parse_json(row["content_json"], {})
            if not isinstance(content, dict) or not hmac.compare_digest(
                str(row["content_hash"]), _content_hash(content)
            ):
                raise ControlPlaneConflict("Workspace template content integrity check failed")
            results.append(self._workspace_template_summary(row, content))
        return results

    def get_workspace_template(
        self,
        *,
        template_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM workspace_templates
                WHERE template_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (template_id, tenant_id, owner_id),
            ).fetchone()
        if row is None:
            return None
        content = _parse_json(row["content_json"], {})
        if not isinstance(content, dict) or not hmac.compare_digest(
            str(row["content_hash"]), _content_hash(content)
        ):
            raise ControlPlaneConflict("Workspace template content integrity check failed")
        return {**self._workspace_template_summary(row, content), "content": content}

    def workspace_template_library_refs(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT library.library_id, library.stable_id, library.version, library.content_hash,
                       library.status, library.scopes_json AS allowed_scopes_json,
                       grants.scopes_json AS granted_scopes_json
                FROM workspace_capability_grants AS grants
                INNER JOIN control_plane_library AS library ON library.library_id = grants.library_id
                WHERE grants.tenant_id = ? AND grants.owner_id = ? AND grants.worker_id = ?
                  AND grants.revoked_at IS NULL AND grants.library_id IS NOT NULL
                ORDER BY grants.created_at, grants.rowid
                """,
                (tenant_id, owner_id, worker_id),
            ).fetchall()
        refs: list[dict[str, Any]] = []
        for row in rows:
            if str(row["status"]) != "available":
                raise ControlPlaneError("Workspace Library capability is no longer available")
            allowed = {str(value) for value in _parse_json(row["allowed_scopes_json"], [])}
            granted = sorted({str(value) for value in _parse_json(row["granted_scopes_json"], [])})
            if not set(granted).issubset(allowed):
                raise ControlPlaneConflict("Workspace Library scopes no longer match the approved version")
            refs.append(
                {
                    "library_id": str(row["library_id"]),
                    "stable_id": str(row["stable_id"]),
                    "version": str(row["version"]),
                    "content_hash": str(row["content_hash"]),
                    "scopes": granted,
                }
            )
        return refs

    def validate_workspace_template_libraries(
        self,
        *,
        library_refs: list[dict[str, Any]],
        profile: str,
    ) -> list[dict[str, Any]]:
        pending = list(library_refs)
        approvals: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        with self._connect() as conn:
            while pending:
                requested = pending.pop(0)
                if not isinstance(requested, dict):
                    raise ControlPlaneError("Workspace template Library reference is invalid")
                library_id = str(requested.get("library_id") or "").strip()
                stable_id = str(requested.get("stable_id") or "").strip()
                version = str(requested.get("version") or "").strip()
                content_hash = str(requested.get("content_hash") or "").strip()
                if library_id:
                    row = conn.execute(
                        "SELECT * FROM control_plane_library WHERE library_id = ? AND status = 'available'",
                        (library_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM control_plane_library
                        WHERE stable_id = ? AND version = ? AND content_hash = ? AND status = 'available'
                        """,
                        (stable_id, version, content_hash),
                    ).fetchone()
                if row is None:
                    raise ControlPlaneError("Required Library version is no longer available")
                identity = (str(row["stable_id"]), str(row["version"]), str(row["content_hash"]))
                if identity != (stable_id, version, content_hash):
                    raise ControlPlaneConflict("Required Library version or content hash changed")
                if identity in seen:
                    continue
                supported = {str(value) for value in _parse_json(row["supported_profiles_json"], [])}
                if supported and profile not in supported:
                    raise ControlPlaneError("Required Library version is incompatible with this worker profile")
                allowed = {str(value) for value in _parse_json(row["scopes_json"], [])}
                scopes = sorted({str(value) for value in requested.get("scopes", []) if str(value)})
                if not set(scopes).issubset(allowed):
                    raise ControlPlaneConflict("Required Library scopes exceed the approved version")
                manifest = _parse_json(row["manifest_json"], {})
                if _library_activation(manifest) is None:
                    raise ControlPlaneError("Required Library version is not activatable")
                seen.add(identity)
                approvals.append(
                    {
                        "library_id": str(row["library_id"]),
                        "stable_id": identity[0],
                        "version": identity[1],
                        "content_hash": identity[2],
                        "scopes": scopes or sorted(allowed),
                        "approval_required": True,
                    }
                )
                dependencies = manifest.get("dependencies", []) if isinstance(manifest, dict) else []
                if not isinstance(dependencies, list):
                    raise ControlPlaneError("Library dependency metadata is invalid")
                pending.extend(dependencies)
        return approvals

    def reserve_workspace_template_instantiation(
        self,
        *,
        template_id: str,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        request_hash = _content_hash(request_payload)
        now = time.time()
        reserved_project_id = _id("prj")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM workspace_template_instantiations
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                """,
                (tenant_id, owner_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["request_hash"]), request_hash):
                    raise ControlPlaneConflict("Idempotency key was already used for a different template request")
                if str(existing["status"]) == "failed" and not existing["project_id"] and not existing["worker_id"]:
                    conn.execute(
                        """
                        UPDATE workspace_template_instantiations
                        SET status = 'pending', project_id = ?, worker_id = NULL, created_at = ?, completed_at = NULL
                        WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ? AND status = 'failed'
                        """,
                        (reserved_project_id, now, tenant_id, owner_id, idempotency_key),
                    )
                    return {
                        "status": "pending",
                        "project_id": reserved_project_id,
                        "idempotent_replay": False,
                        "safe_retry": True,
                    }
                if str(existing["status"]) != "completed":
                    raise ControlPlaneConflict("Template instantiation with this key is already in progress")
                return {**dict(existing), "idempotent_replay": True}
            conn.execute(
                """
                INSERT INTO workspace_template_instantiations
                    (tenant_id, owner_id, idempotency_key, template_id, request_hash, status, project_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (tenant_id, owner_id, idempotency_key, template_id, request_hash, reserved_project_id, now),
            )
        return {
            "status": "pending",
            "project_id": reserved_project_id,
            "idempotent_replay": False,
            "safe_retry": False,
        }

    def complete_workspace_template_instantiation(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        project_id: str,
        worker_id: str,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspace_template_instantiations
                SET status = 'completed', project_id = ?, worker_id = ?, completed_at = ?
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (project_id, worker_id, time.time(), tenant_id, owner_id, idempotency_key),
            )
        if cursor.rowcount != 1:
            raise ControlPlaneConflict("Template instantiation reservation was lost")

    def fail_workspace_template_instantiation(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        project_id: str | None,
        worker_id: str | None,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspace_template_instantiations
                SET status = 'failed', project_id = ?, worker_id = ?, completed_at = NULL
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (project_id, worker_id, tenant_id, owner_id, idempotency_key),
            )
        if cursor.rowcount != 1:
            raise ControlPlaneConflict("Template instantiation reservation was lost")

    def complete_workspace_template_cleanup(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        project_id: str,
        worker_id: str | None,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspace_template_instantiations
                SET project_id = NULL, worker_id = NULL
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                  AND status = 'failed' AND project_id = ?
                  AND COALESCE(worker_id, '') = ?
                """,
                (tenant_id, owner_id, idempotency_key, project_id, worker_id or ""),
            )
        if cursor.rowcount != 1:
            raise ControlPlaneConflict("Template instantiation cleanup state changed unexpectedly")

    def reserve_workspace_duplication(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        source_worker_id: str,
        requested_name: str,
    ) -> dict[str, Any]:
        request_hash = _content_hash(
            {
                "source_worker_id": source_worker_id,
                "name": requested_name,
            }
        )
        now = time.time()
        reserved_project_id = _id("prj")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM workspace_duplications
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                """,
                (tenant_id, owner_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["request_hash"]), request_hash):
                    raise ControlPlaneConflict(
                        "Idempotency key was already used for a different workspace duplicate request"
                    )
                status = str(existing["status"])
                if status == "pending":
                    return {**dict(existing), "in_progress": True, "idempotent_replay": True}
                if status == "completed":
                    response = _parse_json(existing["response_json"], {})
                    if not isinstance(response, dict) or not response:
                        raise ControlPlaneConflict("Completed workspace duplication record is inconsistent")
                    return {
                        **dict(existing),
                        "response": response,
                        "idempotent_replay": True,
                    }
                if status == "failed" and not existing["project_id"] and not existing["worker_id"]:
                    conn.execute(
                        """
                        UPDATE workspace_duplications
                        SET status = 'pending', project_id = ?, error_text = '', failed_at = NULL, updated_at = ?
                        WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ? AND status = 'failed'
                        """,
                        (reserved_project_id, now, tenant_id, owner_id, idempotency_key),
                    )
                    return {
                        "status": "pending",
                        "project_id": reserved_project_id,
                        "idempotent_replay": False,
                        "safe_retry": True,
                    }
                if status == "failed":
                    return {**dict(existing), "failed_replay": True, "idempotent_replay": True}
                raise ControlPlaneConflict("Workspace duplication reservation is inconsistent")
            conn.execute(
                """
                INSERT INTO workspace_duplications
                    (tenant_id, owner_id, idempotency_key, source_worker_id, requested_name,
                     request_hash, status, project_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    tenant_id,
                    owner_id,
                    idempotency_key,
                    source_worker_id,
                    requested_name,
                    request_hash,
                    reserved_project_id,
                    now,
                    now,
                ),
            )
        return {
            "status": "pending",
            "project_id": reserved_project_id,
            "idempotent_replay": False,
            "safe_retry": False,
        }

    def record_workspace_duplication_project(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        project_id: str,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspace_duplications
                SET project_id = ?, updated_at = ?
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (project_id, time.time(), tenant_id, owner_id, idempotency_key),
            )
        if cursor.rowcount != 1:
            raise ControlPlaneConflict("Workspace duplication reservation was lost")

    def complete_workspace_duplication(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        project_id: str,
        worker_id: str,
        response: dict[str, Any],
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspace_duplications
                SET status = 'completed', project_id = ?, worker_id = ?, response_json = ?,
                    error_text = '', updated_at = ?, completed_at = ?, failed_at = NULL
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (
                    project_id,
                    worker_id,
                    _json(response),
                    now,
                    now,
                    tenant_id,
                    owner_id,
                    idempotency_key,
                ),
            )
        if cursor.rowcount != 1:
            raise ControlPlaneConflict("Workspace duplication reservation was lost")

    def fail_workspace_duplication(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        error_text: str,
        project_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspace_duplications
                SET status = 'failed',
                    project_id = COALESCE(?, project_id),
                    worker_id = COALESCE(?, worker_id),
                    error_text = ?, updated_at = ?, failed_at = ?
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (
                    project_id,
                    worker_id,
                    str(error_text or "Workspace duplication failed")[:2000],
                    now,
                    now,
                    tenant_id,
                    owner_id,
                    idempotency_key,
                ),
            )
        if cursor.rowcount != 1:
            raise ControlPlaneConflict("Workspace duplication reservation was lost")

    def _library_install_plan(
        self,
        conn: sqlite3.Connection,
        *,
        library_id: str,
        profile: str,
    ) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        visiting: set[tuple[str, str, str]] = set()
        visited: set[tuple[str, str, str]] = set()
        planned_scopes: dict[tuple[str, str, str], set[str]] = {}

        def visit(row: sqlite3.Row, requested_scopes: list[str] | None = None) -> None:
            manifest = _parse_json(row["manifest_json"], {})
            try:
                normalized = validate_library_manifest(manifest)
            except LibraryManifestError as exc:
                raise ControlPlaneConflict(f"Library manifest changed since review or is invalid: {exc}") from exc
            if not hmac.compare_digest(str(row["content_hash"]), normalized["content_hash"]):
                raise ControlPlaneConflict("Library content integrity check failed")
            identity = (
                normalized["stable_id"],
                normalized["version"],
                normalized["content_hash"],
            )
            if identity in visiting:
                raise ControlPlaneConflict("Library dependency cycle detected")
            if identity in visited:
                repeated_scopes = set(
                    normalized["requested_scopes"] if requested_scopes is None else requested_scopes
                )
                if repeated_scopes != planned_scopes[identity]:
                    raise ControlPlaneConflict(
                        "Library dependency graph requests inconsistent scopes for the same pinned version"
                    )
                return
            if profile not in normalized["supported_profiles"]:
                raise ControlPlaneError("Library item is not compatible with this workspace profile")
            visiting.add(identity)
            for dependency in normalized["dependencies"]:
                dependency_row = conn.execute(
                    """
                    SELECT * FROM control_plane_library
                    WHERE stable_id = ? AND version = ? AND content_hash = ? AND status = 'available'
                    """,
                    (
                        dependency["stable_id"],
                        dependency["version"],
                        dependency["content_hash"],
                    ),
                ).fetchone()
                if dependency_row is None:
                    raise ControlPlaneError(
                        f"Required Library dependency {dependency['stable_id']} {dependency['version']} is unavailable"
                    )
                visit(dependency_row, list(dependency["scopes"]))
            visiting.remove(identity)
            visited.add(identity)
            allowed = set(normalized["requested_scopes"])
            scopes = sorted(
                set(normalized["requested_scopes"] if requested_scopes is None else requested_scopes)
            )
            if not set(scopes).issubset(allowed):
                raise ControlPlaneConflict("Library dependency scopes exceed its published manifest")
            planned_scopes[identity] = set(scopes)
            plan.append(
                {
                    "row": row,
                    "manifest": normalized,
                    "scopes": scopes,
                    "snapshot": _library_confirmation_snapshot(row),
                }
            )

        root = conn.execute(
            "SELECT * FROM control_plane_library WHERE library_id = ? AND status = 'available'",
            (library_id,),
        ).fetchone()
        if root is None:
            raise ControlPlaneError("Library item is not available")
        visit(root)
        return plan

    def create_pending_change(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        change_type: str,
        target_id: str,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> dict[str, Any]:
        change_id = _id("chg")
        confirmation_token = secrets.token_urlsafe(32)
        now = time.time()
        pending_payload = dict(payload)
        with self._connect() as conn:
            # The target-state check and pending-change reservation are one
            # linearized decision. Close may win before this transaction (and
            # the preparation is rejected), or after it (and confirmation is
            # rejected), but it cannot slip between the check and INSERT.
            conn.execute("BEGIN IMMEDIATE")
            if str(change_type).strip() in {"workspace_grant", "library_enable", "library_upgrade"}:
                library_id = str(pending_payload.get("library_id") or "").strip()
                if library_id:
                    worker = conn.execute(
                        """
                        SELECT profile, duplication_report_json FROM workers
                        WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                        """,
                        (str(target_id), tenant_id, owner_id),
                    ).fetchone()
                    if worker is None:
                        raise ControlPlaneError("Target workspace is no longer available for this user")
                    plan = self._library_install_plan(
                        conn,
                        library_id=library_id,
                        profile=str(worker["profile"] or ""),
                    )
                    library = plan[-1]["row"]
                    if str(change_type).strip() != "library_upgrade":
                        existing_stable_grant = conn.execute(
                            """
                            SELECT grants.grant_id, grants.library_id
                            FROM workspace_capability_grants AS grants
                            INNER JOIN control_plane_library AS installed
                                ON installed.library_id = grants.library_id
                            WHERE grants.tenant_id = ? AND grants.owner_id = ? AND grants.worker_id = ?
                              AND grants.revoked_at IS NULL AND installed.stable_id = ?
                            ORDER BY grants.created_at DESC, grants.rowid DESC LIMIT 1
                            """,
                            (tenant_id, owner_id, str(target_id), str(library["stable_id"])),
                        ).fetchone()
                        if existing_stable_grant is not None:
                            if str(existing_stable_grant["library_id"]) == library_id:
                                raise ControlPlaneConflict("Library item is already enabled for this workspace")
                            raise ControlPlaneConflict(
                                "This workspace already has another version; prepare a Library upgrade"
                            )
                    snapshot = plan[-1]["snapshot"]
                    supported_profiles = set(snapshot["supported_profiles"])
                    if supported_profiles and str(worker["profile"] or "") not in supported_profiles:
                        raise ControlPlaneError("Library item is not compatible with this workspace profile")
                    requested_scopes = {
                        str(value).strip()
                        for value in (pending_payload.get("scopes") or [])
                        if str(value).strip()
                    }
                    allowed_scopes = set(snapshot["allowed_scopes"])
                    report = _parse_json(worker["duplication_report_json"], {})
                    waived = {
                        str(value).strip()
                        for value in ((report or {}).get("waived_reapprovals") or [])
                        if str(value).strip()
                    } if isinstance(report, dict) else set()
                    copied_review = next(
                        (
                            entry
                            for entry in ((report or {}).get("reapproval_items") or [])
                            if isinstance(entry, dict)
                            and str(entry.get("resolution") or "") == "library_grant"
                            and str(entry.get("reference") or "") == library_id
                            and str(entry.get("action_id") or "") not in waived
                        ),
                        None,
                    ) if isinstance(report, dict) else None
                    if copied_review is not None:
                        copied_scopes = {
                            str(value).strip()
                            for value in (copied_review.get("scopes") or [])
                            if str(value).strip()
                        }
                        if not copied_scopes.issubset(allowed_scopes):
                            raise ControlPlaneConflict(
                                "The copied capability changed; review its current permissions"
                            )
                        if requested_scopes and requested_scopes != copied_scopes:
                            raise ControlPlaneConflict(
                                "Copied capability review must keep its exact permissions"
                            )
                        requested_scopes = copied_scopes
                    if requested_scopes and not requested_scopes.issubset(allowed_scopes):
                        raise ControlPlaneError("Requested workspace scopes exceed the approved capability scopes")
                    pending_payload["scopes"] = sorted(requested_scopes or allowed_scopes)
                    pending_payload["library_snapshot"] = snapshot
                    pending_payload["library_plan_snapshot"] = [
                        {"library_snapshot": item["snapshot"], "scopes": item["scopes"]}
                        for item in plan
                    ]
                    pending_payload["library_plan_snapshot"][-1]["scopes"] = pending_payload["scopes"]
                    if str(change_type).strip() == "library_upgrade":
                        replaces_grant_id = str(pending_payload.get("replaces_grant_id") or "").strip()
                        if not replaces_grant_id:
                            raise ControlPlaneError("Library upgrade requires the current workspace grant")
                        current_grant = conn.execute(
                            """
                            SELECT grants.*, library.stable_id, library.version
                            FROM workspace_capability_grants AS grants
                            INNER JOIN control_plane_library AS library ON library.library_id = grants.library_id
                            WHERE grants.grant_id = ? AND grants.tenant_id = ? AND grants.owner_id = ?
                              AND grants.worker_id = ? AND grants.revoked_at IS NULL
                            """,
                            (replaces_grant_id, tenant_id, owner_id, str(target_id)),
                        ).fetchone()
                        if current_grant is None:
                            raise ControlPlaneError("Current Library workspace grant is unavailable")
                        if str(current_grant["stable_id"]) != str(library["stable_id"]):
                            raise ControlPlaneConflict("Library upgrades must retain the same stable_id")
                        if compare_semantic_versions(
                            str(library["version"]),
                            str(current_grant["version"]),
                        ) <= 0:
                            raise ControlPlaneConflict("Library upgrade must select a newer version")
                        latest = conn.execute(
                            """
                            SELECT grant_id FROM workspace_capability_grants
                            WHERE tenant_id = ? AND owner_id = ? AND worker_id = ? AND revoked_at IS NULL
                            ORDER BY created_at DESC, rowid DESC LIMIT 1
                            """,
                            (tenant_id, owner_id, str(target_id)),
                        ).fetchone()
                        if latest is None or str(latest["grant_id"]) != replaces_grant_id:
                            raise ControlPlaneConflict("Upgrade the newest workspace capability first")
                        existing_scopes = set(_parse_json(current_grant["scopes_json"], []))
                        if not set(pending_payload["scopes"]).issubset(existing_scopes):
                            raise ControlPlaneConflict("A Library upgrade cannot widen workspace scopes")
                        new_scope_union: set[str] = set()
                        for item in pending_payload["library_plan_snapshot"]:
                            if isinstance(item, dict):
                                new_scope_union.update(
                                    str(scope) for scope in item.get("scopes", []) if str(scope)
                                )
                        if not new_scope_union.issubset(_grant_scope_union(current_grant)):
                            raise ControlPlaneConflict(
                                "A Library upgrade cannot widen dependency or workspace scopes"
                            )
                        pending_payload["replaces_grant_id"] = replaces_grant_id
                        pending_payload["replaces_library_version"] = str(current_grant["version"])
            elif str(change_type).strip() == "workspace_provider_account":
                worker = conn.execute(
                    """
                    SELECT worker_id, profile, state FROM workers
                    WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                    """,
                    (str(target_id), tenant_id, owner_id),
                ).fetchone()
                if worker is None:
                    raise ControlPlaneError("Target workspace is no longer available for this user")
                if str(worker["state"] or "") in {
                    "terminating",
                    "termination_failed",
                    "terminated",
                }:
                    raise ControlPlaneConflict(
                        "Workspace is closed; create a new workspace for new work"
                    )
                policy = str(pending_payload.get("policy") or "").strip().lower()
                account_id = str(pending_payload.get("account_id") or "").strip()
                if policy not in WORKSPACE_ACCOUNT_POLICIES:
                    raise ControlPlaneError("Workspace provider account policy is invalid")
                if policy == "legacy":
                    if account_id:
                        raise ControlPlaneError("Deployment account policy cannot include a personal account")
                    pending_payload = {"policy": "legacy"}
                else:
                    if not account_id:
                        raise ControlPlaneError("A personal provider account must be selected")
                    account = conn.execute(
                        """
                        SELECT account_id, provider, label, status, updated_at
                        FROM provider_accounts
                        WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                        """,
                        (account_id, tenant_id, owner_id),
                    ).fetchone()
                    if account is None:
                        raise ControlPlaneError("Provider account not found for this user")
                    supported = PROFILE_ACCOUNT_PROVIDERS.get(str(worker["profile"] or ""), set())
                    if str(account["provider"] or "").strip().lower() not in supported:
                        raise ControlPlaneError("Provider account does not match this workspace profile")
                    if str(account["status"] or "").strip().lower() != "ready":
                        raise ControlPlaneConflict("Provider account must be reconnected before selection")
                    pending_payload = {
                        "policy": policy,
                        "account_id": account_id,
                        "account_snapshot": {
                            "account_id": account_id,
                            "provider": str(account["provider"] or ""),
                            "label": str(account["label"] or ""),
                            "status": str(account["status"] or ""),
                            "updated_at": float(account["updated_at"] or 0),
                        },
                    }
            elif str(change_type).strip() == "workspace_duplication_reapproval_waiver":
                worker = conn.execute(
                    """
                    SELECT worker_id, state, duplication_report_json FROM workers
                    WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                    """,
                    (str(target_id), tenant_id, owner_id),
                ).fetchone()
                if worker is None:
                    raise ControlPlaneError("Target workspace is no longer available for this user")
                if str(worker["state"] or "") in {
                    "terminating",
                    "termination_failed",
                    "terminated",
                }:
                    raise ControlPlaneConflict(
                        "Workspace is closed; create a new workspace for new work"
                    )
                report = _parse_json(worker["duplication_report_json"], {})
                if not isinstance(report, dict):
                    raise ControlPlaneError("Workspace capability review is invalid")
                action_id = str(pending_payload.get("action_id") or "").strip()
                item = next(
                    (
                        entry
                        for entry in (report.get("reapproval_items") or [])
                        if isinstance(entry, dict)
                        and str(entry.get("action_id") or "") == action_id
                    ),
                    None,
                )
                if item is None:
                    raise ControlPlaneError("Workspace capability review item is unavailable")
                if str(item.get("resolution") or "") == "provider_selection":
                    raise ControlPlaneConflict(
                        "Choose a reviewed worker account instead of bypassing its selection"
                    )
                waived = {
                    str(value)
                    for value in (report.get("waived_reapprovals") or [])
                    if str(value)
                }
                if action_id in waived:
                    raise ControlPlaneConflict("Workspace capability review item was already skipped")
                pending_payload = {
                    "action_id": action_id,
                    "review_snapshot": item,
                    "label": str(item.get("label") or "Copied capability")[:160],
                }
            conn.execute(
                """
                INSERT INTO control_plane_pending_changes
                    (change_id, tenant_id, owner_id, change_type, target_id, payload_json,
                     confirmation_hash, status, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    change_id,
                    tenant_id,
                    owner_id,
                    str(change_type).strip()[:120],
                    str(target_id).strip()[:200],
                    _json(pending_payload),
                    _secret_hash(confirmation_token),
                    now + max(60, min(int(ttl_seconds), 30 * 60)),
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM control_plane_pending_changes WHERE change_id = ?", (change_id,)).fetchone()
        result = self._row(row) or {}
        result["confirmation_token"] = confirmation_token
        return result

    def get_pending_change(
        self,
        *,
        change_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        """Return browser-safe confirmation metadata for the authenticated owner."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM control_plane_pending_changes
                WHERE change_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (change_id, tenant_id, owner_id),
            ).fetchone()
        if row is None:
            raise ControlPlaneError("Pending change not found for this user")
        return self._row(row) or {}

    def confirm_pending_change(
        self,
        *,
        change_id: str,
        tenant_id: str,
        owner_id: str,
        confirmation_token: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM control_plane_pending_changes
                WHERE change_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (change_id, tenant_id, owner_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneError("Pending change not found for this user")
            if str(row["status"]) != "pending":
                raise ControlPlaneConflict("Pending change is already resolved")
            if float(row["expires_at"]) <= now:
                conn.execute(
                    "UPDATE control_plane_pending_changes SET status = 'expired', resolved_at = ? WHERE change_id = ?",
                    (now, change_id),
                )
                raise ControlPlaneError("Pending change confirmation expired")
            if not confirmation_token or not hmac.compare_digest(
                str(row["confirmation_hash"]),
                _secret_hash(confirmation_token),
            ):
                raise ControlPlaneError("Pending change confirmation is invalid")
            change_type = str(row["change_type"] or "")
            payload = _parse_json(row["payload_json"], {})
            if not isinstance(payload, dict):
                raise ControlPlaneError("Pending change payload is invalid")
            applied: dict[str, Any] | None = None
            if change_type in {"workspace_grant", "library_enable", "library_upgrade"}:
                library_id = str(payload.get("library_id") or "").strip() or None
                connection_id = str(payload.get("connection_id") or "").strip() or None
                account_id = str(payload.get("account_id") or "").strip() or None
                if sum(value is not None for value in (library_id, connection_id, account_id)) != 1:
                    raise ControlPlaneError("A workspace change must name exactly one capability")
                worker = conn.execute(
                    """
                    SELECT worker_id, profile, bootstrap_bundle_json FROM workers
                    WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                    """,
                    (str(row["target_id"]), tenant_id, owner_id),
                ).fetchone()
                if worker is None:
                    raise ControlPlaneError("Target workspace is no longer available for this user")
                allowed_scopes: set[str] = set()
                plan: list[dict[str, Any]] = []
                if library_id:
                    plan = self._library_install_plan(
                        conn,
                        library_id=library_id,
                        profile=str(worker["profile"] or ""),
                    )
                    library = plan[-1]["row"]
                    reviewed_snapshot = payload.get("library_snapshot")
                    current_snapshot = plan[-1]["snapshot"]
                    if not isinstance(reviewed_snapshot, dict) or not hmac.compare_digest(
                        _json(reviewed_snapshot),
                        _json(current_snapshot),
                    ):
                        raise ControlPlaneConflict(
                            "Library item changed since review; prepare the change again"
                        )
                    reviewed_plan = payload.get("library_plan_snapshot")
                    current_plan = [
                        {"library_snapshot": item["snapshot"], "scopes": item["scopes"]}
                        for item in plan
                    ]
                    current_plan[-1]["scopes"] = sorted(
                        {
                            str(value).strip()
                            for value in (payload.get("scopes") or [])
                            if str(value).strip()
                        }
                        or set(current_snapshot["allowed_scopes"])
                    )
                    if not isinstance(reviewed_plan, list) or not hmac.compare_digest(
                        _json(reviewed_plan),
                        _json(current_plan),
                    ):
                        raise ControlPlaneConflict(
                            "Library dependency plan changed since review; prepare the change again"
                        )
                    if change_type != "library_upgrade":
                        active_same_stable = conn.execute(
                            """
                            SELECT grants.grant_id FROM workspace_capability_grants AS grants
                            INNER JOIN control_plane_library AS installed
                                ON installed.library_id = grants.library_id
                            WHERE grants.tenant_id = ? AND grants.owner_id = ? AND grants.worker_id = ?
                              AND grants.revoked_at IS NULL AND installed.stable_id = ?
                            LIMIT 1
                            """,
                            (tenant_id, owner_id, str(row["target_id"]), str(library["stable_id"])),
                        ).fetchone()
                        if active_same_stable is not None:
                            raise ControlPlaneConflict(
                                "Workspace Library version changed since review; prepare an upgrade"
                            )
                    allowed_scopes.update(str(value) for value in _parse_json(library["scopes_json"], []))
                    supported_profiles = {
                        str(value) for value in _parse_json(library["supported_profiles_json"], [])
                    }
                    if supported_profiles and str(worker["profile"] or "") not in supported_profiles:
                        raise ControlPlaneError("Library item is not compatible with this workspace profile")
                if connection_id:
                    raise ControlPlaneError(
                        "Connected services must be activated by their brokered workspace bundle"
                    )
                if account_id:
                    raise ControlPlaneError(
                        "Provider accounts must be selected through the workspace execution policy"
                    )
                requested_scopes = {
                    str(value).strip()
                    for value in (payload.get("scopes") or [])
                    if str(value).strip()
                }
                if requested_scopes and not requested_scopes.issubset(allowed_scopes):
                    raise ControlPlaneError("Requested workspace scopes exceed the approved capability scopes")
                scopes = sorted(requested_scopes or allowed_scopes)
                current_bundle = _parse_json(worker["bootstrap_bundle_json"], {})
                if not isinstance(current_bundle, dict):
                    current_bundle = {}
                prior_bundle = current_bundle
                replaced_grant: sqlite3.Row | None = None
                if change_type == "library_upgrade":
                    replaces_grant_id = str(payload.get("replaces_grant_id") or "").strip()
                    replaced_grant = conn.execute(
                        """
                        SELECT grants.*, library.stable_id
                        FROM workspace_capability_grants AS grants
                        INNER JOIN control_plane_library AS library ON library.library_id = grants.library_id
                        WHERE grants.grant_id = ? AND grants.tenant_id = ? AND grants.owner_id = ?
                          AND grants.worker_id = ? AND grants.revoked_at IS NULL
                        """,
                        (replaces_grant_id, tenant_id, owner_id, str(row["target_id"])),
                    ).fetchone()
                    if replaced_grant is None:
                        raise ControlPlaneConflict("Current Library grant changed since review")
                    if str(replaced_grant["stable_id"]) != str(library["stable_id"]):
                        raise ControlPlaneConflict("Library upgrade stable_id changed since review")
                    latest = conn.execute(
                        """
                        SELECT grant_id FROM workspace_capability_grants
                        WHERE tenant_id = ? AND owner_id = ? AND worker_id = ? AND revoked_at IS NULL
                        ORDER BY created_at DESC, rowid DESC LIMIT 1
                        """,
                        (tenant_id, owner_id, str(row["target_id"])),
                    ).fetchone()
                    if latest is None or str(latest["grant_id"]) != replaces_grant_id:
                        raise ControlPlaneConflict("Current Library grant is no longer the newest capability")
                    old_applied = _parse_json(replaced_grant["applied_bootstrap_bundle_json"], {})
                    if current_bundle != old_applied:
                        raise ControlPlaneConflict(
                            "Workspace configuration changed after the current Library version; upgrade stopped"
                        )
                    prior_bundle = _parse_json(replaced_grant["prior_bootstrap_bundle_json"], {})
                    if not isinstance(prior_bundle, dict):
                        raise ControlPlaneError("Current Library rollback state is invalid")
                    existing_scopes = set(_parse_json(replaced_grant["scopes_json"], []))
                    if not set(scopes).issubset(existing_scopes):
                        raise ControlPlaneConflict("A Library upgrade cannot widen workspace scopes")
                    confirmed_scope_union: set[str] = set()
                    for item in current_plan:
                        confirmed_scope_union.update(
                            str(scope) for scope in item.get("scopes", []) if str(scope)
                        )
                    if not confirmed_scope_union.issubset(_grant_scope_union(replaced_grant)):
                        raise ControlPlaneConflict(
                            "A Library upgrade cannot widen dependency or workspace scopes"
                        )
                    current_bundle = prior_bundle
                merged_bundle = current_bundle
                probe_results: list[dict[str, Any]] = []
                installed_plan: list[dict[str, Any]] = []
                for item in plan:
                    manifest = item["manifest"]
                    try:
                        activation_bundle = activation_bundle_for_profile(
                            manifest,
                            str(worker["profile"] or ""),
                        )
                        merged_bundle = _merge_bootstrap(merged_bundle, activation_bundle)
                        probe_result = probe_activation(
                            manifest,
                            profile=str(worker["profile"] or ""),
                            merged_bundle=merged_bundle,
                        )
                    except LibraryManifestError as exc:
                        raise ControlPlaneError(str(exc)) from exc
                    probe_results.append(
                        {
                            "library_id": str(item["row"]["library_id"]),
                            **probe_result,
                        }
                    )
                    installed_plan.append(
                        {
                            "library_id": str(item["row"]["library_id"]),
                            "stable_id": str(item["row"]["stable_id"]),
                            "version": str(item["row"]["version"]),
                            "content_hash": str(item["row"]["content_hash"]),
                            "scopes": (
                                scopes if str(item["row"]["library_id"]) == library_id else item["scopes"]
                            ),
                        }
                    )
                conn.execute(
                    "UPDATE workers SET bootstrap_bundle_json = ?, updated_at = ? WHERE worker_id = ?",
                    (_json(merged_bundle), _worker_updated_at(), str(row["target_id"])),
                )
                grant_id = _id("grant")
                if replaced_grant is not None:
                    conn.execute(
                        "UPDATE workspace_capability_grants SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
                        (now, str(replaced_grant["grant_id"])),
                    )
                conn.execute(
                    """
                    INSERT INTO workspace_capability_grants
                        (grant_id, tenant_id, owner_id, worker_id, library_id, connection_id,
                         account_id, scopes_json, prior_bootstrap_bundle_json,
                         applied_bootstrap_bundle_json, installation_plan_json, probe_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        grant_id,
                        tenant_id,
                        owner_id,
                        str(row["target_id"]),
                        library_id,
                        connection_id,
                        account_id,
                        _json(scopes),
                        _json(prior_bundle),
                        _json(merged_bundle),
                        _json(installed_plan),
                        _json({"status": "healthy", "results": probe_results}),
                        now,
                    ),
                )
                grant = conn.execute(
                    "SELECT * FROM workspace_capability_grants WHERE grant_id = ?",
                    (grant_id,),
                ).fetchone()
                applied = self._row(grant)
                if applied is not None:
                    applied["upgrade"] = replaced_grant is not None
                    if replaced_grant is not None:
                        applied["replaced_grant_id"] = str(replaced_grant["grant_id"])
            elif change_type == "connection_write":
                raise ControlPlaneError(
                    "Connection writes must use the connected service's brokered confirmation flow"
                )
            elif change_type == "workspace_provider_account":
                worker = conn.execute(
                    """
                    SELECT worker_id, profile, state, bootstrap_bundle_json FROM workers
                    WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                    """,
                    (str(row["target_id"]), tenant_id, owner_id),
                ).fetchone()
                if worker is None:
                    raise ControlPlaneError("Target workspace is no longer available for this user")
                worker_state = str(worker["state"] or "").strip().lower()
                if worker_state in ACCOUNT_SWITCH_BLOCKED_WORKER_STATES:
                    if worker_state in {"terminating", "termination_failed", "terminated"}:
                        raise ControlPlaneConflict(
                            "Workspace is closed; create a new workspace for new work"
                        )
                    raise ControlPlaneConflict(
                        "Pause the workspace and wait for active work to finish before switching accounts"
                    )
                active_run = conn.execute(
                    """
                    SELECT run_id FROM runs
                    WHERE worker_id = ? AND state IN ('queued', 'running')
                    LIMIT 1
                    """,
                    (str(row["target_id"]),),
                ).fetchone()
                active_lease = conn.execute(
                    """
                    SELECT lease_id FROM provider_account_leases
                    WHERE worker_id = ? AND released_at IS NULL AND expires_at > ?
                    LIMIT 1
                    """,
                    (str(row["target_id"]), now),
                ).fetchone()
                if active_run is not None or active_lease is not None:
                    raise ControlPlaneConflict(
                        "Wait for queued or running work to finish before switching accounts"
                    )
                policy = str(payload.get("policy") or "").strip().lower()
                account_id = str(payload.get("account_id") or "").strip()
                if policy not in WORKSPACE_ACCOUNT_POLICIES:
                    raise ControlPlaneError("Workspace provider account policy is invalid")
                selection: dict[str, str] = {"policy": policy}
                applied_account: dict[str, Any] | None = None
                if policy == "legacy":
                    if account_id:
                        raise ControlPlaneError("Deployment account policy cannot include a personal account")
                else:
                    if not account_id:
                        raise ControlPlaneError("A personal provider account must be selected")
                    account = conn.execute(
                        """
                        SELECT account_id, provider, label, status, updated_at
                        FROM provider_accounts
                        WHERE account_id = ? AND tenant_id = ? AND owner_id = ?
                        """,
                        (account_id, tenant_id, owner_id),
                    ).fetchone()
                    if account is None:
                        raise ControlPlaneError("Provider account not found for this user")
                    supported = PROFILE_ACCOUNT_PROVIDERS.get(str(worker["profile"] or ""), set())
                    if str(account["provider"] or "").strip().lower() not in supported:
                        raise ControlPlaneError("Provider account does not match this workspace profile")
                    if str(account["status"] or "").strip().lower() != "ready":
                        raise ControlPlaneConflict("Provider account must be reconnected before selection")
                    account_lease = conn.execute(
                        """
                        SELECT lease_id FROM provider_account_leases
                        WHERE account_id = ? AND released_at IS NULL AND expires_at > ?
                        LIMIT 1
                        """,
                        (account_id, now),
                    ).fetchone()
                    if account_lease is not None:
                        raise ControlPlaneConflict(
                            "Wait for the selected provider account's active mission to finish"
                        )
                    reviewed_snapshot = payload.get("account_snapshot")
                    current_snapshot = {
                        "account_id": str(account["account_id"]),
                        "provider": str(account["provider"] or ""),
                        "label": str(account["label"] or ""),
                        "status": str(account["status"] or ""),
                        "updated_at": float(account["updated_at"] or 0),
                    }
                    if not isinstance(reviewed_snapshot, dict) or not hmac.compare_digest(
                        _json(reviewed_snapshot), _json(current_snapshot)
                    ):
                        raise ControlPlaneConflict(
                            "Provider account changed since review; prepare the switch again"
                        )
                    selection["account_id"] = account_id
                    applied_account = {
                        "account_id": account_id,
                        "provider": str(account["provider"] or ""),
                        "label": str(account["label"] or ""),
                    }
                current_bundle = _parse_json(worker["bootstrap_bundle_json"], {})
                if not isinstance(current_bundle, dict):
                    current_bundle = {}
                next_bundle = dict(current_bundle)
                next_bundle["provider_account"] = selection
                conn.execute(
                    "UPDATE workers SET bootstrap_bundle_json = ?, updated_at = ? WHERE worker_id = ?",
                    (_json(next_bundle), _worker_updated_at(), str(row["target_id"])),
                )
                applied = {
                    "worker_id": str(row["target_id"]),
                    "provider_account": selection,
                    "account": applied_account,
                    "applies_to": "future_runs",
                }
            elif change_type == "workspace_duplication_reapproval_waiver":
                worker = conn.execute(
                    """
                    SELECT worker_id, duplication_report_json FROM workers
                    WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                    """,
                    (str(row["target_id"]), tenant_id, owner_id),
                ).fetchone()
                if worker is None:
                    raise ControlPlaneError("Target workspace is no longer available for this user")
                report = _parse_json(worker["duplication_report_json"], {})
                if not isinstance(report, dict):
                    raise ControlPlaneError("Workspace capability review is invalid")
                action_id = str(payload.get("action_id") or "").strip()
                item = next(
                    (
                        entry
                        for entry in (report.get("reapproval_items") or [])
                        if isinstance(entry, dict)
                        and str(entry.get("action_id") or "") == action_id
                    ),
                    None,
                )
                reviewed = payload.get("review_snapshot")
                if item is None or not isinstance(reviewed, dict) or not hmac.compare_digest(
                    _json(item), _json(reviewed)
                ):
                    raise ControlPlaneConflict(
                        "Workspace capability review changed; prepare the decision again"
                    )
                if str(item.get("resolution") or "") == "provider_selection":
                    raise ControlPlaneConflict(
                        "Choose a reviewed worker account instead of bypassing its selection"
                    )
                waived = {
                    str(value)
                    for value in (report.get("waived_reapprovals") or [])
                    if str(value)
                }
                if action_id in waived:
                    raise ControlPlaneConflict(
                        "Workspace capability review item was already resolved"
                    )
                waived.add(action_id)
                report["waived_reapprovals"] = sorted(waived)
                conn.execute(
                    "UPDATE workers SET duplication_report_json = ?, updated_at = ? WHERE worker_id = ?",
                    (_json(report), _worker_updated_at(), str(row["target_id"])),
                )
                applied = {
                    "worker_id": str(row["target_id"]),
                    "action_id": action_id,
                    "applies_to": "future_runs",
                }
            else:
                raise ControlPlaneError("Unsupported pending change type")
            conn.execute(
                "UPDATE control_plane_pending_changes SET status = 'confirmed', resolved_at = ? WHERE change_id = ?",
                (now, change_id),
            )
            confirmed = conn.execute(
                "SELECT * FROM control_plane_pending_changes WHERE change_id = ?",
                (change_id,),
            ).fetchone()
        result = self._row(confirmed) or {}
        if applied is not None:
            result["applied"] = applied
        return result

    def list_workspace_grants(self, *, tenant_id: str, owner_id: str, worker_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workspace_capability_grants
                WHERE tenant_id = ? AND owner_id = ? AND worker_id = ? AND revoked_at IS NULL
                ORDER BY created_at, rowid
                """,
                (tenant_id, owner_id, worker_id),
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def workspace_capability_readiness(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return bounded, owner-scoped capability readiness in one query for catalog cards."""
        normalized_ids = list(
            dict.fromkeys(str(worker_id or "").strip() for worker_id in worker_ids if str(worker_id or "").strip())
        )[:200]
        result = {
            worker_id: {
                "active_grants": 0,
                "unavailable_grants": 0,
                "readiness": "ready",
            }
            for worker_id in normalized_ids
        }
        if not normalized_ids:
            return result
        placeholders = ",".join("?" for _ in normalized_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT grants.worker_id,
                       COUNT(*) AS active_grants,
                       SUM(
                           CASE
                               WHEN grants.library_id IS NOT NULL AND COALESCE(library.status, '') != 'available' THEN 1
                               WHEN grants.connection_id IS NOT NULL AND COALESCE(connections.status, '') != 'ready' THEN 1
                               WHEN grants.account_id IS NOT NULL AND COALESCE(accounts.status, '') != 'ready' THEN 1
                               ELSE 0
                           END
                       ) AS unavailable_grants
                FROM workspace_capability_grants AS grants
                LEFT JOIN control_plane_library AS library ON library.library_id = grants.library_id
                LEFT JOIN control_plane_connections AS connections ON connections.connection_id = grants.connection_id
                LEFT JOIN provider_accounts AS accounts ON accounts.account_id = grants.account_id
                WHERE grants.tenant_id = ? AND grants.owner_id = ? AND grants.revoked_at IS NULL
                  AND grants.worker_id IN ({placeholders})
                GROUP BY grants.worker_id
                """,
                (tenant_id, owner_id, *normalized_ids),
            ).fetchall()
        for row in rows:
            unavailable = int(row["unavailable_grants"] or 0)
            result[str(row["worker_id"])] = {
                "active_grants": int(row["active_grants"] or 0),
                "unavailable_grants": unavailable,
                "readiness": "action_required" if unavailable else "ready",
            }
        return result

    def revoke_workspace_grant(
        self,
        *,
        grant_id: str,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
    ) -> dict[str, Any]:
        """Safely undo the latest active capability without overwriting later workspace changes."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute(
                """
                SELECT rowid, * FROM workspace_capability_grants
                WHERE grant_id = ? AND tenant_id = ? AND owner_id = ? AND worker_id = ?
                """,
                (grant_id, tenant_id, owner_id, worker_id),
            ).fetchone()
            if grant is None or grant["revoked_at"] is not None:
                raise ControlPlaneError("Workspace capability grant not found for this user")
            latest = conn.execute(
                """
                SELECT rowid, grant_id FROM workspace_capability_grants
                WHERE tenant_id = ? AND owner_id = ? AND worker_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (tenant_id, owner_id, worker_id),
            ).fetchone()
            if latest is None or str(latest["grant_id"]) != grant_id:
                raise ControlPlaneConflict("Remove newer workspace capabilities first")
            worker = conn.execute(
                """
                SELECT bootstrap_bundle_json FROM workers
                WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (worker_id, tenant_id, owner_id),
            ).fetchone()
            if worker is None:
                raise ControlPlaneError("Target workspace is no longer available for this user")
            current_bundle = _parse_json(worker["bootstrap_bundle_json"], {})
            applied_bundle = _parse_json(grant["applied_bootstrap_bundle_json"], {})
            if current_bundle != applied_bundle:
                raise ControlPlaneConflict(
                    "Workspace configuration changed after this capability was enabled; automatic removal was stopped"
                )
            prior_bundle = _parse_json(grant["prior_bootstrap_bundle_json"], {})
            if not isinstance(prior_bundle, dict):
                raise ControlPlaneError("Workspace capability rollback state is invalid")
            conn.execute(
                "UPDATE workers SET bootstrap_bundle_json = ?, updated_at = ? WHERE worker_id = ?",
                (_json(prior_bundle), _worker_updated_at(), worker_id),
            )
            conn.execute(
                "UPDATE workspace_capability_grants SET revoked_at = ? WHERE grant_id = ?",
                (now, grant_id),
            )
            revoked = conn.execute(
                "SELECT * FROM workspace_capability_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
        return self._row(revoked) or {}
