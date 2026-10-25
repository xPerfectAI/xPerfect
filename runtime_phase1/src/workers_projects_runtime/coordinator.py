"""Standalone conversation coordination over existing native sessions and durable runs.

This module never executes a harness or schedules worker runs. Its ledger records accepted
user intent and links it to the service's authoritative delegation/run records.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .failure_classification import is_user_resumable_failure
from .models import CLOSED_WORKER_STATES, utc_now


class CoordinatorConflict(ValueError):
    pass


class CoordinatorScopeError(PermissionError):
    pass


class CoordinatorWorkerBlocked(CoordinatorConflict):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


COORDINATOR_SCHEMA_VERSION = 4


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
    library_text_chars: int = Field(default=24000, ge=0, le=262144, strict=True)
    scope: CoordinatorScope = Field(default_factory=CoordinatorScope)


class Goal(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=262144)


class Dispatch(StrictModel):
    goal_id: str
    route_id: str = Field(default="", max_length=100)
    worker_id: str = Field(default="", max_length=512)
    instruction: str = Field(min_length=1, max_length=524288)

    def model_post_init(self, __context: Any) -> None:
        if bool(self.route_id) == bool(self.worker_id):
            raise ValueError("Choose exactly one route_id or worker_id")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.worker_id):
            raise ValueError("Worker identity must be printable")

    def request_payload(self) -> dict:
        # Old route receipts and reservation digests must stay byte-exact.
        return self.model_dump(exclude_defaults=True)


class Control(StrictModel):
    action: Literal["queue", "message", "pause", "resume", "stop", "steer", "retry", "dismiss"]
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
_SAFE_FOREGROUND_FAILURE_CODES = frozenset({
    "native_input_expired", "model_configuration_required", "provider_auth_missing",
    "provider_connected_account_reconnect_required", "provider_unauthorized",
})
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
    tools = source.with_name("coordinator-tools.json").read_bytes()
    return {"id": "xperfect.coordinator", "version": 3,
            "source": "workers_projects_runtime/prompts/coordinator.md",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "tool_schema_version": json.loads(tools)["version"],
            "tool_schema_sha256": hashlib.sha256(tools).hexdigest(), "content": content}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS coordinator_conversations (
 conversation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 config_json TEXT NOT NULL, prompt_json TEXT NOT NULL, created_at TEXT NOT NULL,
 scope_json TEXT NOT NULL DEFAULT '{}',
 library_json TEXT NOT NULL DEFAULT '{}',
 restore_hold INTEGER NOT NULL DEFAULT 0, restore_hold_set_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS coordinator_turns (
 conversation_id TEXT NOT NULL REFERENCES coordinator_conversations(conversation_id),
 turn_id TEXT NOT NULL, message TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
 blocker TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '',
 response_json TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'interactive', created_at TEXT NOT NULL,
 retry_after_at TEXT NOT NULL DEFAULT '', retry_attempts INTEGER NOT NULL DEFAULT 0,
 library_json TEXT NOT NULL DEFAULT '{}',
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
                ("coordinator_conversations", "library_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("coordinator_conversations", "restore_hold", "INTEGER NOT NULL DEFAULT 0"),
                ("coordinator_conversations", "restore_hold_set_hash", "TEXT NOT NULL DEFAULT ''"),
                ("coordinator_turns", "restore_hold", "INTEGER NOT NULL DEFAULT 0"),
                ("coordinator_turns", "retry_after_at", "TEXT NOT NULL DEFAULT ''"),
                ("coordinator_turns", "retry_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("coordinator_turns", "library_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("coordinator_goals", "restore_hold", "INTEGER NOT NULL DEFAULT 0"),
            ):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_coordinator_goal_worker_run ON coordinator_goals(worker_id,run_id)")
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
        tenant = str(conversation.get("tenant_id") or "local")
        owner = str(conversation.get("owner_id") or "")
        model = self.provider._model(config.model, tenant_id=tenant, owner_id=owner)
        worker = {
            "tenant_id": tenant,
            "owner_id": owner,
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
        # Existing registry remains the authority for exact foreground model and effort,
        # including a model this owner chose in Connections.
        model = self.provider._model(config.model, tenant_id=tenant, owner_id=owner)
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

    @staticmethod
    def _library_summary(selection: dict) -> dict:
        """Owner-visible selection metadata, never native source-path authority or bytes."""
        items = {}
        for item in [*selection.get("selected_items", []), *selection.get("items", [])]:
            items.setdefault((item.get("item_id"), item.get("version_id")), item)
        def metadata(item, keys):
            result = {key: item[key] for key in keys if key in item}
            source_uri = (item.get("payload") or {}).get("source_uri")
            if source_uri:
                result["source_uri"] = source_uri
            return result
        return {
            "refs": selection.get("refs", []),
            "items": [metadata(item, ("item_id", "version_id", "name", "kind", "version", "state", "status", "source_uri"))
                      for item in items.values()],
            "pending": [metadata(item, ("item_id", "version_id", "name", "kind", "code", "state", "status", "reason", "profile", "source_uri"))
                        for item in selection.get("pending", selection.get("requirements", []))],
        }

    def _library_owner(self):
        library = getattr(self.service, "personal_library", None)
        if library is None:
            raise CoordinatorConflict("Personal Library is unavailable")
        return library

    @staticmethod
    def _library_refs(refs) -> list[dict]:
        from .personal_library import LibraryRef
        return [LibraryRef.model_validate(ref).model_dump() for ref in refs]

    def _goal_library(self, conversation_id: str, goal: dict) -> dict:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT library_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, goal["source_turn_id"]),
            ).fetchone()
        return json.loads(str(row["library_json"] or "{}")) if row else {}

    def _bind_goal_library(self, tenant: str, owner: str, selection: dict, reservation: dict) -> None:
        if not selection.get("upload_ids"):
            return
        # Use the same accepted upload/version inputs as ordinary launches. This
        # precedes goal attachment/start; reservation recovery retries the bind.
        from .native_model_selection import native_timing_event
        started, status = time.monotonic_ns(), "failed"
        try:
            self.service.files.bind(
                reservation["worker_id"], tenant, owner, selection["upload_ids"],
                "coordinator-library-" + digest({"run_id": reservation["initial_run_id"], "refs": selection["refs"]}),
                revisions=selection["upload_revisions"],
            )
            status = "completed"
        finally:
            native_timing_event("coordinator_library_bind", worker=reservation["worker_id"], run=reservation["initial_run_id"],
                elapsedMs=(time.monotonic_ns()-started)/1_000_000, status=status, files=len(selection["upload_ids"]))

    def _library_packet(self, tenant: str, owner: str, selection: dict, config: CoordinatorConfig) -> dict:
        if not selection.get("refs"):
            return {}
        started = time.monotonic_ns()
        from .conversation_provider import DIRECT_PROFILES
        model = self.provider._model(config.model, tenant_id=tenant, owner_id=owner)
        direct = getattr(model, "harness_profile", "") in DIRECT_PROFILES
        context = self._library_owner().knowledge_context(
            tenant, owner, selection["refs"], max_chars=config.library_text_chars if direct else 0, resolved=selection,
        )
        source_data = []
        for source in context.get("sources", []):
            descriptor = {key: source[key] for key in ("item_id", "version_id", "name", "upload_id", "sha256") if key in source}
            if direct:
                text = source.get("text", source.get("content"))
                loaded = source.get("loaded") is True and isinstance(text, str)
                descriptor.update(loaded=loaded, reason="loaded" if loaded else str(source.get("reason") or "not_loaded"))
                if loaded:
                    descriptor["text"] = text
                else:
                    descriptor["status"] = "Not loaded for this Queen; available to Helpers"
            else:
                descriptor.update(delivery="native_file", status="Provided to native Files; read when needed")
            source_data.append(descriptor)
        summary = self._library_summary(selection)
        bundle = dict(config.bootstrap_bundle)
        bundle["library_selection"] = summary | {
            "upload_ids": selection.get("upload_ids", []),
            "upload_revisions": selection.get("upload_revisions", []),
            "context": [{key: value for key, value in source.items() if key != "text"} for source in source_data],
        }
        # Native files must bind through the real foreground worker before its
        # first start. Raw bootstrap entries have no worker projection receipt.
        from .native_model_selection import native_timing_event
        native_timing_event("coordinator_library_context", elapsedMs=(time.monotonic_ns()-started)/1_000_000,
            mode="direct" if direct else "native", selectedItems=len(selection["refs"]), files=len(source_data),
            includedChars=sum(len(source.get("text", "")) for source in source_data),
            notLoaded=sum(source.get("loaded") is False for source in source_data))
        # Reference data is appended after unchanged conversation history. It is
        # not a new objective, rewritten brief, or instruction layer.
        message = {
            "role": "tool", "content": canonical({
                "type": "selected_library_reference_material", "sources": source_data,
                "requirements_not_applied": summary["pending"],
            }),
        }
        bundle["library_selection"]["reference_message"] = {
            "id": "library-" + digest(message),
            "sha256": hashlib.sha256(message["content"].encode("utf-8")).hexdigest(),
        }
        return {"bootstrap_bundle": bundle, "message": message}

    def accept_turn(self, tenant: str, owner: str, conversation_id: str, turn_id: str,
                    message: str, goals: list[Goal] | None = None,
                    library_refs: list[dict] | None = None) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        self._assert_not_restore_held(conversation)
        if not turn_id or not message:
            raise CoordinatorConflict("Turn ID and message are required")
        goals = goals or []
        config = CoordinatorConfig.model_validate_json(conversation["config_json"])
        refs = self._library_refs(library_refs) if library_refs is not None else None
        with self.store._connect() as conn:
            accepted = conn.execute(
                "SELECT message,library_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
        resolved = None
        if accepted:
            previous = json.loads(str(accepted["library_json"] or "{}"))
            if accepted["message"] != message or (refs is not None and refs != previous.get("refs", [])):
                raise CoordinatorConflict("Turn idempotency key changed content")
        elif refs:
            library = self._library_owner()
            current = json.loads(str(conversation.get("library_json") or "{}"))
            resolved = library.compact_selection(current if refs == current.get("refs") else
                library.resolve_refs(tenant, owner, refs))
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]:
                raise CoordinatorConflict("Conversation is held for exact continuity resume")
            old = conn.execute("SELECT message,library_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (conversation_id, turn_id)).fetchone()
            if old and old["message"] != message:
                raise CoordinatorConflict("Turn idempotency key changed content")
            if old:
                if refs is not None and refs != json.loads(str(old["library_json"] or "{}")).get("refs", []):
                    raise CoordinatorConflict("Turn idempotency key changed Library selection")
            else:
                current = conn.execute(
                    "SELECT library_json FROM coordinator_conversations WHERE conversation_id=?", (conversation_id,),
                ).fetchone()
                selection = (resolved or {}) if refs is not None else json.loads(str(current["library_json"] or "{}"))
                if selection.get("refs"):
                    if refs is None:
                        selection = self._library_owner().compact_selection(selection)
                    self._library_owner().retain_refs(
                        tenant, owner, selection["refs"], holder_kind="coordinator_turn",
                        holder_id="coordinator-turn-" + digest({"conversation_id": conversation_id, "turn_id": turn_id}),
                        connection=conn, resolved=selection,
                    )
                conn.execute(
                    "INSERT INTO coordinator_turns(conversation_id,turn_id,message,library_json,created_at) VALUES(?,?,?,?,?)",
                    (conversation_id, turn_id, message, canonical(selection), now()),
                )
                conn.execute("UPDATE coordinator_conversations SET library_json=? WHERE conversation_id=?", (canonical(selection), conversation_id))
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
        if dispatch.worker_id:
            return self._dispatch_existing_worker(tenant, owner, conversation_id, dispatch, origin_scope)
        config = CoordinatorConfig.model_validate_json(conversation["config_json"])
        route = next((r for r in config.routes if r.id == dispatch.route_id), None)
        if route is None:
            raise CoordinatorScopeError("Route outside coordinator authority")
        if route.connection_id:
            self._assert_owned_connection(tenant, owner, route.connection_id)
        goal = self._goal(conversation_id, dispatch.goal_id)
        selection = self._goal_library(conversation_id, goal)
        serialized = canonical(dispatch.request_payload())
        bundle = {**route.bootstrap_bundle,
                  "launch_authority": {"version": 1, "kind": "conversation_orchestrator", "execution_mode": route.execution_mode,
                    "worker_model": route.model, "worker_reasoning_effort": route.effort}}
        if selection.get("refs"):
            bundle["library_selection"] = self._library_summary(selection) | {
                "upload_ids": selection.get("upload_ids", []),
            }
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
        from .service import standalone_coordinator_host_enabled
        contained = packaged_linux()
        standalone_host = route.execution_mode == "host" and standalone_coordinator_host_enabled()
        if contained or standalone_host:
            # Standalone xPerfect uses its existing owner account/admission lane:
            # contained boxes when packaged, ordinary scoped host work locally.
            # An explicitly configured legacy client policy keeps its own grant.
            from .service import (
                _contains_parallel_forbidden_authority_key,
                _parallel_clean_room_rejected,
                _validate_parallel_clean_room_environment,
                _validate_parallel_clean_room_files,
                _validate_parallel_clean_room_mcp,
            )
            if (
                (contained and route.execution_mode != "docker")
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
                route.profile, route.execution_mode, bundle, tenant_id=tenant, owner_id=owner,
            )
            bundle.pop("launch_authority")
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
                    "dispatch": dispatch.request_payload(),
                    "route": route.model_dump(),
                    "goal": goal["text"],
                    "origin_scope": origin_scope,
                    **({"library_refs": selection["refs"]} if selection.get("refs") else {}),
                }),
                origin_ref=f"{conversation_id}:{goal['goal_id']}", title=goal["goal_id"], goal=goal["text"], instruction=dispatch.instruction,
                origin_surface="coordinator", worker_name=goal["goal_id"], worker_role="worker",
                profile=route.profile, execution_mode=route.execution_mode, resource_class=route.resource_class,
                bootstrap_bundle=bundle,
                origin_scope=origin_scope or None,
                trusted_coordinator_model=standalone_model,
                start_run=False,
                emit_callback=False)
            self._bind_goal_library(tenant, owner, selection, reservation)
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

    def _scoped_worker(self, tenant: str, owner: str, scope: dict, worker_id: str) -> dict:
        worker = self.store.get_worker(worker_id)
        if (not worker or not scope.get("project_id")
                or worker.get("tenant_id") != tenant or worker.get("owner_id") != owner
                or worker.get("project_id") != scope["project_id"]
                or (scope.get("workspace_id") and worker.get("workspace_id") != scope["workspace_id"])
                or worker.get("workspace_kind") == "ephemeral"
                or worker.get("catalog_state", "active") != "active"
                or worker.get("trusted_run_lane") == "conversation"
                or self.store.get_provider_session_by_worker(worker_id)):
            raise CoordinatorScopeError("Worker outside this conversation's available project scope")
        with self.store._connect() as conn:
            delegated = conn.execute("SELECT 1 FROM delegations WHERE worker_id=? LIMIT 1", (worker_id,)).fetchone()
        if delegated:
            raise CoordinatorScopeError("Worker outside this conversation's available project scope")
        if worker.get("state") in CLOSED_WORKER_STATES:
            raise CoordinatorWorkerBlocked("worker_unavailable")
        return worker

    def _dispatch_existing_worker(self, tenant: str, owner: str, conversation_id: str,
                                  dispatch: Dispatch, scope: dict) -> dict:
        worker = self._scoped_worker(tenant, owner, scope, dispatch.worker_id)
        goal = self._goal(conversation_id, dispatch.goal_id)
        selection = self._goal_library(conversation_id, goal)
        serialized = canonical(dispatch.request_payload())
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT g.*,c.restore_hold AS conversation_restore_hold FROM coordinator_goals g "
                "JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id "
                "WHERE g.conversation_id=? AND g.goal_id=?", (conversation_id, dispatch.goal_id),
            ).fetchone()
            if current["intent_state"] == "cancelled":
                raise CoordinatorConflict("Goal was stopped before dispatch")
            if current["restore_hold"] or current["conversation_restore_hold"]:
                raise CoordinatorConflict("Goal is held for exact continuity resume")
            if current["dispatch_json"] and current["dispatch_json"] != serialized:
                raise CoordinatorConflict("Goal already has a different dispatch; use exact run controls")
            if current["run_id"]:
                # Exact replay never restarts, rewinds a steer, or changes the saved worker.
                return self.goal_snapshot(tenant, owner, conversation_id, dispatch.goal_id)
            conn.execute("UPDATE coordinator_goals SET dispatch_json=?,worker_id=? WHERE conversation_id=? AND goal_id=?",
                         (serialized, dispatch.worker_id, conversation_id, dispatch.goal_id))
        try:
            from .mission_provider_accounts import mission_provider_account_selection
            account = mission_provider_account_selection(worker)
            if account is not None and self.service._retained_provider_account_problem(
                    account_id=account.account_id, tenant_id=tenant, owner_id=owner,
                    profile=worker["profile"]):
                raise CoordinatorWorkerBlocked("worker_account_reconnect_required")
            if self.service._unresolved_duplication_reapprovals(worker):
                raise CoordinatorWorkerBlocked("worker_review_required")
            run = self.service.assign_run(
                dispatch.worker_id, dispatch.instruction,
                idempotency_key=_worker_assignment_key(conversation_id, dispatch.goal_id),
                start_processor=False, resume_paused_worker=False,
            )
            reservation = {"worker_id": dispatch.worker_id, "initial_run_id": run["run_id"], "work_ref": ""}
            self._bind_goal_library(tenant, owner, selection, reservation)
        except Exception as exc:
            with self.store._connect() as conn:
                conn.execute("UPDATE coordinator_goals SET blocker=? WHERE conversation_id=? AND goal_id=?",
                             (_safe_blocker_code(exc), conversation_id, dispatch.goal_id))
            return self.goal_snapshot(tenant, owner, conversation_id, dispatch.goal_id)
        with self.store._connect() as conn:
            attached = conn.execute(
                "UPDATE coordinator_goals SET worker_id=?,run_id=?,blocker='' "
                "WHERE conversation_id=? AND goal_id=? AND run_id='' AND restore_hold=0 "
                "AND (SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?)=0",
                (dispatch.worker_id, run["run_id"], conversation_id, dispatch.goal_id, conversation_id),
            ).rowcount
        if attached:
            if self._goal(conversation_id, dispatch.goal_id)["intent_state"] == "cancelled":
                self.service.cancel_run(dispatch.worker_id, run["run_id"])
            else:
                self.service.start_assigned_run(dispatch.worker_id)
        return self.goal_snapshot(tenant, owner, conversation_id, dispatch.goal_id)

    def _available_workers(self, tenant: str, owner: str, conversation: dict) -> dict:
        # A bounded identity projection, never worker bootstrap, credentials or logs.
        scope = json.loads(str(conversation.get("scope_json") or "{}"))
        project_id = str(scope.get("project_id") or "")
        if not project_id or not self.store.get_project(project_id, tenant_id=tenant, owner_id=owner):
            return {"workers": [], "workers_truncated": False}
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT w.worker_id,w.name,w.role,w.profile,w.model,w.state FROM workers w "
                "WHERE w.project_id=? AND w.tenant_id=? AND w.owner_id=? "
                "AND w.state NOT IN ('terminating','termination_failed','terminated') "
                "AND w.catalog_state='active' "
                "AND w.trusted_run_lane<>'conversation' AND w.workspace_kind<>'ephemeral' "
                "AND NOT EXISTS (SELECT 1 FROM delegations d WHERE d.worker_id=w.worker_id) "
                "AND (?='' OR w.workspace_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM provider_sessions p WHERE p.worker_id=w.worker_id) "
                "ORDER BY w.name,w.worker_id LIMIT 101",
                (project_id, tenant, owner, scope.get("workspace_id", ""), scope.get("workspace_id", "")),
            ).fetchall()
        return {"workers": [dict(row) for row in rows[:100]], "workers_truncated": len(rows)>100}

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
                and bool(goal["work_ref"])
                and goal["intent_state"] != "cancelled"
                and is_user_resumable_failure(
                    failure_class=run.get("failure_class"),
                    retryable=run.get("failure_retryable"),
                    runtime_invoked_at=run.get("runtime_invoked_at", ...),
                    started_at=run.get("started_at", ...),
                )
            )
        if goal["work_ref"]:
            delegation = self.store.get_delegation(goal["work_ref"], tenant_id=tenant, owner_id=owner)
            result["actions"] = []
            action_mask = getattr(self.service, "_active_work_service_actions", None)
            state = getattr(self.service, "_active_work_service_state", None)
            if delegation and delegation.get("worker_id") == goal["worker_id"]:
                result["dismissed"] = bool(delegation.get("dismissed_at"))
            if (delegation and delegation.get("worker_id") == goal["worker_id"]
                    and callable(action_mask) and callable(state)):
                control_run_id = str(delegation.get("run_id") or delegation.get("current_run_id") or "")
                with self.store._connect() as conn:
                    owned_control = self._owned_goal_result_run(
                        conn, tenant, owner, conversation_id, goal_id, control_run_id,
                    )
                if owned_control:
                    result["control_run_id"] = control_run_id
                    result["control_state"] = state(delegation)
                    actions = action_mask(delegation, state(delegation))
                    if goal["intent_state"] == "cancelled":
                        actions = actions & {"stop", "dismiss"}
                    result["actions"] = sorted(actions)
        return result

    def snapshot(self, tenant: str, owner: str, conversation_id: str) -> dict:
        conversation = self._conversation(tenant, owner, conversation_id)
        with self.store._connect() as conn:
            goals = conn.execute("SELECT goal_id FROM coordinator_goals WHERE conversation_id=? ORDER BY created_at,goal_id", (conversation_id,)).fetchall()
            turns = [dict(r) for r in conn.execute(
                "SELECT conversation_id,turn_id,message,origin,request_id,blocker,response_json,created_at,restore_hold,retry_after_at,retry_attempts "
                "FROM coordinator_turns WHERE conversation_id=? ORDER BY created_at,turn_id", (conversation_id,))]
            prepared_library = conn.execute(
                "SELECT json_extract(payload_json,'$.metadata.bootstrap_bundle.library_selection.refs') AS refs, "
                "json_extract(payload_json,'$.metadata.bootstrap_bundle.library_selection.context') AS context "
                "FROM coordinator_turns WHERE conversation_id=? AND payload_json<>'' ORDER BY created_at DESC,turn_id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
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
        selected_library = self._library_summary(json.loads(str(conversation.get("library_json") or "{}")))
        selected_library["context"] = []
        if prepared_library and json.loads(prepared_library["refs"] or "[]") == selected_library["refs"]:
            selected_library["context"] = json.loads(prepared_library["context"] or "[]")
        for turn in turns:
            if turn["response_json"]:
                try:
                    response = json.loads(turn["response_json"])
                except (TypeError, ValueError):
                    continue
                turn["response_json"] = canonical(project_coordinator_response(
                    self.service, tenant, owner, str(turn["request_id"]), response))
        return {"conversation_id": conversation_id, "model": config.model, "effort": config.effort,
                **self._available_workers(tenant, owner, conversation),
                "max_goals": config.max_goals, "routes": [{k: v for k, v in r.model_dump().items() if k != "bootstrap_bundle"} for r in config.routes],
                # Status and results remain readable after a scoped origin is
                # removed. New starts and dispatch still validate the origin.
                "scope": json.loads(str(conversation.get("scope_json") or "{}")),
                "context_manifest": config.context_manifest,
                "library": selected_library,
                "library_text_chars": config.library_text_chars,
                "prompt": {k: v for k, v in json.loads(conversation["prompt_json"]).items() if k != "content"},
                "restore_hold": bool(conversation.get("restore_hold")),
                "restore_hold_set_hash": str(conversation.get("restore_hold_set_hash") or ""),
                "turns": [{k: v for k, v in t.items() if k not in {"payload_json", "library_json"}} for t in turns],
                "foreground_native_runs": foreground_native_runs,
                "goals": [self._goal_summary(self.goal_snapshot(tenant, owner, conversation_id, r["goal_id"])) for r in goals]}

    def control(self, tenant: str, owner: str, conversation_id: str, goal_id: str, control: Control) -> dict:
        self._assert_not_restore_held(self._conversation(tenant, owner, conversation_id))
        goal = self.goal_snapshot(tenant, owner, conversation_id, goal_id)
        if control.action in {"queue", "message", "steer"} and not control.message.strip():
            raise CoordinatorConflict("Guidance is required for this action")
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
            if not previous and control.action in {"queue", "message", "dismiss"} and control.action not in goal.get("actions", []):
                raise CoordinatorConflict("This action is unavailable for the exact work")
            if control.run_id != goal.get("control_run_id", held["run_id"]) and not previous:
                raise CoordinatorConflict("Control targets a stale or unrelated run")
            if held["intent_state"] == "cancelled" and not previous and control.action not in {"dismiss", "stop"}:
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
                    (conversation_id, goal_id, held["run_id"]),
                )
            conn.execute("INSERT OR IGNORE INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json) VALUES(?,?,?,?)", (conversation_id, goal_id, control.idempotency_key, payload))
        if control.action == "retry":
            return self._retry_goal(tenant, owner, conversation_id, goal_id, control)
        if control.action in {"queue", "message", "dismiss"}:
            return self._delegated_goal_control(tenant, owner, conversation_id, goal_id, control)
        worker_id = goal["worker_id"]
        stop_outcome = None
        if control.run_id:
            if control.action == "stop":
                # The exact work-stop lifecycle also handles paused and
                # prelaunch-reserved runs. cancel_run leaves paused runs live.
                dispatch_receipt = json.loads(self._goal(conversation_id, goal_id)["dispatch_json"] or "{}")
                if dispatch_receipt.get("worker_id"):
                    self.service.cancel_run(worker_id, control.run_id)
                    stopped_run = self.store.get_run(control.run_id) or {}
                    stop_outcome = {"accepted": True, "confirmation_pending": stopped_run.get("state")
                                    not in {"completed", "failed", "cancelled", "interrupted"}}
                else:
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
        if stop_outcome is not None:
            stopped = self.store.get_run(control.run_id)
            terminal = bool(stopped and stopped["state"] in {"completed", "failed", "cancelled", "interrupted"})
            if not stop_outcome.get("accepted") and not terminal:
                raise CoordinatorConflict("Exact Stop was not accepted; retry this work")
            if terminal and not stop_outcome.get("confirmation_pending"):
                # These queued effects belong to this stopped goal, not another mission.
                # Use existing exact pending-run cancellation; keep every result/history row.
                with self.store._connect() as conn:
                    effects = conn.execute(
                        "SELECT json_extract(response_json,'$.run_id') FROM coordinator_actions "
                        "WHERE conversation_id=? AND goal_id=? AND json_valid(response_json) "
                        "AND json_valid(payload_json) AND json_extract(payload_json,'$.action') IN ('queue','message')",
                        (conversation_id, goal_id),
                    ).fetchall()
                    pending = [run_id for run_id in dict.fromkeys([goal["run_id"], *(row[0] for row in effects)])
                               if run_id and (owned := self._owned_goal_result_run(conn, tenant, owner, conversation_id, goal_id, run_id))
                               and owned["state"] == "queued"]
                for run_id in pending:
                    self.service.cancel_run(worker_id, run_id)
        response = self.goal_snapshot(tenant, owner, conversation_id, goal_id)
        if stop_outcome is not None:
            if stop_outcome.get("confirmation_pending") or not terminal:
                # A pending stop is not a completed idempotent response. The
                # same key must be able to retry after exact termination proof.
                return {**response, "confirmation_pending": True}
        with self.store._connect() as conn:
            conn.execute("UPDATE coordinator_actions SET response_json=? WHERE conversation_id=? AND goal_id=? AND idempotency_key=?", (canonical(response), conversation_id, goal_id, control.idempotency_key))
        return response

    def _delegated_goal_control(self, tenant: str, owner: str, conversation_id: str,
                                goal_id: str, control: Control) -> dict:
        goal = self._goal(conversation_id, goal_id)
        delegation = self.store.get_delegation(goal["work_ref"], tenant_id=tenant, owner_id=owner)
        if not delegation or delegation.get("worker_id") != goal["worker_id"]:
            raise CoordinatorScopeError("Bound work unavailable")
        effect_key = f"coordinator-{control.action}:{conversation_id}:{goal_id}:{control.idempotency_key}"
        replacement = None
        if control.action in {"queue", "message"}:
            replacement_id = self.service.active_work_effect_run_id(delegation, idempotency_key=effect_key)
            replacement = self.store.get_run(replacement_id)
        if replacement is None:
            try:
                outcome = self.service.execute_active_work_action(
                    delegation, action=control.action, instruction=control.message,
                    idempotency_key=effect_key, expected_run_id=control.run_id,
                    start_processor=False,
                )
            except (RuntimeError, ValueError) as exc:
                raise CoordinatorConflict("This action is unavailable for the exact work") from exc
            replacement_id = str(outcome.get("run_id") or control.run_id)
        else:
            if (replacement["state"] in {"completed", "failed", "cancelled", "interrupted"}
                    and not replacement.get("started_at") and goal["run_id"] != replacement_id):
                raise CoordinatorConflict("Reserved follow-up is terminal; refresh the exact work")
            outcome = {"status": replacement["state"] if replacement["state"] in {"completed", "failed", "cancelled", "interrupted"} else "queued",
                       "run_id": replacement_id,
                       "delivery_mode": "queued_next_boundary" if control.action == "message" else "queued"}
        if control.action in {"queue", "message"}:
            source = self.store.get_run(control.run_id)
            replacement = self.store.get_run(replacement_id)
            if (not source or not replacement or replacement_id == control.run_id
                    or replacement.get("worker_id") != goal["worker_id"]
                    or replacement.get("project_id") != source.get("project_id")
                    or replacement.get("tenant_id", "local") != tenant):
                raise CoordinatorConflict("Invalid follow-up receipt")
            self._bind_goal_replacement(tenant, owner, conversation_id, goal_id,
                                        goal, goal["run_id"], replacement_id, preserve_newer_effect=True)
        response = {**self.goal_snapshot(tenant, owner, conversation_id, goal_id),
                    "action": control.action, "status": str(outcome.get("status") or "accepted")}
        if control.action in {"queue", "message"}:
            # A later action may already be current. This receipt owns this exact effect,
            # so admission/history must never inherit the later goal snapshot's run ID.
            effect = self.store.get_run(replacement_id)
            response.update({"run_id": replacement_id, "state": effect["state"],
                             "output_text": effect.get("output_text", ""),
                             "error_text": effect.get("error_text", ""),
                             "failure_class": effect.get("failure_class", ""),
                             "active_attempt_id": effect.get("active_attempt_id", "")})
        if outcome.get("delivery_mode"):
            response["delivery_mode"] = outcome["delivery_mode"]
        with self.store._connect() as conn:
            conn.execute("UPDATE coordinator_actions SET response_json=? WHERE conversation_id=? AND goal_id=? AND idempotency_key=?",
                         (canonical(response), conversation_id, goal_id, control.idempotency_key))
        if control.action in {"queue", "message"}:
            # Successful effect authority must exist before waking native admission,
            # including a recovered predecessor that is no longer the latest result.
            self.service.start_assigned_run(goal["worker_id"])
        return response

    def _bind_goal_replacement(self, tenant: str, owner: str, conversation_id: str,
                               goal_id: str, goal: dict, source_run_id: str, replacement_id: str,
                               *, preserve_newer_effect: bool = False) -> None:
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if preserve_newer_effect:
                # Re-read attachment authority while holding the native transaction.
                # Concurrent or recovered follow-ups keep reservation order, including
                # the gap between another attachment and its successful receipt.
                current = conn.execute(
                    "SELECT current.first_queued_at,current.run_id,replacement.first_queued_at AS replacement_queued_at "
                    "FROM coordinator_goals g JOIN runs current ON current.run_id=g.run_id "
                    "JOIN runs replacement ON replacement.run_id=? "
                    "JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id "
                    "JOIN workers w ON w.worker_id=current.worker_id "
                    "WHERE g.conversation_id=? AND g.goal_id=? AND g.intent_state='accepted' "
                    "AND g.restore_hold=0 AND c.restore_hold=0 AND c.tenant_id=? AND c.owner_id=? "
                    "AND current.worker_id=? AND replacement.worker_id=current.worker_id "
                    "AND current.tenant_id=? AND replacement.tenant_id=current.tenant_id "
                    "AND w.tenant_id=current.tenant_id AND w.owner_id=?",
                    (replacement_id, conversation_id, goal_id, tenant, owner, goal["worker_id"], tenant, owner),
                ).fetchone()
                if current:
                    if current["run_id"] == replacement_id:
                        return
                    receipts = conn.execute(
                        "SELECT idempotency_key,json_extract(payload_json,'$.action') AS action "
                        "FROM coordinator_actions WHERE conversation_id=? AND goal_id=? "
                        "AND json_valid(payload_json) AND json_extract(payload_json,'$.action') IN ('queue','message')",
                        (conversation_id, goal_id),
                    ).fetchall()
                    owns_effect = any(current["run_id"] == self.service.active_work_effect_run_id(
                        goal, idempotency_key=f"coordinator-{receipt['action']}:{conversation_id}:{goal_id}:{receipt['idempotency_key']}",
                    ) for receipt in receipts)
                    if owns_effect:
                        if datetime.fromisoformat(current["first_queued_at"]) >= datetime.fromisoformat(current["replacement_queued_at"]):
                            return
                        source_run_id = current["run_id"]
            updated = conn.execute(
                "UPDATE coordinator_goals SET run_id=?,blocker='' "
                "WHERE conversation_id=? AND goal_id=? AND run_id=? "
                "AND intent_state='accepted' AND restore_hold=0 "
                "AND (SELECT restore_hold FROM coordinator_conversations WHERE conversation_id=?)=0",
                (replacement_id, conversation_id, goal_id, source_run_id, conversation_id),
            ).rowcount
            if updated and not conn.execute(
                "UPDATE delegations SET current_run_id=?,updated_at=? "
                "WHERE work_ref=? AND tenant_id=? AND owner_id=? AND current_run_id IN (?,?)",
                (replacement_id, utc_now(), goal["work_ref"], tenant, owner, source_run_id, replacement_id),
            ).rowcount:
                conn.execute("ROLLBACK")
                updated = 0
            current = conn.execute(
                "SELECT run_id,intent_state FROM coordinator_goals WHERE conversation_id=? AND goal_id=?",
                (conversation_id, goal_id),
            ).fetchone()
        if not updated and (not current or current["run_id"] != replacement_id or current["intent_state"] != "accepted"):
            self.service.stop_run(goal["worker_id"], replacement_id)
            raise CoordinatorConflict("Goal changed before follow-up attachment")

    def _follow_recorded_retry(self, tenant: str, owner: str, conversation_id: str, goal_id: str,
                               work_ref: str, run_id: str) -> None:
        """Move a delegation that an earlier retry of this goal left on the run it replaced.

        Before delegations followed their goal's replacements, a completed retry recorded its
        source run and its replacement. Only that recorded pair moves the delegation here.
        """
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            delegation = conn.execute(
                "SELECT current_run_id FROM delegations WHERE work_ref=? AND tenant_id=? AND owner_id=?",
                (work_ref, tenant, owner),
            ).fetchone()
            if delegation is None or delegation["current_run_id"] == run_id:
                return
            for action in conn.execute(
                "SELECT payload_json,response_json FROM coordinator_actions "
                "WHERE conversation_id=? AND goal_id=? AND response_json<>''",
                (conversation_id, goal_id),
            ).fetchall():
                try:
                    payload, response = json.loads(action["payload_json"]), json.loads(action["response_json"])
                except ValueError:
                    continue
                if (isinstance(payload, dict) and isinstance(response, dict) and payload.get("action") == "retry"
                        and payload.get("run_id") == delegation["current_run_id"] and response.get("run_id") == run_id):
                    conn.execute(
                        "UPDATE delegations SET current_run_id=?,updated_at=? "
                        "WHERE work_ref=? AND tenant_id=? AND owner_id=? AND current_run_id=?",
                        (run_id, utc_now(), work_ref, tenant, owner, delegation["current_run_id"]),
                    )
                    return

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
            self._follow_recorded_retry(tenant, owner, conversation_id, goal_id, goal["work_ref"], control.run_id)
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
        self._bind_goal_replacement(tenant, owner, conversation_id, goal_id,
                                    goal, control.run_id, replacement_id)
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
            accepted = conn.execute(
                "SELECT library_json,payload_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
        try:
            library_packet = self._library_packet(
                tenant, owner, json.loads(str(accepted["library_json"] or "{}")), config,
            ) if accepted and not accepted["payload_json"] else {}
        except Exception as exc:
            blocker = _safe_blocker_code(exc)
            with self.store._connect() as conn:
                conn.execute(
                    "UPDATE coordinator_turns SET blocker=? WHERE conversation_id=? AND turn_id=? AND request_id=''",
                    (blocker, conversation_id, turn_id),
                )
            return {"turn_id": turn_id, "state": "blocked", "blocker": blocker,
                    "message": "Selected Library material is unavailable. Refresh the selection and try again."}
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
                if library_packet:
                    messages.append(library_packet["message"])
                packet = {"model": config.model, "reasoning_effort": config.effort,
                          "messages": messages, "metadata": {
                            "tenant_id": tenant, "owner_id": owner, "conversation_id": conversation_id,
                            "agent_id": "coordinator", "message_id": turn_id, "stream_id": turn_id,
                            "idempotency_key": f"{conversation_id}:{turn_id}",
                            "surface": "coordinator",
                            "bootstrap_bundle": library_packet.get("bootstrap_bundle", config.bootstrap_bundle),
                            "allowed_ai_origin_scope": self._conversation_scope(conversation),
                        }}
                if library_packet:
                    # The existing history owner groups this reference with the
                    # latest accepted user turn and must not silently clip it.
                    packet["metadata"]["visible_message_chain"] = [{
                        **library_packet["bootstrap_bundle"]["library_selection"]["reference_message"],
                        "role": "tool", "accepted_source": True,
                    }]
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

    def _repair_turn_output_identity(self, tenant: str, owner: str, conversation_id: str,
                                     turn_id: str, record: dict) -> dict:
        """Bind legacy Chat output to its durable turn; never repeat native work."""
        if record.get("stream_id") or not record.get("run_id"):
            return record
        query = (
            "SELECT p.*,t.payload_json,r.state AS native_state,r.failure_class AS native_failure, "
            "r.failure_structured AS native_structured,r.active_attempt_id AS native_attempt FROM provider_requests p "
            "JOIN coordinator_turns t ON t.request_id=p.request_id "
            "JOIN coordinator_conversations c ON c.conversation_id=t.conversation_id "
            "JOIN provider_sessions s ON s.session_id=p.session_id "
            "JOIN runs r ON r.run_id=p.run_id JOIN workers w ON w.worker_id=r.worker_id "
            "WHERE c.conversation_id=? AND t.turn_id=? AND p.request_id=? AND p.run_id=? "
            "AND c.tenant_id=? AND c.owner_id=? AND p.tenant_id=c.tenant_id AND p.owner_id=c.owner_id "
            "AND s.conversation_id=c.conversation_id AND s.agent_id='coordinator' "
            "AND s.tenant_id=c.tenant_id AND s.owner_id=c.owner_id AND s.worker_id=w.worker_id "
            "AND w.tenant_id=c.tenant_id AND w.owner_id=c.owner_id AND r.tenant_id=c.tenant_id "
            "AND r.project_id=w.project_id AND s.project_id=w.project_id "
            "AND p.message_id=t.turn_id AND p.stream_id='' AND p.response_json='' AND p.fallback_state='' "
            "AND p.restore_hold=0 AND c.restore_hold=0 AND t.restore_hold=0 AND t.blocker<>'cancelled' "
            "AND p.state IN ('queued','running','completed','failed')"
        )
        parameters = (conversation_id, turn_id, record["request_id"], record["run_id"], tenant, owner)
        with self.store._connect() as conn:
            row = conn.execute(query, parameters).fetchone()
        if not row:
            return record
        current = dict(row)
        try:
            metadata = json.loads(current["payload_json"])["metadata"]
        except (ValueError, KeyError, TypeError):
            return record
        if (any(metadata.get(key) != value for key, value in {
                "tenant_id": tenant, "owner_id": owner, "conversation_id": conversation_id,
                "agent_id": "coordinator", "message_id": turn_id}.items())
                or metadata.get("stream_id")):
            return record
        recovering = current["state"] == "failed"
        if recovering:
            if (current["native_state"] != "completed" or current["native_structured"] != 1
                    or current["native_failure"] != "native_output_files_identity_unavailable"):
                return record
            contract = self.provider._saved_completion_contract(current)
            run = self.store.get_run(current["run_id"])
            if not contract or not run or self.provider._native_terminal_output_snapshot(current, run) is None:
                return record
            # Validate the original saved result and its exact files before releasing
            # only response projection to the provider's normal completion/history path.
            try:
                response = self.provider._build_canonical_response({**current, "stream_id": turn_id}, run, contract)
            except HTTPException as exc:
                if (isinstance(exc.detail, dict)
                        and exc.detail.get("code") == "native_output_files_identity_unavailable"):
                    return record
                raise
            if not response.get("glasshive", {}).get("output_files"):
                return record
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(query, parameters).fetchone()
            if (not latest or any(latest[key] != current[key] for key in
                    ("state", "native_state", "native_failure", "native_structured", "native_attempt", "payload_json"))):
                return self.store.get_provider_request(record["request_id"]) or record
            conn.execute("UPDATE provider_requests SET stream_id=?,state=?,updated_at=? WHERE request_id=?",
                         (turn_id, "running" if recovering else current["state"], now(), current["request_id"]))
            if recovering:
                conn.execute("UPDATE runs SET failure_class='',failure_structured=0,failure_retryable=0, "
                             "failure_user_message='',failure_recommended_recovery='',failure_diagnostic_summary='' "
                             "WHERE run_id=?", (current["run_id"],))
        return self.store.get_provider_request(record["request_id"]) or record

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
            record = self._repair_turn_output_identity(tenant, owner, conversation_id, turn["turn_id"], record)
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
                            and run.get("failure_class") in _SAFE_FOREGROUND_FAILURE_CODES):
                        blocker = run["failure_class"]
                with self.store._connect() as conn:
                    conn.execute("UPDATE coordinator_turns SET blocker=? WHERE conversation_id=? AND turn_id=?", (blocker, conversation_id, turn["turn_id"]))
        for goal in snapshot["goals"]:
            if goal["state"] == "restore_held":
                continue
            with self.store._connect() as conn:
                # Queue/Message preserve the source run. Its result is still evidence for this
                # exact goal after a follow-up becomes current; the existing action receipt owns it.
                sources = conn.execute(
                    "SELECT json_extract(payload_json,'$.run_id'),json_extract(response_json,'$.run_id') FROM coordinator_actions "
                    "WHERE conversation_id=? AND goal_id=? AND response_json<>'' "
                    "AND json_valid(payload_json) AND json_valid(response_json) "
                    "AND json_extract(payload_json,'$.action') IN ('queue','message')",
                    (conversation_id, goal["goal_id"]),
                ).fetchall()
                for run_id in dict.fromkeys([goal["run_id"], *(run_id for row in sources for run_id in row)]):
                    if not run_id:
                        continue
                    run = self._owned_goal_result_run(conn, tenant, owner, conversation_id, goal["goal_id"], run_id)
                    # Message can retire a queued reservation into its replacement before
                    # it starts. That predecessor has no independent worker result to surface.
                    # Genuine admission failures still belong to the exact goal, even historically.
                    if (run and run_id != goal["run_id"] and run["state"] == "cancelled"
                            and not run.get("started_at")):
                        continue
                    if run and run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
                        receipt = {"goal_id": goal["goal_id"], "run_id": run_id, "state": run["state"]}
                        conn.execute("INSERT OR IGNORE INTO coordinator_events(conversation_id,kind,identity,payload_json,created_at) VALUES(?,?,?,?,?)", (conversation_id, "result_available", run_id, canonical(receipt), now()))
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

    def accept_native_goals(self, principal: dict, goals: list[Goal]) -> dict:
        """The authenticated run owns its accepted source; the model does not guess IDs."""
        tenant, owner, conversation_id = self.native_identity(principal)
        session = self.store.get_provider_session_by_worker(principal["worker_id"])
        request = self.store.get_provider_request_for_run(principal["run_id"])
        run = self.store.get_run(principal["run_id"])
        if (not session or not request or not run or run["worker_id"] != principal["worker_id"]
                or request["tenant_id"] != tenant or request["owner_id"] != owner
                or request["session_id"] != session["session_id"]
                or not request.get("message_id")):
            raise CoordinatorScopeError("Current accepted conversation turn is unavailable")
        return self.accept_goals(tenant, owner, conversation_id, request["message_id"], goals)

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
                    run = self._owned_goal_result_run(
                        conn, tenant, owner, conversation_id, item["goal_id"], item["run_id"],
                    )
                    if run:
                        output = str(run["output_text"] or "")
                        length = min(len(output), 12000, inline_remaining)
                        item["result"] = {
                            "output_text": output[:length], "total_chars": len(output),
                            "next_offset": length if length < len(output) else None,
                            "sha256": hashlib.sha256(output.encode()).hexdigest(),
                            "failure_class": str(run["failure_class"] or ""),
                        }
                        files = coordinator_current_result_files(
                            self.service, conn, tenant, owner, conversation_id,
                            str(item["goal_id"]), str(item["run_id"]),
                        )
                        if files:
                            item["result"]["current_workspace_files"] = list(files.values())
                        inline_remaining -= length
                items.append(item)
            evidence = {"kind": "worker_result_notifications", "items": items}
            current_library = conn.execute(
                "SELECT library_json FROM coordinator_conversations WHERE conversation_id=?", (conversation_id,),
            ).fetchone()[0]
            conn.execute("INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,library_json,created_at) VALUES(?,?,?,?,?,?)", (conversation_id, turn_id, canonical(evidence), "worker_results", current_library, now()))
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
        """Finish a recorded control after a process exits before goal rebinding."""

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
            if control.action not in {"retry", "queue", "message", "dismiss"}:
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
                # As for turns, a permanent typed admission blocker waits for an explicit
                # re-dispatch; only crash recovery and capacity blockers replay here.
                if goal["blocker"] and goal["blocker"] not in _RETRYABLE_ADMISSION_CODES:
                    continue
                results.append(self.dispatch(tenant, owner, conversation_id, Dispatch.model_validate_json(goal["dispatch_json"])))
                continue
            # Stop can win between the service reservation and goal attachment. Recover
            # only an existing reservation; never create new work for a stopped intent.
            dispatch = Dispatch.model_validate_json(goal["dispatch_json"])
            if dispatch.worker_id:
                run = self.store.get_run(_worker_assignment_run_id(dispatch.worker_id, conversation_id, goal["goal_id"]))
                worker = self.store.get_worker(dispatch.worker_id)
                reservation = ({"work_ref": "", "worker_id": dispatch.worker_id, "initial_run_id": run["run_id"]}
                               if run and worker and worker["tenant_id"] == tenant and worker["owner_id"] == owner
                               and run["worker_id"] == dispatch.worker_id else None)
            else:
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

    @staticmethod
    def _owned_goal_result_run(conn: Any, tenant: str, owner: str, conversation_id: str,
                               goal_id: str, run_id: str) -> dict | None:
        row = conn.execute(
            "SELECT r.* FROM coordinator_goals g JOIN runs r ON r.worker_id=g.worker_id "
            "JOIN workers w ON w.worker_id=r.worker_id "
            "WHERE g.conversation_id=? AND g.goal_id=? AND r.run_id=? "
            "AND r.tenant_id=? AND w.tenant_id=? AND w.owner_id=? "
            "AND (g.run_id=r.run_id OR EXISTS (SELECT 1 FROM coordinator_actions a "
            "WHERE a.conversation_id=g.conversation_id AND a.goal_id=g.goal_id "
            "AND json_valid(a.payload_json) "
            "AND json_extract(a.payload_json,'$.action') IN ('queue','message') "
            "AND (json_extract(a.payload_json,'$.run_id')=r.run_id "
            "OR (json_valid(a.response_json) AND json_extract(a.response_json,'$.run_id')=r.run_id))))",
            (conversation_id, goal_id, run_id, tenant, tenant, owner),
        ).fetchone()
        return dict(row) if row else None

    def read_result(self, tenant: str, owner: str, conversation_id: str, goal_id: str,
                    offset: int = 0, max_chars: int = 12000, run_id: str = "") -> dict:
        if offset < 0 or not 1 <= max_chars <= 64000:
            raise CoordinatorConflict("Invalid result page")
        goal = self.goal_snapshot(tenant, owner, conversation_id, goal_id)
        if run_id and run_id != goal["run_id"]:
            with self.store._connect() as conn:
                result = self._owned_goal_result_run(conn, tenant, owner, conversation_id, goal_id, run_id)
            if result is None:
                raise CoordinatorScopeError("Bound result unavailable")
            goal = {**goal, "run_id": run_id, "state": result["state"],
                    "output_text": result["output_text"], "failure_class": result["failure_class"]}
        text = goal.get("output_text", "")
        if offset > len(text):
            raise CoordinatorConflict("Result offset exceeds source")
        end = min(len(text), offset + max_chars)
        with self.store._connect() as conn:
            files = coordinator_current_result_files(
                self.service, conn, tenant, owner, conversation_id, goal_id, str(goal["run_id"]),
            )
        return {"goal_id": goal_id, "run_id": goal["run_id"], "state": goal["state"],
                "output_text": text[offset:end], "total_chars": len(text), "offset": offset,
                "next_offset": end if end < len(text) else None,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "failure_class": goal.get("failure_class", ""),
                **({"current_workspace_files": list(files.values())} if files else {})}


def _current_path_references(output: str) -> set[str]:
    from .deliverables import NATIVE_MARKDOWN_LINK_OR_ESCAPE

    references = {match.group(2).strip().removeprefix("<").removesuffix(">")
        for match in NATIVE_MARKDOWN_LINK_OR_ESCAPE.finditer(output) if match.group(2) is not None}
    # Web/action URLs and immutable identities already have their own renderer.
    return {reference for reference in references if reference and ":" not in reference
            and not reference.startswith("//") and not any(c in reference for c in "?#\\\x00\r\n")}


def coordinator_current_result_files(service: Any, conn: Any, tenant: str, owner: str,
                                     conversation_id: str, goal_id: str, run_id: str,
                                     *, references: set[str] | None = None) -> dict[str, dict]:
    """Project exact declared current files; never manufacture captured run bytes."""
    from .workspace_files import FileAdmissionError, native_workspace_relative_path

    source = CoordinatorService._owned_goal_result_run(conn, tenant, owner, conversation_id, goal_id, run_id)
    if not source or source["state"] != "completed":
        return {}
    references_in_source = _current_path_references(str(source["output_text"] or ""))
    if references is not None:
        references_in_source.intersection_update(references)
    if not references_in_source:
        return {}
    row = conn.execute(
        "SELECT g.work_ref,c.scope_json FROM coordinator_goals g "
        "JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id "
        "WHERE g.conversation_id=? AND g.goal_id=? AND c.tenant_id=? AND c.owner_id=?",
        (conversation_id, goal_id, tenant, owner),
    ).fetchone()
    worker = service.store.get_worker(source["worker_id"], tenant, owner)
    if not row or not worker or source["project_id"] != worker.get("project_id"):
        return {}
    # Callback missions retain their completion-envelope publication fence.
    if (service._bootstrap_bundle_for(worker) or {}).get("callbacks") not in (None, {}):
        return {}
    delegation = service.store.get_delegation_for_worker(worker["worker_id"], tenant_id=tenant, owner_id=owner)
    if row["work_ref"]:
        if (not delegation or delegation["work_ref"] != row["work_ref"]
                or delegation.get("origin_surface") != "coordinator"
                or delegation.get("origin_ref") != f"{conversation_id}:{goal_id}"
                or delegation.get("project_id") != source["project_id"]):
            return {}
    else:
        scope = json.loads(row["scope_json"] or "{}")
        if (delegation or not scope.get("project_id") or scope["project_id"] != worker["project_id"]
                or (scope.get("workspace_id") and scope["workspace_id"] != worker.get("workspace_id"))):
            return {}
    files = {}
    for reference in sorted(references_in_source):
        relative = native_workspace_relative_path(service.runtime, worker, reference)
        if relative is None or reference in files:
            continue
        try:
            file = service.files.current_artifact(worker, relative)
        except (FileAdmissionError, OSError, ValueError):
            continue
        open_url = service._signed_artifact_open_url(worker, relative)
        download_url = service._signed_artifact_download_url(worker, relative)
        if open_url and download_url:
            files[reference] = {
                "filename": file["filename"], "open_url": open_url, "download_url": download_url,
                "semantics": "current_workspace_file",
            }
    return files


def project_coordinator_response(service: Any, tenant: str, owner: str,
                                 request_id: str, response: dict) -> dict:
    """Render a response copy with exact owner-authorized current-file actions.

    Stored provider/turn text and result hashes remain unchanged. Multiple workers
    declaring the same native path are ambiguous and grant no link authority.
    """
    from .deliverables import NATIVE_MARKDOWN_LINK_OR_ESCAPE

    if not request_id or not isinstance(response, dict):
        return response
    references = set()
    for choice in response.get("choices", []):
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            references.update(_current_path_references(message["content"]))
    if not references:
        return response
    with service.store._connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_turns'").fetchone():
            return response
        conversation = conn.execute(
            "SELECT c.conversation_id FROM coordinator_conversations c "
            "JOIN coordinator_turns t ON t.conversation_id=c.conversation_id "
            "JOIN provider_requests p ON p.request_id=t.request_id "
            "WHERE t.request_id=? AND c.tenant_id=? AND c.owner_id=? "
            "AND p.tenant_id=c.tenant_id AND p.owner_id=c.owner_id",
            (request_id, tenant, owner),
        ).fetchone()
        if not conversation:
            return response
        cid = conversation["conversation_id"]
        candidates = conn.execute(
            "SELECT DISTINCT g.goal_id,r.run_id,r.worker_id FROM coordinator_goals g "
            "JOIN runs r ON r.worker_id=g.worker_id WHERE g.conversation_id=? AND r.state='completed' "
            "AND (g.run_id=r.run_id OR EXISTS (SELECT 1 FROM coordinator_actions a "
            "WHERE a.conversation_id=g.conversation_id AND a.goal_id=g.goal_id "
            "AND json_valid(a.payload_json) "
            "AND json_extract(a.payload_json,'$.action') IN ('queue','message') "
            "AND (json_extract(a.payload_json,'$.run_id')=r.run_id "
            "OR (json_valid(a.response_json) AND json_extract(a.response_json,'$.run_id')=r.run_id))))",
            (cid,),
        ).fetchall()
        aliases: dict[str, dict[tuple[str, str], dict]] = {}
        for candidate in candidates:
            files = coordinator_current_result_files(service, conn, tenant, owner, cid,
                candidate["goal_id"], candidate["run_id"], references=references)
            for reference, file in files.items():
                identity = (candidate["worker_id"], reference)
                aliases.setdefault(reference, {})[identity] = file
    if not aliases:
        return response
    projected = json.loads(json.dumps(response))

    def render(match):
        if match.group(2) is None:
            return match.group(0)
        reference = match.group(2).strip().removeprefix("<").removesuffix(">")
        choices = aliases.get(reference, {})
        if len(choices) != 1:
            return match.group(0)
        return f"{match.group(1)}({next(iter(choices.values()))['download_url']})"

    for choice in projected.get("choices", []):
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"] = NATIVE_MARKDOWN_LINK_OR_ESCAPE.sub(render, message["content"])
    return projected


def project_coordinator_output(service: Any, request: dict, output: str) -> str:
    response = project_coordinator_response(service, str(request.get("tenant_id") or ""),
        str(request.get("owner_id") or ""), str(request.get("request_id") or ""),
        {"choices": [{"message": {"content": output}}]})
    return response["choices"][0]["message"]["content"]


def render_coordinator_result_file(service: Any, request: dict, reference: str,
                                   selected_link: str, output_files: list | None) -> str | None:
    """Publish only a terminal result bound to this request's exact Chat receipts.

    A hash names bytes; it grants no access. Keep the source worker/run/attempt as
    the publication authority and reuse its existing immutable file renderer.
    """
    from .deliverables import native_output_sha256

    if not request.get("request_id"):
        return None
    with service.store._connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_turns'").fetchone():
            return None
        conversation = conn.execute(
            "SELECT c.* FROM provider_requests p JOIN coordinator_turns t ON t.request_id=p.request_id "
            "JOIN coordinator_conversations c ON c.conversation_id=t.conversation_id "
            "WHERE p.request_id=? AND p.run_id=? AND p.tenant_id=? AND p.owner_id=? "
            "AND c.tenant_id=p.tenant_id AND c.owner_id=p.owner_id",
            (request["request_id"], request.get("run_id"), request.get("tenant_id"), request.get("owner_id")),
        ).fetchone()
        if not conversation:
            return None
        scope = json.loads(conversation["scope_json"] or "{}")
        candidates = conn.execute(
            "SELECT DISTINCT g.goal_id,g.work_ref,r.run_id FROM coordinator_goals g "
            "JOIN runs r ON r.worker_id=g.worker_id WHERE g.conversation_id=? "
            "AND r.state IN ('completed','failed','cancelled','interrupted') "
            "AND instr(r.output_text,?)>0 "
            "AND (g.run_id=r.run_id OR EXISTS (SELECT 1 FROM coordinator_actions a "
            "WHERE a.conversation_id=g.conversation_id AND a.goal_id=g.goal_id "
            "AND json_valid(a.payload_json) AND json_valid(a.response_json) "
            "AND json_extract(a.payload_json,'$.action') IN ('queue','message') "
            "AND (json_extract(a.payload_json,'$.run_id')=r.run_id "
            "OR json_extract(a.response_json,'$.run_id')=r.run_id)))",
            (conversation["conversation_id"], reference),
        ).fetchall()
        for candidate in candidates:
            source = CoordinatorService._owned_goal_result_run(
                conn, request["tenant_id"], request["owner_id"], conversation["conversation_id"],
                candidate["goal_id"], candidate["run_id"],
            )
            if source is None:
                continue
            worker = service.store.get_worker(source["worker_id"])
            if not worker or source["project_id"] != worker.get("project_id"):
                continue
            if candidate["work_ref"]:
                delegation = service.store.get_delegation(candidate["work_ref"],
                    tenant_id=request["tenant_id"], owner_id=request["owner_id"])
                if (not delegation or delegation["worker_id"] != source["worker_id"]
                        or delegation["project_id"] != source["project_id"]):
                    continue
            elif (not scope.get("project_id") or worker.get("project_id") != scope["project_id"]
                  or (scope.get("workspace_id") and worker.get("workspace_id") != scope["workspace_id"])):
                continue
            _, files = service.runtime.provider_native_image_output(worker, source, "")
            if not any("artifact_sha256:" + native_output_sha256(item[2]) == reference for item in files):
                continue
            # No request_id on this source request: publication cannot recurse into Chat.
            source_request = {"run_id": source["run_id"], "owner_id": request["owner_id"],
                              "tenant_id": request["tenant_id"]}
            captured: list[dict] = []
            rendered = service.render_provider_native_images(
                source_request, source, selected_link,
                output_files=captured if output_files is not None else None)
            if output_files is not None:
                output_files.extend(item for item in captured if item not in output_files)
            return rendered
    return None


def _worker_assignment_key(conversation_id: str, goal_id: str) -> str:
    return f"coordinator:{conversation_id}:{goal_id}"


def _worker_assignment_run_id(worker_id: str, conversation_id: str, goal_id: str) -> str:
    from .service import WorkersProjectsService
    return WorkersProjectsService._idempotent_run_id(worker_id, _worker_assignment_key(conversation_id, goal_id))


def coordinator_run_ready(conn: Any, run_id: str) -> bool:
    """Queue claim must wait for context/files attachment, including on busy workers."""
    try:
        guard_coordinator_run(conn, run_id)
    except CoordinatorConflict:
        return False
    return True


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

    # Ordinary assignment has no delegation row. Its existing dispatch receipt and
    # assignment ID fence the gap before attachment, without another durable ledger.
    rows = conn.execute(
        "SELECT g.*,c.restore_hold AS conversation_restore_hold,r.worker_id AS assigned_worker "
        "FROM runs r JOIN workers w ON w.worker_id=r.worker_id "
        "JOIN coordinator_conversations c ON c.tenant_id=w.tenant_id AND c.owner_id=w.owner_id "
        "JOIN coordinator_goals g ON g.conversation_id=c.conversation_id AND g.worker_id=r.worker_id "
        "WHERE r.run_id=? AND json_valid(g.dispatch_json) "
        "AND json_extract(g.dispatch_json,'$.worker_id')=r.worker_id",
        (run_id,),
    ).fetchall()
    for goal in rows:
        bound = goal["run_id"] == run_id
        reserved = _worker_assignment_run_id(goal["assigned_worker"], goal["conversation_id"], goal["goal_id"]) == run_id
        if (bound or reserved) and (not bound or goal["intent_state"] == "cancelled"
                or goal["restore_hold"] or goal["conversation_restore_hold"]):
            raise CoordinatorConflict("Coordinator goal is unattached, held or stopped before execution")


def coordinator_dispatch_origin(conn: Any, tenant: str, owner: str, origin_ref: str) -> dict | None:
    """Current accepted dispatch provenance, before its worker/run is reserved."""
    row = conn.execute(
        "SELECT g.*,c.config_json,c.scope_json,c.tenant_id,c.owner_id "
        "FROM coordinator_goals g JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id "
        "JOIN coordinator_turns t ON t.conversation_id=g.conversation_id AND t.turn_id=g.source_turn_id "
        "WHERE c.tenant_id=? AND c.owner_id=? AND c.conversation_id || ':' || g.goal_id=? "
        "AND g.intent_state='accepted' AND g.restore_hold=0 AND c.restore_hold=0 AND t.restore_hold=0",
        (tenant, owner, origin_ref),
    ).fetchone()
    return dict(row) if row else None


def coordinator_current_run_origins(conn: Any, worker: dict, run_id: str) -> list[dict]:
    """Exact active goal/effect ownership, narrower than historical result access."""
    rows = conn.execute(
        "SELECT g.*,c.config_json,c.scope_json,c.tenant_id,c.owner_id,"
        "w.project_id AS worker_project_id,w.workspace_id AS worker_workspace_id,"
        "w.profile AS worker_profile,w.model AS worker_model,w.execution_mode AS worker_mode,"
        "w.workspace_kind AS worker_kind,w.trusted_run_lane AS worker_lane,"
        "w.bootstrap_bundle_json AS worker_bootstrap "
        "FROM runs r JOIN workers w ON w.worker_id=r.worker_id "
        "JOIN coordinator_goals g ON g.worker_id=w.worker_id "
        "JOIN coordinator_conversations c ON c.conversation_id=g.conversation_id "
        "JOIN coordinator_turns t ON t.conversation_id=g.conversation_id AND t.turn_id=g.source_turn_id "
        "WHERE r.run_id=? AND w.worker_id=? AND c.tenant_id=? AND c.owner_id=? "
        "AND r.tenant_id=c.tenant_id AND w.tenant_id=c.tenant_id AND w.owner_id=c.owner_id "
        "AND r.project_id=w.project_id "
        "AND g.intent_state='accepted' AND g.restore_hold=0 AND c.restore_hold=0 AND t.restore_hold=0 "
        "AND w.state NOT IN ('terminated','terminating','termination_failed','stopping') "
        "AND r.state IN ('queued','claimed','admitted','running','settling','paused') "
        "AND (g.run_id=r.run_id OR EXISTS (SELECT 1 FROM coordinator_actions a "
        "WHERE a.conversation_id=g.conversation_id AND a.goal_id=g.goal_id "
        "AND json_valid(a.payload_json) AND json_valid(a.response_json) "
        "AND json_extract(a.payload_json,'$.action') IN ('queue','message') "
        "AND json_extract(a.response_json,'$.run_id')=r.run_id))",
        (run_id, worker.get("worker_id"), worker.get("tenant_id") or "local", worker.get("owner_id")),
    ).fetchall()
    origins = []
    for raw in rows:
        row = dict(raw)
        if any(str(row[column] or "") != str(worker.get(key) or "") for column, key in (
            ("worker_profile", "profile"), ("worker_model", "model"), ("worker_mode", "execution_mode"),
            ("worker_bootstrap", "bootstrap_bundle_json"),
        )):
            continue
        try:
            dispatch = Dispatch.model_validate_json(row["dispatch_json"])
            stored_scope = json.loads(row["scope_json"])
            scope = CoordinatorScope.model_validate({
                key: stored_scope[key] for key in CoordinatorScope.model_fields if key in stored_scope
            })
        except ValueError:
            continue
        if dispatch.worker_id:
            if (dispatch.worker_id != worker.get("worker_id") or row["work_ref"]
                    or scope.project_id != row["worker_project_id"]
                    or (scope.workspace_id and scope.workspace_id != row["worker_workspace_id"])
                    or row["worker_kind"] == "ephemeral" or row["worker_lane"] == "conversation"
                    or conn.execute("SELECT 1 FROM delegations WHERE worker_id=?", (worker["worker_id"],)).fetchone()
                    or conn.execute("SELECT 1 FROM provider_sessions WHERE worker_id=?", (worker["worker_id"],)).fetchone()):
                continue
        elif not conn.execute(
            "SELECT 1 FROM delegations WHERE work_ref=? AND worker_id=? AND tenant_id=? AND owner_id=? "
            "AND origin_surface='coordinator' AND origin_ref=?",
            (row["work_ref"], worker["worker_id"], row["tenant_id"], row["owner_id"],
             row["conversation_id"] + ":" + row["goal_id"]),
        ).fetchone():
            continue
        origins.append(row)
    return origins
