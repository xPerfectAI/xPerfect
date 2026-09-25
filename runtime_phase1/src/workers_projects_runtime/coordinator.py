"""Standalone conversation coordination over existing native sessions and durable runs.

This module never executes a harness or schedules worker runs. Its ledger records accepted
user intent and links it to the service's authoritative delegation/run records.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .failure_classification import is_user_resumable_failure


class CoordinatorConflict(ValueError):
    pass


class CoordinatorScopeError(PermissionError):
    pass


COORDINATOR_SCHEMA_VERSION = 3


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Route(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    profile: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    execution_mode: Literal["host", "docker"]
    connection_id: str = Field(default="", max_length=512)
    resource_class: str = "standard"
    bootstrap_bundle: dict[str, Any] = Field(default_factory=dict)


class CoordinatorScope(StrictModel):
    """Owner-validated origin scope copied to every coordinator child."""

    project_id: str = Field(default="", max_length=512)
    workspace_id: str = Field(default="", max_length=512)
    connection_id: str = Field(default="", max_length=512)
    execution_mode: Literal["host", "docker"] = "host"

    @staticmethod
    def _clean(value: str) -> str:
        value = str(value or "").strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Coordinator scope values must be printable")
        return value

    def model_post_init(self, __context: Any) -> None:
        for value in (self.project_id, self.workspace_id, self.connection_id):
            self._clean(value)
        if self.workspace_id and not self.project_id:
            raise ValueError("Coordinator workspace scope requires a project")


class CoordinatorConfig(StrictModel):
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    max_goals: int = Field(default=100, ge=10, le=1000)
    wake_on_results: bool = True
    routes: list[Route] = Field(default_factory=list, max_length=32)
    bootstrap_bundle: dict[str, Any] = Field(default_factory=dict)
    context_manifest: dict[str, Any] = Field(default_factory=dict)
    developer_instructions: str = ""
    scope: CoordinatorScope = Field(default_factory=CoordinatorScope)


class Goal(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=262144)


class Dispatch(StrictModel):
    goal_id: str
    route_id: str
    instruction: str = Field(min_length=1, max_length=524288)


class Control(StrictModel):
    action: Literal["pause", "resume", "stop", "steer", "retry"]
    run_id: str = ""
    idempotency_key: str = Field(min_length=1, max_length=160)
    message: str = Field(default="", max_length=524288)


class RestoreResume(StrictModel):
    expected_hold_set_hash: str = Field(min_length=64, max_length=64)
    approved_request_ids: list[str] = Field(default_factory=list, max_length=1000)
    approved_turn_ids: list[str] = Field(default_factory=list, max_length=1000)
    approved_goal_ids: list[str] = Field(default_factory=list, max_length=1000)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


_SAFE_HTTP_BLOCKER_CODES = frozenset(
    {
        "provider_account_busy",
        "provider_auth_missing",
        "provider_auth_projection_unavailable",
        "provider_connected_account_reconnect_required",
        "provider_context_limit_exceeded",
        "provider_quota_exhausted",
        "provider_rate_limited",
        "provider_request_rejected",
        "provider_unauthorized",
        "provider_unavailable",
        "provider_upstream_unavailable",
    }
)
_RETRYABLE_ADMISSION_CODES = frozenset({
    "shared_capacity_busy",
    "shared_resource_authority_unavailable",
    "host_capacity",
    "provider_account_busy",
})
_SAFE_SHARED_ADMISSION_CODES = _RETRYABLE_ADMISSION_CODES | frozenset({
    "shared_account_recovery_pending",
    "shared_configuration_required",
    "shared_configuration_invalid",
    "shared_storage_authority_unavailable",
    "shared_account_projection_unavailable",
    "shared_owner_storage_unavailable",
    "shared_account_container_unavailable",
    "shared_runtime_unavailable",
    "shared_linux_runtime_required",
    "shared_workspace_required",
    "shared_host_execution_unavailable",
    "shared_profile_unavailable",
    "shared_provider_selection_invalid",
    "shared_provider_selection_unavailable",
    "shared_provider_account_unavailable",
    "shared_provider_account_mismatch",
    "shared_provider_account_not_ready",
})


def _safe_blocker_code(exc: Exception) -> str:
    """Keep typed admission reasons while excluding provider error prose."""

    typed_code = str(getattr(exc, "code", "") or "").strip()
    if typed_code:
        return typed_code
    shared_code = str(getattr(exc, "reason_code", "") or "").strip()
    if shared_code in _SAFE_SHARED_ADMISSION_CODES:
        return shared_code
    if not isinstance(exc, HTTPException):
        return type(exc).__name__
    detail = exc.detail
    candidates = []
    if isinstance(detail, dict):
        candidates.extend(
            detail.get(key)
            for key in ("code", "failure_class", "reason_class")
        )
    elif isinstance(detail, str):
        candidates.append(detail.strip())
    for candidate in candidates:
        if candidate in _SAFE_HTTP_BLOCKER_CODES:
            return candidate
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"http_{status}"
    return "provider_admission_failed"


from .bootstrap import worker_prompt_layer_producer


@worker_prompt_layer_producer("system_instructions", "tool_schemas")
def prompt_manifest() -> dict[str, Any]:
    source = Path(__file__).with_name("prompts") / "coordinator.md"
    content = source.read_text(encoding="utf-8")
    return {"id": "xperfect.coordinator", "version": 1,
            "source": "workers_projects_runtime/prompts/coordinator.md",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "tool_schema_sha256": hashlib.sha256(source.with_name("coordinator-tools.json").read_bytes()).hexdigest(), "content": content}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS coordinator_conversations (
 conversation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 config_json TEXT NOT NULL, prompt_json TEXT NOT NULL, created_at TEXT NOT NULL,
 scope_json TEXT NOT NULL DEFAULT '{}',
 restore_hold INTEGER NOT NULL DEFAULT 0, restore_hold_set_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS coordinator_turns (
 conversation_id TEXT NOT NULL REFERENCES coordinator_conversations(conversation_id),
 turn_id TEXT NOT NULL, message TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
 blocker TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '',
 response_json TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'interactive', created_at TEXT NOT NULL,
 retry_after_at TEXT NOT NULL DEFAULT '', retry_attempts INTEGER NOT NULL DEFAULT 0,
 restore_hold INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(conversation_id, turn_id)
);
CREATE TABLE IF NOT EXISTS coordinator_goals (
 conversation_id TEXT NOT NULL REFERENCES coordinator_conversations(conversation_id),
 goal_id TEXT NOT NULL, source_turn_id TEXT NOT NULL, text TEXT NOT NULL,
 dispatch_json TEXT NOT NULL DEFAULT '', work_ref TEXT NOT NULL DEFAULT '',
 worker_id TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
 blocker TEXT NOT NULL DEFAULT '', intent_state TEXT NOT NULL DEFAULT 'accepted', created_at TEXT NOT NULL,
 restore_hold INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(conversation_id, goal_id),
 FOREIGN KEY(conversation_id, source_turn_id) REFERENCES coordinator_turns(conversation_id, turn_id)
);
CREATE TABLE IF NOT EXISTS coordinator_actions (
 conversation_id TEXT NOT NULL, goal_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 payload_json TEXT NOT NULL, response_json TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(conversation_id,goal_id,idempotency_key),
 FOREIGN KEY(conversation_id,goal_id) REFERENCES coordinator_goals(conversation_id,goal_id)
);
CREATE TABLE IF NOT EXISTS coordinator_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 conversation_id TEXT NOT NULL REFERENCES coordinator_conversations(conversation_id),
 kind TEXT NOT NULL, identity TEXT NOT NULL, payload_json TEXT NOT NULL, delivered_turn_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(conversation_id, kind, identity)
);
"""


class CoordinatorService:
    def __init__(self, store: Any, service: Any, provider: Any) -> None:
        self.store, self.service, self.provider = store, service, provider
        self._reconcile_after = ""
        from .schema_version import begin_schema_migration, execute_schema_script, require_compatible_schema, record_schema_version
        with store._connect() as conn:
            begin_schema_migration(conn)
            require_compatible_schema(conn, component="coordinator", target_version=COORDINATOR_SCHEMA_VERSION)
            execute_schema_script(conn, _SCHEMA)
            for table, column, definition in (
                ("coordinator_conversations", "scope_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("coordinator_conversations", "restore_hold", "INTEGER NOT NULL DEFAULT 0"),
                ("coordinator_conversations", "restore_hold_set_hash", "TEXT NOT NULL DEFAULT ''"),
                ("coordinator_turns", "restore_hold", "INTEGER NOT NULL DEFAULT 0"),
                ("coordinator_turns", "retry_after_at", "TEXT NOT NULL DEFAULT ''"),
                ("coordinator_turns", "retry_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("coordinator_goals", "restore_hold", "INTEGER NOT NULL DEFAULT 0"),
            ):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            record_schema_version(conn, component="coordinator", version=COORDINATOR_SCHEMA_VERSION)

    def _conversation(self, tenant: str, owner: str, conversation_id: str) -> dict:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM coordinator_conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                               (conversation_id, tenant, owner)).fetchone()
        if row is None:
            raise CoordinatorScopeError("Conversation unavailable")
        return dict(row)

    @staticmethod
    def _hold_digest(items: list[str] | tuple[str, ...] | set[str]) -> str:
        return digest(sorted({str(item) for item in items if str(item)}))

    def _restore_hold_items(self, conn: Any, conversation_id: str) -> list[str]:
        items = [
            f"turn:{row['turn_id']}"
            for row in conn.execute(
                "SELECT turn_id FROM coordinator_turns "
                "WHERE conversation_id=? AND restore_hold=1 ORDER BY turn_id",
                (conversation_id,),
            )
        ]
        items.extend(
            f"goal:{row['goal_id']}"
            for row in conn.execute(
                "SELECT goal_id FROM coordinator_goals "
                "WHERE conversation_id=? AND restore_hold=1 ORDER BY goal_id",
                (conversation_id,),
            )
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_requests'"
        ).fetchone():
            items.extend(
                f"request:{row['request_id']}"
                for row in conn.execute(
                    "SELECT p.request_id FROM provider_requests p "
                    "JOIN provider_sessions s ON s.session_id=p.session_id "
                    "WHERE s.conversation_id=? AND p.restore_hold=1 ORDER BY p.request_id",
                    (conversation_id,),
                )
            )
        return sorted(items)

    def _assert_not_restore_held(self, conversation: dict) -> None:
        if bool(conversation.get("restore_hold")):
            raise CoordinatorConflict("Conversation is held for exact continuity resume")

    def _validated_scope(
        self, tenant: str, owner: str, scope: CoordinatorScope
    ) -> dict[str, Any]:
        project_id = str(scope.project_id or "").strip()
        workspace_id = str(scope.workspace_id or "").strip()
        if not project_id:
            if workspace_id or scope.connection_id:
                raise CoordinatorScopeError("Coordinator origin scope is unavailable")
            return {}
        project = self.store.get_project(
            project_id, tenant_id=tenant, owner_id=owner
        )
        if project is None:
            raise CoordinatorScopeError("Coordinator origin project is unavailable")
        if workspace_id:
            workspace = self.store.get_execution_workspace(
                workspace_id, tenant, owner
            )
            if (
                workspace is None
                or str(workspace.get("project_id") or "") != project_id
            ):
                raise CoordinatorScopeError(
                    "Coordinator origin workspace is unavailable"
                )
            if str(workspace.get("execution_mode") or "") != str(scope.execution_mode):
                raise CoordinatorScopeError(
                    "Coordinator origin placement does not match the workspace"
                )
        connection_id = str(scope.connection_id or "").strip()
        if connection_id:
            self._assert_owned_connection(tenant, owner, connection_id)
        return {
            "version": 1,
            "tenant_id": tenant,
            "owner_id": owner,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "execution_mode": str(scope.execution_mode),
            "ref": "",
            "source_event_id": "",
            "source_revision": 0,
            "surface": "coordinator",
        }

    def _assert_owned_connection(
        self, tenant: str, owner: str, connection_id: str
    ) -> None:
        """Accept only an existing owner-scoped account or configured route."""

        control_plane = getattr(self.service, "control_plane_store", None)
        if control_plane is None:
            raise CoordinatorScopeError("Coordinator connection is unavailable")
        try:
            account = control_plane.get_provider_account(
                account_id=connection_id,
                tenant_id=tenant,
                owner_id=owner,
            )
            connections = control_plane.list_connections(
                tenant_id=tenant,
                owner_id=owner,
            )
        except Exception as exc:
            raise CoordinatorScopeError("Coordinator connection is unavailable") from exc
        if account is None and not any(
            str(item.get("connection_id") or "") == connection_id
            for item in connections
            if isinstance(item, dict)
        ):
            raise CoordinatorScopeError("Coordinator connection is unavailable")

    def _conversation_scope(self, conversation: dict) -> dict[str, Any]:
        raw = conversation.get("scope_json")
        if raw in (None, "", "{}"):
            config = CoordinatorConfig.model_validate_json(
                conversation["config_json"]
            )
            scope = config.scope
            tenant = str(conversation.get("tenant_id") or "local")
            owner = str(conversation.get("owner_id") or "")
            return self._validated_scope(tenant, owner, scope)
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CoordinatorScopeError("Coordinator origin scope is invalid") from exc
        if not isinstance(parsed, dict):
            raise CoordinatorScopeError("Coordinator origin scope is invalid")
        if parsed.get("version", 1) != 1 or isinstance(parsed.get("version", 1), bool):
            raise CoordinatorScopeError("Coordinator origin scope is invalid")
        tenant = str(conversation.get("tenant_id") or "local")
        owner = str(conversation.get("owner_id") or "")
        if str(parsed.get("tenant_id") or tenant) != tenant or str(
            parsed.get("owner_id") or owner
        ) != owner:
            raise CoordinatorScopeError("Coordinator origin scope is invalid")
        try:
            source_revision = int(parsed.get("source_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise CoordinatorScopeError("Coordinator origin scope is invalid") from exc
        if source_revision < 0 or isinstance(parsed.get("source_revision"), bool):
            raise CoordinatorScopeError("Coordinator origin scope is invalid")
        extensions: dict[str, str | int] = {}
        for key in ("ref", "source_event_id", "surface"):
            value = str(parsed.get(key) or "")
            if len(value) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise CoordinatorScopeError("Coordinator origin scope is invalid")
            extensions[key] = value
        return self._validated_scope(
            tenant,
            owner,
            CoordinatorScope(
                project_id=str(parsed.get("project_id") or ""),
                workspace_id=str(parsed.get("workspace_id") or ""),
                connection_id=str(parsed.get("connection_id") or ""),
                execution_mode=str(parsed.get("execution_mode") or "host"),
            ),
        ) | {"version": 1, **extensions, "source_revision": source_revision}

    def _assert_foreground_allowed_ai(
        self,
        conversation: dict,
        config: CoordinatorConfig,
    ) -> None:
        """Apply the same owner policy to the coordinator's own native turn."""

        scope = self._conversation_scope(conversation)
        if not scope:
            return
        policy_service = getattr(self.service, "allowed_ai_policy", None)
        if policy_service is None:
            return
        model = self.provider._model(config.model)
        worker = {
            "tenant_id": str(conversation.get("tenant_id") or "local"),
            "owner_id": str(conversation.get("owner_id") or ""),
            "project_id": str(scope.get("project_id") or ""),
            "workspace_id": str(scope.get("workspace_id") or ""),
            "profile": str(getattr(model, "harness_profile", "") or ""),
            "model": str(getattr(model, "native_model", "") or ""),
            "origin_scope": dict(scope),
            "bootstrap_bundle_json": json.dumps(
                {
                    "connection_id": str(scope.get("connection_id") or ""),
                    "provider_model": str(getattr(model, "native_model", "") or ""),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        policy_service.admission_snapshot(worker)

    def create(self, tenant: str, owner: str, config: CoordinatorConfig) -> dict:
        if not tenant or not owner:
            raise CoordinatorScopeError("Authenticated owner required")
        if len({route.id for route in config.routes}) != len(config.routes):
            raise CoordinatorConflict("Route IDs must be unique")
        # Existing registry remains the authority for exact foreground model and effort.
        model = self.provider._model(config.model)
        if config.effort not in model.effort_choices:
            raise CoordinatorConflict("Unsupported exact model effort")
        identity = f"coordinator-{uuid.uuid4().hex}"
        created_project_id = ""
        if not config.scope.project_id:
            # A standalone conversation owns one persistent policy origin.
            # Subsequent native sessions and helpers inherit this same project.
            profile = str(getattr(model, "harness_profile", "") or "")
            if not profile and config.routes:
                profile = config.routes[0].profile
            project = self.store.create_project(
                owner_id=owner,
                title="Conversation",
                goal="Standalone conversation",
                default_worker_profile=profile or "codex-cli",
                tenant_id=tenant,
            )
            created_project_id = project["project_id"]
            config = config.model_copy(update={
                "scope": config.scope.model_copy(update={"project_id": project["project_id"]}),
            })
        scope = self._validated_scope(tenant, owner, config.scope)
        if scope:
            scope = {
                **scope,
                "ref": identity,
                "source_event_id": identity,
            }
        with self.store._connect() as conn:
            if created_project_id:
                conn.execute(
                    "UPDATE projects SET origin_scope_json=? "
                    "WHERE project_id=? AND tenant_id=? AND owner_id=?",
                    (canonical(scope), created_project_id, tenant, owner),
                )
            conn.execute(
                "INSERT INTO coordinator_conversations "
                "(conversation_id,tenant_id,owner_id,config_json,prompt_json,scope_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    identity,
                    tenant,
                    owner,
                    canonical(config.model_dump()),
                    canonical(prompt_manifest()),
                    canonical(scope),
                    now(),
                ),
            )
        return self.snapshot(tenant, owner, identity)

    def accept_turn(self, tenant: str, owner: str, conversation_id: str, turn_id: str,
                    message: str, goals: list[Goal] | None = None) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        self._assert_not_restore_held(conversation)
        if not turn_id or not message:
            raise CoordinatorConflict("Turn ID and message are required")
        goals = goals or []
        config = CoordinatorConfig.model_validate_json(conversation["config_json"])
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]:
                raise CoordinatorConflict("Conversation is held for exact continuity resume")
            old = conn.execute("SELECT message FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone()
            if old and old["message"] != message:
                raise CoordinatorConflict("Turn idempotency key changed content")
            conn.execute("INSERT OR IGNORE INTO coordinator_turns(conversation_id,turn_id,message,created_at) VALUES(?,?,?,?)", (conversation_id, turn_id, message, now()))
            self._accept_goals(conn, conversation_id, turn_id, goals, config.max_goals)
        return self.snapshot(tenant, owner, conversation_id)

    @staticmethod
    def _accept_goals(conn: Any, conversation_id: str, turn_id: str, goals: list[Goal], maximum: int) -> None:
        existing = {r["goal_id"]: dict(r) for r in conn.execute("SELECT * FROM coordinator_goals WHERE conversation_id=?", (conversation_id,))}
        if len({g.id for g in goals}) != len(goals):
            raise CoordinatorConflict("Duplicate goal IDs in batch")
        if len(set(existing) | {g.id for g in goals}) > maximum:
            raise CoordinatorConflict("Configured goal budget exceeded; no goals in this batch were accepted")
        for goal in goals:
            if goal.id in existing and (existing[goal.id]["text"] != goal.text or existing[goal.id]["source_turn_id"] != turn_id):
                raise CoordinatorConflict("Goal identity changed content or source")
        for goal in goals:
            conn.execute("INSERT OR IGNORE INTO coordinator_goals(conversation_id,goal_id,source_turn_id,text,created_at) VALUES(?,?,?,?,?)", (conversation_id, goal.id, turn_id, goal.text, now()))

    def accept_goals(self, tenant: str, owner: str, conversation_id: str, turn_id: str, goals: list[Goal]) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        self._assert_not_restore_held(conversation)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]:
                raise CoordinatorConflict("Conversation is held for exact continuity resume")
            if not conn.execute("SELECT 1 FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone():
                raise CoordinatorConflict("Source turn unavailable")
            self._accept_goals(conn, conversation_id, turn_id, goals,
                               CoordinatorConfig.model_validate_json(conversation["config_json"]).max_goals)
        return self.snapshot(tenant, owner, conversation_id)

    def _goal(self, conversation_id: str, goal_id: str) -> dict:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM coordinator_goals WHERE conversation_id=? AND goal_id=?", (conversation_id, goal_id)).fetchone()
        if row is None:
            raise CoordinatorConflict("Goal unavailable")
        return dict(row)

    def dispatch(self, tenant: str, owner: str, conversation_id: str, dispatch: Dispatch) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        self._assert_not_restore_held(conversation)
        origin_scope = self._conversation_scope(conversation)
        config = CoordinatorConfig.model_validate_json(conversation["config_json"])
        route = next((r for r in config.routes if r.id == dispatch.route_id), None)
        if route is None:
            raise CoordinatorScopeError("Route outside coordinator authority")
        if route.connection_id:
            self._assert_owned_connection(tenant, owner, route.connection_id)
        goal = self._goal(conversation_id, dispatch.goal_id)
        serialized = canonical(dispatch.model_dump())
        bundle = {**route.bootstrap_bundle,
                  "viventium_launch_authority": {"version": 1, "kind": "conversation_orchestrator", "execution_mode": route.execution_mode,
                    "worker_model": route.model, "worker_reasoning_effort": route.effort}}
        if route.connection_id:
            configured_id = str(bundle.get("connection_id") or "").strip()
            provider_account = bundle.get("provider_account")
            account_id = (
                str(provider_account.get("account_id") or "").strip()
                if isinstance(provider_account, dict) else ""
            )
            if (configured_id and configured_id != route.connection_id) or (
                account_id and account_id != route.connection_id
            ):
                raise CoordinatorScopeError("Route connection conflicts with its bootstrap")
            bundle["connection_id"] = route.connection_id
        standalone_model = None
        from .execution_profile import packaged_linux
        if packaged_linux():
            # The packaged runtime already has owner-scoped, isolated member boxes
            # and native account projection. It has no Viventium proxy services.
            # Use that existing substrate for its own children; the Viventium
            # clean-room envelope remains unchanged outside this profile.
            from .service import (
                _contains_parallel_forbidden_authority_key,
                _parallel_clean_room_rejected,
                _validate_parallel_clean_room_environment,
                _validate_parallel_clean_room_files,
                _validate_parallel_clean_room_mcp,
            )
            if (
                route.execution_mode != "docker"
                or "execution_policy" in bundle
                or "glasshive_capability_authorization" in bundle
                or "glasshive_capability_broker" in bundle
                or _contains_parallel_forbidden_authority_key(bundle)
            ):
                raise _parallel_clean_room_rejected("caller provider credentials are not allowed")
            _validate_parallel_clean_room_environment(bundle)
            _validate_parallel_clean_room_files(bundle)
            _validate_parallel_clean_room_mcp(bundle)
            standalone_model, bundle = self.service._configured_parallel_worker_route(
                route.profile, route.execution_mode, bundle
            )
            bundle.pop("viventium_launch_authority")
            account_store = getattr(self.service, "control_plane_store", None)
            if account_store is None:
                raise CoordinatorScopeError("Worker account choices are unavailable")
            from .mission_provider_accounts import _PROFILE_PROVIDERS
            providers = _PROFILE_PROVIDERS.get(route.profile)
            if not providers:
                raise CoordinatorScopeError("This worker has no contained account route")
            from .mission_provider_accounts import mission_provider_account_selection
            explicit = mission_provider_account_selection({"bootstrap_bundle": bundle})
            selected_account = str(route.connection_id or "").strip()
            if explicit is not None:
                if selected_account and selected_account != explicit.account_id:
                    raise CoordinatorScopeError("Worker account choice conflicts with its route")
                selected_account = explicit.account_id
            if not selected_account:
                candidates = [account for account in account_store.list_provider_accounts(
                    tenant_id=tenant, owner_id=owner
                ) if account.get("provider") in providers and account.get("status") == "ready"]
                defaults = [account for account in candidates if account.get("is_default")]
                choices = defaults or candidates
                if len(choices) == 1:
                    selected_account = str(choices[0]["account_id"])
            account = account_store.get_provider_account(
                account_id=selected_account, tenant_id=tenant, owner_id=owner
            ) if selected_account else None
            if (account is None or account.get("provider") not in providers
                    or account.get("status") != "ready"):
                raise CoordinatorScopeError("Choose a ready account for this worker")
            bundle["provider_account"] = {
                "policy": "personal_required", "account_id": selected_account,
            }
        # Intent precedes service mutation. A restart repeats the same reservation key;
        # reserve_delegation binds its exact request digest and recovers its existing run.
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT g.dispatch_json,g.intent_state,g.restore_hold,c.restore_hold AS conversation_restore_hold "
                "FROM coordinator_goals g JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id "
                "WHERE g.conversation_id=? AND g.goal_id=?",
                (conversation_id, dispatch.goal_id),
            ).fetchone()
            if current["intent_state"] == "cancelled":
                raise CoordinatorConflict("Goal was stopped before dispatch")
            if current["restore_hold"] or current["conversation_restore_hold"]:
                raise CoordinatorConflict("Goal is held for exact continuity resume")
            if current["dispatch_json"] and current["dispatch_json"] != serialized:
                raise CoordinatorConflict("Goal already has a different dispatch; use exact run controls")
            conn.execute("UPDATE coordinator_goals SET dispatch_json=? WHERE conversation_id=? AND goal_id=? AND restore_hold=0", (serialized, conversation_id, dispatch.goal_id))
            # A goal's first dispatch has one stable order among this conversation's delegations.
            admission_ordinal = 0 if current["dispatch_json"] else conn.execute(
                "SELECT COUNT(*) FROM coordinator_goals WHERE conversation_id=? AND dispatch_json<>''",
                (conversation_id,),
            ).fetchone()[0]
        try:
            qa_admission = getattr(self.service, "local_qa_coordinator_admission", None)
            if admission_ordinal and qa_admission is not None:
                qa_admission(tenant_id=tenant, owner_id=owner, conversation_id=conversation_id,
                             ordinal=admission_ordinal)
            reservation = self.service.reserve_delegation(
                tenant_id=tenant, owner_id=owner, idempotency_key=f"{conversation_id}:{goal['goal_id']}",
                request_digest=digest({
                    "dispatch": dispatch.model_dump(),
                    "route": route.model_dump(),
                    "goal": goal["text"],
                    "origin_scope": origin_scope,
                }),
                origin_ref=f"{conversation_id}:{goal['goal_id']}", title=goal["goal_id"], goal=goal["text"], instruction=dispatch.instruction,
                origin_surface="coordinator", worker_name=goal["goal_id"], worker_role="worker",
                profile=route.profile, execution_mode=route.execution_mode, resource_class=route.resource_class,
                bootstrap_bundle=bundle,
                origin_scope=origin_scope or None,
                trusted_coordinator_model=standalone_model,
                start_run=False,
                emit_callback=False)
        except Exception as exc:
            # Keep all accepted siblings. Do not persist exception prose, which can contain
            # native credentials or private provider output.
            with self.store._connect() as conn:
                conn.execute("UPDATE coordinator_goals SET blocker=? WHERE conversation_id=? AND goal_id=?",
                             (_safe_blocker_code(exc), conversation_id, goal["goal_id"]))
            return self.goal_snapshot(tenant, owner, conversation_id, goal["goal_id"])
        with self.store._connect() as conn:
            attached = conn.execute(
                "UPDATE coordinator_goals SET work_ref=?,worker_id=?,run_id=?,blocker='' "
                "WHERE conversation_id=? AND goal_id=? AND restore_hold=0 "
                "AND (run_id='' OR run_id=?) "
                "AND (SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?)=0",
                (reservation["work_ref"], reservation["worker_id"], reservation["initial_run_id"], conversation_id, goal["goal_id"], reservation["initial_run_id"], conversation_id),
            )
        if attached.rowcount != 1:
            return self.goal_snapshot(tenant, owner, conversation_id, goal["goal_id"])
        # Attach before execution. Existing reservation recovery owns crash recovery here.
        if self._goal(conversation_id, goal["goal_id"])["intent_state"] == "cancelled":
            self.service.cancel_run(reservation["worker_id"], reservation["initial_run_id"])
        else:
            self.service.start_assigned_run(reservation["worker_id"])
        return self.goal_snapshot(tenant, owner, conversation_id, goal["goal_id"])

    def goal_snapshot(self, tenant: str, owner: str, conversation_id: str, goal_id: str) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        goal = self._goal(conversation_id, goal_id)
        result = {k: goal[k] for k in ("goal_id", "source_turn_id", "text", "work_ref", "worker_id", "run_id", "blocker")}
        result["state"] = (
            "restore_held" if bool(conversation.get("restore_hold")) or bool(goal.get("restore_hold"))
            else "cancelled" if goal["intent_state"] == "cancelled"
            else "blocked" if goal["blocker"] else "accepted"
        )
        if goal["run_id"]:
            run = self.store.get_run(goal["run_id"])
            worker = self.store.get_worker(goal["worker_id"])
            if not run or not worker or worker.get("tenant_id", "local") != tenant or worker.get("owner_id") != owner or run["worker_id"] != goal["worker_id"]:
                raise CoordinatorScopeError("Bound result unavailable")
            result.update({"state": result["state"] if result["state"] == "restore_held" else run["state"], "failure_class": run.get("failure_class", ""),
                           "output_text": run.get("output_text", ""), "error_text": run.get("error_text", ""),
                           "active_attempt_id": run.get("active_attempt_id", "")})
            result["retryable"] = (
                result["state"] == "failed"
                and goal["intent_state"] != "cancelled"
                and is_user_resumable_failure(
                    failure_class=run.get("failure_class"),
                    retryable=run.get("failure_retryable"),
                    runtime_invoked_at=run.get("runtime_invoked_at", ...),
                    started_at=run.get("started_at", ...),
                )
            )
        return result

    def snapshot(self, tenant: str, owner: str, conversation_id: str) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        with self.store._connect() as conn:
            goals = conn.execute("SELECT goal_id FROM coordinator_goals WHERE conversation_id=? ORDER BY created_at,goal_id", (conversation_id,)).fetchall()
            turns = [dict(r) for r in conn.execute("SELECT * FROM coordinator_turns WHERE conversation_id=? ORDER BY created_at,turn_id", (conversation_id,))]
            # Foreground native work has no coordinator goal until the model
            # explicitly records one. Its owner still needs permission controls.
            foreground_native_runs = [dict(r) for r in conn.execute(
                "SELECT DISTINCT r.worker_id,r.run_id FROM coordinator_turns t "
                "JOIN provider_requests p ON p.request_id=t.request_id "
                "JOIN runs r ON r.run_id=p.run_id "
                "JOIN workers w ON w.worker_id=r.worker_id "
                "WHERE t.conversation_id=? AND t.response_json='' AND t.blocker='' "
                "AND t.restore_hold=0 AND p.tenant_id=? AND p.owner_id=? "
                "AND r.tenant_id=? AND w.tenant_id=? AND w.owner_id=? "
                "AND r.state='running'",
                (conversation_id, tenant, owner, tenant, tenant, owner),
            )]
        config = CoordinatorConfig.model_validate_json(conversation["config_json"])
        return {"conversation_id": conversation_id, "model": config.model, "effort": config.effort,
                "max_goals": config.max_goals, "routes": [{k: v for k, v in r.model_dump().items() if k != "bootstrap_bundle"} for r in config.routes],
                # Status and results remain readable after a scoped origin is
                # removed. New starts and dispatch still validate the origin.
                "scope": json.loads(str(conversation.get("scope_json") or "{}")),
                "context_manifest": config.context_manifest,
                "prompt": {k: v for k, v in json.loads(conversation["prompt_json"]).items() if k != "content"},
                "restore_hold": bool(conversation.get("restore_hold")),
                "restore_hold_set_hash": str(conversation.get("restore_hold_set_hash") or ""),
                "turns": [{k: v for k, v in t.items() if k != "payload_json"} for t in turns],
                "foreground_native_runs": foreground_native_runs,
                "goals": [self._goal_summary(self.goal_snapshot(tenant, owner, conversation_id, r["goal_id"])) for r in goals]}

    def control(self, tenant: str, owner: str, conversation_id: str, goal_id: str, control: Control) -> dict:
        self._assert_not_restore_held(self._conversation(tenant, owner, conversation_id))
        goal = self.goal_snapshot(tenant, owner, conversation_id, goal_id)
        if control.run_id:
            with self.store._connect() as conn:
                sharing = conn.execute("SELECT COUNT(*) FROM coordinator_goals WHERE conversation_id=? AND run_id=?", (conversation_id, control.run_id)).fetchone()[0]
            if sharing > 1:
                raise CoordinatorConflict("These goals share the current reply; use the conversation reply control")
        payload = canonical(control.model_dump())
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            held = conn.execute(
                "SELECT c.restore_hold,g.restore_hold,g.run_id,g.intent_state FROM coordinator_conversations c "
                "JOIN coordinator_goals g ON g.conversation_id=c.conversation_id "
                "WHERE c.conversation_id=? AND g.goal_id=?",
                (conversation_id, goal_id),
            ).fetchone()
            if held and (held[0] or held[1]):
                raise CoordinatorConflict("Goal is held for exact continuity resume")
            previous = conn.execute("SELECT * FROM coordinator_actions WHERE conversation_id=? AND goal_id=? AND idempotency_key=?", (conversation_id, goal_id, control.idempotency_key)).fetchone()
            if previous and previous["payload_json"] != payload:
                raise CoordinatorConflict("Control idempotency key changed content")
            if previous and previous["response_json"]:
                return json.loads(previous["response_json"])
            if control.run_id != held["run_id"] and not previous:
                raise CoordinatorConflict("Control targets a stale or unrelated run")
            if held["intent_state"] == "cancelled" and not previous:
                raise CoordinatorConflict("Goal was already stopped")
            if control.action == "retry":
                if not control.run_id or control.message:
                    raise CoordinatorConflict("Retry requires the exact failed run without new instructions")
                if not previous and (goal["state"] != "failed" or not goal.get("retryable")):
                    raise CoordinatorConflict("This failed work cannot be retried")
                if not previous and conn.execute(
                    "SELECT 1 FROM coordinator_actions WHERE conversation_id=? AND goal_id=? "
                    "AND response_json='' AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.action')='retry' "
                    "AND json_extract(payload_json,'$.run_id')=? LIMIT 1",
                    (conversation_id, goal_id, control.run_id),
                ).fetchone():
                    raise CoordinatorConflict("Retry is already in progress for this goal")
            if not control.run_id:
                if control.action != "stop":
                    raise CoordinatorConflict("This goal has no run to control")
            if control.action == "stop":
                conn.execute(
                    "UPDATE coordinator_goals SET intent_state='cancelled' "
                    "WHERE conversation_id=? AND goal_id=? AND run_id=?",
                    (conversation_id, goal_id, control.run_id),
                )
            conn.execute("INSERT OR IGNORE INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json) VALUES(?,?,?,?)", (conversation_id, goal_id, control.idempotency_key, payload))
        if control.action == "retry":
            return self._retry_goal(tenant, owner, conversation_id, goal_id, control)
        worker_id = goal["worker_id"]
        stop_outcome = None
        if control.run_id:
            if control.action == "stop":
                # The exact work-stop lifecycle also handles paused and
                # prelaunch-reserved runs. cancel_run leaves paused runs live.
                stop_outcome = self.service.stop_run(worker_id, control.run_id)
            elif control.action == "pause":
                self.service.pause_worker(worker_id, run_id=control.run_id)
            elif control.action == "resume":
                self.service.resume_worker(worker_id, run_id=control.run_id)
            else:
                if not control.message:
                    raise CoordinatorConflict("Steer message required")
                result = self.service.steer_worker(worker_id, control.message, run_id=control.run_id,
                                                   idempotency_key=control.idempotency_key)
                replacement = str(result.get("replacement_run_id") or result.get("run_id") or "")
                if replacement and replacement != control.run_id:
                    run = self.store.get_run(replacement)
                    if not run or run["worker_id"] != worker_id:
                        raise CoordinatorConflict("Invalid steer replacement receipt")
                    with self.store._connect() as conn:
                        conn.execute("UPDATE coordinator_goals SET run_id=? WHERE conversation_id=? AND goal_id=?", (replacement, conversation_id, goal_id))
        response = self.goal_snapshot(tenant, owner, conversation_id, goal_id)
        if stop_outcome is not None:
            terminal = response["state"] in {"completed", "failed", "cancelled", "interrupted"}
            if not stop_outcome.get("accepted") and not terminal:
                raise CoordinatorConflict("Exact Stop was not accepted; retry this work")
            if stop_outcome.get("confirmation_pending") or not terminal:
                # A pending stop is not a completed idempotent response. The
                # same key must be able to retry after exact termination proof.
                return {**response, "confirmation_pending": True}
        with self.store._connect() as conn:
            conn.execute("UPDATE coordinator_actions SET response_json=? WHERE conversation_id=? AND goal_id=? AND idempotency_key=?", (canonical(response), conversation_id, goal_id, control.idempotency_key))
        return response

    def _retry_goal(self, tenant: str, owner: str, conversation_id: str, goal_id: str, control: Control) -> dict:
        """Continue one failed child and bind its exact replacement to the same goal."""

        source = self.store.get_run(control.run_id)
        goal = self._goal(conversation_id, goal_id)
        if (
            not source or str(source.get("worker_id") or "") != goal["worker_id"]
            or not goal["work_ref"]
            or (goal["run_id"] == control.run_id and (
                source.get("state") != "failed"
                or not is_user_resumable_failure(
                    failure_class=source.get("failure_class"),
                    retryable=source.get("failure_retryable"),
                    runtime_invoked_at=source.get("runtime_invoked_at", ...),
                    started_at=source.get("started_at", ...),
                )
            ))
        ):
            raise CoordinatorConflict("This failed work cannot be retried")
        delegation = self.store.get_delegation(goal["work_ref"], tenant_id=tenant, owner_id=owner)
        if not delegation or str(delegation.get("worker_id") or "") != goal["worker_id"]:
            raise CoordinatorScopeError("Bound work unavailable")
        effect_key = f"coordinator-retry:{conversation_id}:{goal_id}:{control.idempotency_key}"
        replacement_id = self.service.active_work_effect_run_id(delegation, idempotency_key=effect_key)
        replacement = self.store.get_run(replacement_id)
        if replacement is None:
            if goal["intent_state"] == "cancelled" or goal["run_id"] != control.run_id:
                raise CoordinatorConflict("Retry targets a stopped or changed goal")
            try:
                outcome = self.service.execute_active_work_action(
                    delegation, action="retry", idempotency_key=effect_key,
                    expected_run_id=control.run_id,
                    start_processor=False,
                )
            except RuntimeError as exc:
                raise CoordinatorConflict("Retry is unavailable for this exact work") from exc
            replacement_id = str(outcome.get("run_id") or "")
            replacement = self.store.get_run(replacement_id)
        if (
            not replacement or replacement_id == control.run_id
            or str(replacement.get("worker_id") or "") != goal["worker_id"]
            or str(replacement.get("project_id") or "") != str(source.get("project_id") or "")
            or str(replacement.get("tenant_id") or "local") != tenant
        ):
            raise CoordinatorConflict("Invalid retry replacement receipt")
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE coordinator_goals SET run_id=?,blocker='' "
                "WHERE conversation_id=? AND goal_id=? AND run_id=? "
                "AND intent_state='accepted' AND restore_hold=0 "
                "AND (SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?)=0",
                (replacement_id, conversation_id, goal_id, control.run_id, conversation_id),
            ).rowcount
            current = conn.execute(
                "SELECT run_id,intent_state FROM coordinator_goals WHERE conversation_id=? AND goal_id=?",
                (conversation_id, goal_id),
            ).fetchone()
        if not updated and (not current or current["run_id"] != replacement_id or current["intent_state"] != "accepted"):
            self.service.stop_run(goal["worker_id"], replacement_id)
            raise CoordinatorConflict("Retry lost the exact goal binding")
        # The replacement was reserved without submitting its processor.
        # Exact Stop now targets this bound run and can win before native start.
        self.service.start_assigned_run(goal["worker_id"])
        response = self.goal_snapshot(tenant, owner, conversation_id, goal_id)
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE coordinator_actions SET response_json=? "
                "WHERE conversation_id=? AND goal_id=? AND idempotency_key=? AND response_json=''",
                (canonical(response), conversation_id, goal_id, control.idempotency_key),
            )
            saved = conn.execute(
                "SELECT response_json FROM coordinator_actions "
                "WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                (conversation_id, goal_id, control.idempotency_key),
            ).fetchone()
        return json.loads(saved["response_json"]) if saved and saved["response_json"] else response

    def start_turn(self, tenant: str, owner: str, conversation_id: str, turn_id: str) -> dict:
        from .conversation_provider import ChatCompletionRequest

        conversation = self._conversation(tenant, owner, conversation_id)
        config = CoordinatorConfig.model_validate_json(conversation["config_json"])
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            live_conversation = conn.execute("SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            turn = conn.execute("SELECT * FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone()
            if not turn:
                raise CoordinatorConflict("Accepted turn unavailable")
            if bool(live_conversation[0]) or bool(turn["restore_hold"]):
                return {"turn_id": turn_id, "state": "restore_held", "blocker": "continuity_restore_hold"}
            if turn["origin"] != "interactive" and conn.execute("SELECT 1 FROM coordinator_turns WHERE conversation_id=? AND origin='interactive' AND request_id='' AND blocker='' LIMIT 1", (conversation_id,)).fetchone():
                return {"turn_id": turn_id, "state": "queued"}
            if turn["blocker"] == "cancelled":
                return {"turn_id": turn_id, "state": "cancelled"}
            if turn["request_id"]:
                return {"turn_id": turn_id, "request_id": turn["request_id"], "state": "accepted"}
            if not turn["payload_json"]:
                messages = [{"role": "system", "content": json.loads(conversation["prompt_json"])["content"]}]
                if config.developer_instructions:
                    messages.append({"role": "developer", "content": config.developer_instructions})
                # Exact source history is retained; the existing provider admission layer
                # either admits it or returns an explicit budget error. No silent slicing.
                prior = conn.execute("SELECT * FROM coordinator_turns WHERE conversation_id=? AND created_at<=? ORDER BY created_at,turn_id", (conversation_id, turn["created_at"])).fetchall()
                for item in prior:
                    messages.append({"role": "user" if item["origin"] == "interactive" else "tool", "content": item["message"]})
                    if item["response_json"]:
                        response = json.loads(item["response_json"])
                        for choice in response.get("choices", []):
                            authored = choice.get("message", {})
                            if authored.get("content"):
                                messages.append({"role": "assistant", "content": authored["content"]})
                packet = {"model": config.model, "reasoning_effort": config.effort,
                          "messages": messages, "metadata": {
                            "tenant_id": tenant, "owner_id": owner, "conversation_id": conversation_id,
                            "agent_id": "coordinator", "message_id": turn_id, "idempotency_key": f"{conversation_id}:{turn_id}",
                            "surface": "coordinator",
                            "bootstrap_bundle": config.bootstrap_bundle,
                            "allowed_ai_origin_scope": self._conversation_scope(conversation),
                        }}
                serialized = canonical(packet)
                conn.execute("UPDATE coordinator_turns SET payload_json=? WHERE conversation_id=? AND turn_id=?", (serialized, conversation_id, turn_id))
            else:
                serialized = turn["payload_json"]
        try:
            self._assert_foreground_allowed_ai(conversation, config)
            record = self.provider.start(ChatCompletionRequest.model_validate_json(serialized), tenant_id=tenant)
        except Exception as exc:
            blocker = _safe_blocker_code(exc)
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT origin,retry_attempts FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                    (conversation_id, turn_id),
                ).fetchone()
                retryable_result = (
                    current is not None
                    and current["origin"] == "worker_results"
                    and blocker in {"host_capacity", "provider_account_busy"}
                )
                retryable_admission = (
                    current is not None
                    and blocker in _RETRYABLE_ADMISSION_CODES
                    and int(current["retry_attempts"] or 0) < 6
                )
                retryable = retryable_result or retryable_admission
                attempts = int(current["retry_attempts"] or 0) + 1 if retryable else 0
                retry_after = (
                    (datetime.now(UTC) + timedelta(seconds=min(60, 5 * (2 ** min(attempts - 1, 4))))).isoformat()
                    if retryable else ""
                )
                conn.execute(
                    "UPDATE coordinator_turns SET blocker=?,retry_after_at=?,retry_attempts=? "
                    "WHERE conversation_id=? AND turn_id=? AND request_id='' AND blocker<>'cancelled'",
                    (blocker, retry_after, attempts, conversation_id, turn_id),
                )
                settled = conn.execute(
                    "SELECT request_id,blocker FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                    (conversation_id, turn_id),
                ).fetchone()
                if settled and settled["blocker"] == "cancelled":
                    return {"turn_id": turn_id, "state": "cancelled"}
                if settled and settled["request_id"]:
                    return {"turn_id": turn_id, "request_id": settled["request_id"], "state": "accepted"}
            return {"turn_id": turn_id, "state": "blocked", "blocker": blocker}
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE coordinator_turns SET request_id=?,blocker='',retry_after_at='' "
                         "WHERE conversation_id=? AND turn_id=? AND request_id='' AND blocker<>'cancelled'",
                         (record["request_id"], conversation_id, turn_id))
            settled = conn.execute(
                "SELECT request_id,blocker FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
            if settled and settled["blocker"] == "cancelled":
                return {"turn_id": turn_id, "state": "cancelled"}
            if settled and settled["request_id"]:
                return {"turn_id": turn_id, "request_id": settled["request_id"], "state": record["state"]}
        return {"turn_id": turn_id, "request_id": record["request_id"], "state": record["state"]}

    def refresh(self, tenant: str, owner: str, conversation_id: str) -> dict:
        from .conversation_provider import ChatCompletionRequest

        conversation = self._conversation(tenant, owner, conversation_id)
        snapshot = self.snapshot(tenant, owner, conversation_id)
        if bool(conversation.get("restore_hold")):
            return snapshot
        for turn in snapshot["turns"]:
            if bool(turn.get("restore_hold")) or not turn["request_id"] or turn["response_json"]:
                continue
            record = self.store.get_provider_request(turn["request_id"])
            if not record:
                continue
            record = self.provider._sync(record)
            if record["state"] == "completed":
                run = self.store.get_run(record["run_id"])
                response = self.provider.response_payload(record, run, ChatCompletionRequest.model_validate_json(self._turn_payload(conversation_id, turn["turn_id"])))
                with self.store._connect() as conn:
                    conn.execute("UPDATE coordinator_turns SET response_json=?,blocker='' WHERE conversation_id=? AND turn_id=?", (canonical(response), conversation_id, turn["turn_id"]))
            elif record["state"] in {"failed", "cancelled"}:
                blocker = record["state"]
                if blocker == "failed" and record.get("run_id"):
                    run = self.store.get_run(record["run_id"])
                    if (run and run.get("failure_structured") == 1
                            and run.get("failure_class") == "native_input_expired"):
                        blocker = "native_input_expired"
                with self.store._connect() as conn:
                    conn.execute("UPDATE coordinator_turns SET blocker=? WHERE conversation_id=? AND turn_id=?", (blocker, conversation_id, turn["turn_id"]))
        for goal in snapshot["goals"]:
            if goal["state"] == "restore_held":
                continue
            if goal["run_id"] and goal["state"] in {"completed", "failed", "cancelled", "interrupted"}:
                receipt = {k: goal[k] for k in ("goal_id", "run_id", "state")}
                with self.store._connect() as conn:
                    conn.execute("INSERT OR IGNORE INTO coordinator_events(conversation_id,kind,identity,payload_json,created_at) VALUES(?,?,?,?,?)", (conversation_id, "result_available", goal["run_id"], canonical(receipt), now()))
        return self.snapshot(tenant, owner, conversation_id)

    def native_identity(self, principal: dict) -> tuple[str, str, str]:
        # PeerCollaboration already authenticates the live run and exact attempt. A peer
        # token alone is never delegation permission: require this provider-session role.
        session = self.store.get_provider_session_by_worker(principal["worker_id"])
        if not session or session.get("agent_id") != "coordinator":
            raise CoordinatorScopeError("Coordinator role required")
        tenant, owner = principal["tenant_id"], principal["owner_id"]
        conversation_id = session["conversation_id"]
        self._conversation(tenant, owner, conversation_id)
        if session["owner_id"] != owner or session["tenant_id"] != tenant:
            raise CoordinatorScopeError("Coordinator owner mismatch")
        return tenant, owner, conversation_id

    def cancel_turn(self, tenant: str, owner: str, conversation_id: str, turn_id: str) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        self._assert_not_restore_held(conversation)
        with self.store._connect() as conn:
            row = conn.execute("SELECT request_id FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone()
        if not row:
            raise CoordinatorConflict("Accepted turn unavailable")
        with self.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET blocker='cancelled' WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id))
        # Existing provider tombstone serializes against its start lock, including a
        # cancellation arriving before the native request record is attached here.
        return self.provider.cancel_by_idempotency(f"{conversation_id}:{turn_id}", owner, tenant_id=tenant)

    def _turn_payload(self, conversation_id: str, turn_id: str) -> str:
        with self.store._connect() as conn:
            return conn.execute("SELECT payload_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone()["payload_json"]

    def retry_result_turn(self, tenant: str, owner: str, conversation_id: str, source_turn_id: str) -> dict:
        """Retry one failed result handoff with its saved evidence, never its old request."""
        conversation = self._conversation(tenant, owner, conversation_id)
        self._assert_not_restore_held(conversation)
        retry_id = "results-retry-" + digest([conversation_id, source_turn_id])[:32]
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT rowid,* FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, source_turn_id),
            ).fetchone()
            if (not source or source["origin"] != "worker_results" or not source["request_id"]
                    or source["response_json"] or source["blocker"] not in {"failed", "cancelled", "native_input_expired"}):
                raise CoordinatorConflict("Failed result handoff unavailable")
            request = conn.execute(
                "SELECT state FROM provider_requests WHERE request_id=? AND tenant_id=? AND owner_id=?",
                (source["request_id"], tenant, owner),
            ).fetchone()
            if not request or request["state"] not in {"failed", "cancelled"}:
                raise CoordinatorConflict("Result handoff is not settled")
            if conn.execute(
                "SELECT 1 FROM coordinator_turns WHERE conversation_id=? AND rowid>? "
                "AND response_json<>'' LIMIT 1", (conversation_id, source["rowid"]),
            ).fetchone():
                raise CoordinatorConflict("A later reply already used these results")
            conn.execute(
                "INSERT OR IGNORE INTO coordinator_turns"
                "(conversation_id,turn_id,message,origin,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, retry_id, source["message"], "worker_results", now()),
            )
        return self.start_turn(tenant, owner, conversation_id, retry_id)

    def queue_result_turn(self, tenant: str, owner: str, conversation_id: str) -> str | None:
        conversation = self._conversation(tenant, owner, conversation_id)
        if bool(conversation.get("restore_hold")) or not CoordinatorConfig.model_validate_json(conversation["config_json"]).wake_on_results:
            return None
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]:
                return None
            # Foreground work already accepted always wins admission. A single transaction
            # claims all current result notices into one deterministic continuation.
            pending = conn.execute("""SELECT 1 FROM coordinator_turns t
                LEFT JOIN provider_requests p ON p.request_id=t.request_id
                WHERE t.conversation_id=? AND t.restore_hold=0 AND t.response_json='' AND t.blocker<>'cancelled' AND
                (t.request_id='' OR p.request_id IS NULL OR p.state NOT IN ('failed','cancelled','completed')) LIMIT 1""", (conversation_id,)).fetchone()
            if pending:
                return None
            # A result wake uses the same configured assistant account as its helpers.
            # Do not let an early result occupy that account while another dispatched
            # helper is still queued or running. Keep its event durable for one joined
            # continuation after the outstanding helpers settle.
            active_helpers = conn.execute("""SELECT 1 FROM coordinator_goals g
                JOIN runs r ON r.run_id=g.run_id
                WHERE g.conversation_id=? AND g.restore_hold=0
                AND r.state IN ('queued','running') LIMIT 1""", (conversation_id,)).fetchone()
            if active_helpers:
                return None
            events = conn.execute("SELECT * FROM coordinator_events WHERE conversation_id=? AND delivered_turn_id='' ORDER BY sequence", (conversation_id,)).fetchall()
            if not events:
                return None
            # A result from this conversation's foreground native run is
            # already visible in its reply. Waking that run creates a duplicate.
            foreground_runs = {
                row["run_id"] for row in conn.execute(
                    "SELECT p.run_id FROM provider_requests p "
                    "JOIN coordinator_turns t ON t.request_id=p.request_id "
                    "WHERE t.conversation_id=? AND p.run_id IS NOT NULL",
                    (conversation_id,),
                )
            }
            redundant = [e for e in events if e["kind"] == "result_available" and e["identity"] in foreground_runs]
            if redundant:
                conn.executemany(
                    "UPDATE coordinator_events SET delivered_turn_id='foreground' "
                    "WHERE sequence=? AND delivered_turn_id=''",
                    [(e["sequence"],) for e in redundant],
                )
                events = [e for e in events if e not in redundant]
            if not events:
                return None
            turn_id = "results-" + digest([e["sequence"] for e in events])[:32]
            # Carry the exact saved result with the notification when it fits.
            # Larger results remain available through the paged coordinator tool;
            # the explicit offset/hash tells the model what it has not read yet.
            inline_remaining = 48000
            items = []
            for event in events:
                item = json.loads(event["payload_json"])
                if event["kind"] == "result_available" and item.get("goal_id") and item.get("run_id"):
                    run = conn.execute(
                        "SELECT r.output_text,r.failure_class FROM coordinator_goals g "
                        "JOIN runs r ON r.run_id=g.run_id "
                        "JOIN workers w ON w.worker_id=r.worker_id "
                        "WHERE g.conversation_id=? AND g.goal_id=? AND g.run_id=? "
                        "AND r.tenant_id=? AND w.tenant_id=? AND w.owner_id=?",
                        (conversation_id, item["goal_id"], item["run_id"], tenant, tenant, owner),
                    ).fetchone()
                    if run:
                        output = str(run["output_text"] or "")
                        length = min(len(output), 12000, inline_remaining)
                        item["result"] = {
                            "output_text": output[:length], "total_chars": len(output),
                            "next_offset": length if length < len(output) else None,
                            "sha256": hashlib.sha256(output.encode()).hexdigest(),
                            "failure_class": str(run["failure_class"] or ""),
                        }
                        inline_remaining -= length
                items.append(item)
            evidence = {"kind": "worker_result_notifications", "items": items}
            conn.execute("INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,created_at) VALUES(?,?,?,?,?)", (conversation_id, turn_id, canonical(evidence), "worker_results", now()))
            conn.executemany("UPDATE coordinator_events SET delivered_turn_id=? WHERE sequence=? AND delivered_turn_id=''", [(turn_id, e["sequence"]) for e in events])
            return turn_id

    def resume_restored(
        self,
        tenant: str,
        owner: str,
        conversation_id: str,
        expected_hold_set_hash: str,
        approved_request_ids: list[str] | None = None,
        approved_turn_ids: list[str] | None = None,
        approved_goal_ids: list[str] | None = None,
    ) -> dict:
        """Release only an owner-approved exact subset of one continuity hold.

        The durable set hash prevents a stale UI or retry from authorizing a changed
        restore set. Partial approval leaves the remaining rows inert and keeps the
        conversation hold active.
        """

        conversation = self._conversation(tenant, owner, conversation_id)
        expected = str(expected_hold_set_hash or "").strip()
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected.lower()):
            raise CoordinatorConflict("Invalid continuity restore hold receipt")
        requested = {
            "request": {str(value) for value in (approved_request_ids or []) if str(value)},
            "turn": {str(value) for value in (approved_turn_ids or []) if str(value)},
            "goal": {str(value) for value in (approved_goal_ids or []) if str(value)},
        }
        approved = {f"{kind}:{value}" for kind, values in requested.items() for value in values}
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT restore_hold FROM coordinator_conversations "
                "WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (conversation_id, tenant, owner),
            ).fetchone()
            if current_row is None:
                raise CoordinatorScopeError("Conversation unavailable")
            current = self._restore_hold_items(conn, conversation_id)
            actual = self._hold_digest(current)
            if actual != expected:
                raise CoordinatorConflict("Continuity restore hold changed; refresh the receipt")
            if not approved.issubset(set(current)):
                raise CoordinatorConflict("Continuity resume contains an unknown pending item")
            if approved:
                request_ids = sorted(requested["request"])
                if request_ids:
                    placeholders = ",".join("?" for _ in request_ids)
                    conn.execute(
                        f"UPDATE provider_requests SET restore_hold=0, restore_hold_set_hash='' "
                        f"WHERE request_id IN ({placeholders}) AND restore_hold=1 AND session_id IN "
                        "(SELECT session_id FROM provider_sessions WHERE conversation_id=?)",
                        [*request_ids, conversation_id],
                    )
                turn_ids = sorted(requested["turn"])
                if turn_ids:
                    placeholders = ",".join("?" for _ in turn_ids)
                    conn.execute(
                        f"UPDATE coordinator_turns SET restore_hold=0 "
                        f"WHERE conversation_id=? AND turn_id IN ({placeholders}) AND restore_hold=1",
                        [conversation_id, *turn_ids],
                    )
                goal_ids = sorted(requested["goal"])
                if goal_ids:
                    placeholders = ",".join("?" for _ in goal_ids)
                    conn.execute(
                        f"UPDATE coordinator_goals SET restore_hold=0 "
                        f"WHERE conversation_id=? AND goal_id IN ({placeholders}) AND restore_hold=1",
                        [conversation_id, *goal_ids],
                    )
            remaining = self._restore_hold_items(conn, conversation_id)
            remaining_hash = self._hold_digest(remaining)
            held = bool(remaining)
            conn.execute(
                "UPDATE coordinator_conversations SET restore_hold=?, restore_hold_set_hash=? "
                "WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (int(held), remaining_hash if held else "", conversation_id, tenant, owner),
            )
            conn.execute(
                "UPDATE provider_requests SET restore_hold_set_hash=? WHERE restore_hold=1 AND session_id IN "
                "(SELECT session_id FROM provider_sessions WHERE conversation_id=?)",
                (remaining_hash if held else "", conversation_id),
            )
            conn.execute(
                "UPDATE coordinator_turns SET restore_hold=1 WHERE conversation_id=? AND restore_hold=1",
                (conversation_id,),
            )
        return self.snapshot(tenant, owner, conversation_id)

    def reconcile_once(self, limit: int = 100) -> list[dict]:
        """Call from the existing maintenance tick; no independent thread or scheduler.

        Worker reservation recovery and execution remain entirely owned by service/store.
        Native provider idempotency/active-session fencing owns foreground admission.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid reconciliation limit")
        with self.store._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM coordinator_conversations WHERE conversation_id>? ORDER BY conversation_id LIMIT ?", (self._reconcile_after, limit))]
            if not rows:
                self._reconcile_after = ""
                rows = [dict(r) for r in conn.execute("SELECT * FROM coordinator_conversations ORDER BY conversation_id LIMIT ?", (limit,))]
        outcomes = []
        for conversation in rows:
            self._reconcile_after = conversation["conversation_id"]
            scope = conversation["tenant_id"], conversation["owner_id"], conversation["conversation_id"]
            if bool(conversation.get("restore_hold")):
                continue
            try:
                self.recover_dispatches(*scope)
                self.recover_retry_actions(*scope)
                self.refresh(*scope)
                with self.store._connect() as conn:
                    pending = conn.execute(
                        "SELECT turn_id FROM coordinator_turns WHERE conversation_id=? AND request_id='' "
                        "AND (blocker='' OR (origin='worker_results' "
                        "AND blocker IN ('host_capacity','provider_account_busy') "
                        "AND (retry_after_at='' OR retry_after_at<=?)) "
                        "OR (blocker IN ('shared_capacity_busy','shared_resource_authority_unavailable',"
                        "'host_capacity','provider_account_busy') "
                        "AND retry_after_at<>'' AND retry_after_at<=?)) "
                        "ORDER BY CASE origin WHEN 'interactive' THEN 0 ELSE 1 END,created_at LIMIT 1",
                        (scope[2], now(), now()),
                    ).fetchone()
                turn_id = pending["turn_id"] if pending else self.queue_result_turn(*scope)
                if turn_id:
                    outcomes.append(self.start_turn(*scope, turn_id))
            except Exception as exc:
                outcomes.append({"conversation_id": scope[2], "state": "blocked", "blocker": type(exc).__name__})
        return outcomes

    def recover_retry_actions(self, tenant: str, owner: str, conversation_id: str) -> None:
        """Finish a recorded Retry after a process exits before goal rebinding."""

        self._conversation(tenant, owner, conversation_id)
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT goal_id,payload_json FROM coordinator_actions "
                "WHERE conversation_id=? AND response_json='' ORDER BY rowid LIMIT 50",
                (conversation_id,),
            ).fetchall()
        for row in rows:
            try:
                control = Control.model_validate_json(row["payload_json"])
            except ValueError:
                continue
            if control.action != "retry":
                continue
            try:
                self.control(tenant, owner, conversation_id, row["goal_id"], control)
            except (CoordinatorConflict, CoordinatorScopeError, RuntimeError):
                # The exact action remains pending. A later retry can recover it
                # after its typed prerequisite or sibling contention clears.
                continue

    def recover_dispatches(self, tenant: str, owner: str, conversation_id: str) -> list[dict]:
        conversation = self._conversation(tenant, owner, conversation_id)
        if bool(conversation.get("restore_hold")):
            return []
        with self.store._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM coordinator_goals WHERE conversation_id=? AND restore_hold=0 AND dispatch_json<>'' AND run_id=''", (conversation_id,))]
        results = []
        for goal in rows:
            if goal["intent_state"] != "cancelled":
                results.append(self.dispatch(tenant, owner, conversation_id, Dispatch.model_validate_json(goal["dispatch_json"])))
                continue
            # Stop can win between the service reservation and goal attachment. Recover
            # only an existing reservation; never create new work for a stopped intent.
            reservation = self.store.get_delegation_by_idempotency_key(
                tenant_id=tenant, owner_id=owner, idempotency_key=f"{conversation_id}:{goal['goal_id']}")
            if reservation:
                with self.store._connect() as conn:
                    conn.execute("UPDATE coordinator_goals SET work_ref=?,worker_id=?,run_id=? WHERE conversation_id=? AND goal_id=?", (reservation["work_ref"],reservation["worker_id"],reservation["initial_run_id"],conversation_id,goal["goal_id"]))
                self.service.cancel_run(reservation["worker_id"],reservation["initial_run_id"])
                results.append(self.goal_snapshot(tenant,owner,conversation_id,goal["goal_id"]))
        return results

    def projection_for(self, worker: dict, peer_projection: dict, endpoint: str) -> dict | None:
        """Service run binder calls this after the shared peer token has been minted."""
        session = self.store.get_provider_session_by_worker(str(worker["worker_id"]))
        if not session or session.get("agent_id") != "coordinator":
            return None
        self._conversation(session["tenant_id"], session["owner_id"], session["conversation_id"])
        if peer_projection.get("worker_id") != worker["worker_id"] or peer_projection.get("run_id") != worker.get("_active_run_id"):
            raise CoordinatorScopeError("Native projection targets a different run")
        return {**peer_projection, "url": endpoint}

    def bind_native_worker(self, worker: dict, run: dict, peers: Any, endpoint: str) -> dict:
        session = self.store.get_provider_session_by_worker(str(worker["worker_id"]))
        if not session or session.get("agent_id") != "coordinator":
            return worker
        self._conversation(session["tenant_id"], session["owner_id"], session["conversation_id"])
        from urllib.parse import urlparse
        from . import native_transport
        socket_base = native_transport.native_base()
        if socket_base:
            endpoint = socket_base + "/v1/native/coordinator/"
        elif native_transport.packaged_profile():
            # A packaged box reaches the runtime only through its own socket.
            raise CoordinatorScopeError("Coordinator native endpoint unavailable")
        else:
            parsed = urlparse(endpoint)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or parsed.path != "/v1/native/coordinator/"):
                raise CoordinatorScopeError("Coordinator native endpoint invalid")
            if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1", "host.docker.internal"}:
                raise CoordinatorScopeError("Coordinator native endpoint requires TLS")
        token = (worker.get("_peer_native_projection") or {}).get("token")
        if not token:
            # Same O06 binder; coordinator authority does not require peer discovery on.
            token = peers.mint_native_session(worker["worker_id"], run["run_id"])
        projection = {"worker_id": worker["worker_id"], "run_id": run["run_id"], "token": token, "url": endpoint}
        if socket_base:
            projection["transport"] = "stdio"
        return {**worker, "_active_run_id": run["run_id"], "_coordinator_native_projection": projection}


    def handle_goals(self, tenant: str, owner: str, conversation_id: str, goal_ids: list[str], worker_id: str, run_id: str) -> list[dict]:
        self._conversation(tenant, owner, conversation_id)
        session = self.store.get_provider_session_by_worker(worker_id)
        run = self.store.get_run(run_id)
        if not session or session["conversation_id"] != conversation_id or session["tenant_id"] != tenant or session["owner_id"] != owner or not run or run["worker_id"] != worker_id or run["state"] != "running":
            raise CoordinatorScopeError("Current coordinator run required")
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for goal_id in goal_ids:
                row = conn.execute("SELECT * FROM coordinator_goals WHERE conversation_id=? AND goal_id=?", (conversation_id, goal_id)).fetchone()
                if not row or row["intent_state"] == "cancelled" or row["dispatch_json"] or (row["run_id"] and row["run_id"] != run_id):
                    raise CoordinatorConflict("Goal is unavailable for this conversation run")
            conn.executemany("UPDATE coordinator_goals SET worker_id=?,run_id=?,blocker='' WHERE conversation_id=? AND goal_id=?", [(worker_id,run_id,conversation_id,goal_id) for goal_id in goal_ids])
        return [self.goal_snapshot(tenant, owner, conversation_id, goal_id) for goal_id in goal_ids]


    @staticmethod
    def _goal_summary(goal: dict) -> dict:
        return {**{k: v for k, v in goal.items() if k not in {"output_text", "error_text"}},
                "has_result": bool(goal.get("output_text") or goal.get("error_text"))}

    def read_result(self, tenant: str, owner: str, conversation_id: str, goal_id: str,
                    offset: int = 0, max_chars: int = 12000) -> dict:
        if offset < 0 or not 1 <= max_chars <= 64000:
            raise CoordinatorConflict("Invalid result page")
        goal = self.goal_snapshot(tenant, owner, conversation_id, goal_id)
        text = goal.get("output_text", "")
        if offset > len(text):
            raise CoordinatorConflict("Result offset exceeds source")
        end = min(len(text), offset + max_chars)
        return {"goal_id": goal_id, "run_id": goal["run_id"], "state": goal["state"],
                "output_text": text[offset:end], "total_chars": len(text), "offset": offset,
                "next_offset": end if end < len(text) else None,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "failure_class": goal.get("failure_class", "")}


def guard_coordinator_run(conn: Any, run_id: str) -> None:
    """Call in the existing run-admission transaction beside the peer grant fence.

    A reservation may be accepted concurrently with Stop before the goal has its run
    reference. The delegation idempotency key binds the intent without that attachment.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_goals'").fetchone():
        return
    stopped = conn.execute("""SELECT 1 FROM coordinator_goals g JOIN delegations d
        ON d.idempotency_key=g.conversation_id || ':' || g.goal_id
        JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id
        AND c.tenant_id=d.tenant_id AND c.owner_id=d.owner_id
        WHERE d.initial_run_id=? AND (g.intent_state='cancelled' OR g.restore_hold=1 OR c.restore_hold=1) LIMIT 1""", (run_id,)).fetchone()
    if stopped:
        raise CoordinatorConflict("Coordinator goal is held or stopped before execution")
