"""Same-owner peer authority and durable delivery through the existing run queue.

Discovery is metadata visibility, never execution authority. A grant is scoped to
both members and both workspace policy revisions. Revocation is checked again at
the durable runtime-invocation boundary; already invoked models cannot unsee data.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import CLOSED_WORKER_STATES, utc_now
from .schema_version import (
    execute_schema_script,
    record_schema_version,
    require_compatible_schema,
)
from .secret_redaction import CREDENTIAL_REDACTIONS
from .workspace_continuation import (
    build_workspace_continuation_context,
    continuation_instruction,
)

SUPPORTED_SCOPES = frozenset({"message", "wake", "context_read"})

Scope = Literal[
    "message",
    "wake",
    "context_read",
    "artifact_read",
    "file_write",
    "control",
    "delegate",
]


class PeerError(ValueError):
    def __init__(self, code: str, status_code: int = 403):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class PeerPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    discovery: Literal["off", "workspace", "selected", "account"] = "off"
    selected_workspaces: list[Annotated[str, Field(min_length=1, max_length=128)]] = (
        Field(default_factory=list, max_length=100)
    )
    access_enabled: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def check_selection(self):
        if self.discovery != "selected" and self.selected_workspaces:
            raise ValueError("Only selected discovery accepts workspace references")
        if len(set(self.selected_workspaces)) != len(self.selected_workspaces):
            raise ValueError("Workspace references must be unique")
        return self


class PeerGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_worker_id: str = Field(min_length=1, max_length=128)
    target_worker_id: str = Field(min_length=1, max_length=128)
    scopes: list[Scope] = Field(min_length=1, max_length=7)
    resource_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=100
    )
    source_revision: int = Field(ge=1, strict=True)
    target_revision: int = Field(ge=1, strict=True)
    expires_at: str | None
    idempotency_key: str = Field(min_length=1, max_length=128)


class PeerMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_worker_id: str = Field(min_length=1, max_length=128)
    grant_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=65536)
    idempotency_key: str = Field(min_length=1, max_length=128)
    target_run_id: str | None = Field(default=None, max_length=128)
    reply_to: str | None = Field(default=None, max_length=128)
    expires_at: str | None = None

    @model_validator(mode="after")
    def bounded_payload(self):
        if len(self.message.encode("utf-8")) > 65536:
            raise ValueError("Peer message exceeds 65536 bytes")
        return self


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise PeerError("peer_expiry_invalid", 422) from None


def _future(raw: str) -> str:
    value = _time(raw)
    now = datetime.now(timezone.utc)
    if not now < value <= now + timedelta(days=30):
        raise PeerError("peer_expiry_invalid", 422)
    return value.isoformat()


def _redacted(text: str) -> str:
    for pattern, replacement in CREDENTIAL_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def ensure_peer_schema(conn: sqlite3.Connection) -> None:
    from .peer_permissions import preserve_v1_grants, restore_v1_grants

    require_compatible_schema(conn, component="peer_collaboration", target_version=3)
    migration = preserve_v1_grants(conn)
    execute_schema_script(
        conn,
        """
    CREATE TABLE IF NOT EXISTS peer_policies (
      workspace_id TEXT PRIMARY KEY REFERENCES execution_workspaces(workspace_id) ON DELETE CASCADE,
      discovery TEXT NOT NULL DEFAULT 'off', selected_json TEXT NOT NULL DEFAULT '[]',
      access_enabled INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS peer_grants (
      grant_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
      source_worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
      target_worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
      source_workspace_id TEXT NOT NULL, target_workspace_id TEXT NOT NULL,
      source_revision INTEGER NOT NULL, target_revision INTEGER NOT NULL,
      scopes_json TEXT NOT NULL, resources_json TEXT NOT NULL, expires_at TEXT,
      revoked_at TEXT, created_at TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
      UNIQUE(tenant_id, owner_id, idempotency_key)
    );
    CREATE TABLE IF NOT EXISTS peer_messages (
      sequence INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
      tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
      source_worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
      target_worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
      source_run_id TEXT NOT NULL DEFAULT '', target_run_id TEXT NOT NULL DEFAULT '',
      grant_id TEXT NOT NULL REFERENCES peer_grants(grant_id) ON DELETE CASCADE,
      reply_to TEXT, message TEXT NOT NULL, instruction TEXT NOT NULL, continuation_context_json TEXT NOT NULL DEFAULT '{}',
      delivery_run_id TEXT UNIQUE NOT NULL, expires_at TEXT NOT NULL, accepted_at TEXT NOT NULL,
      invoked_at TEXT, unavailable_code TEXT NOT NULL DEFAULT '',
      idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
      UNIQUE(tenant_id, owner_id, source_worker_id, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS peer_messages_recipient ON peer_messages(target_worker_id, sequence);
    CREATE TABLE IF NOT EXISTS peer_native_sessions (
      token_sha256 TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
      run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
      attempt_id TEXT NOT NULL, expires_at TEXT NOT NULL
    );
    """,
    )
    restore_v1_grants(conn, migration)
    conn.execute("""CREATE TABLE IF NOT EXISTS peer_grant_batches (
      tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
      request_json TEXT NOT NULL, grant_ids_json TEXT NOT NULL,
      PRIMARY KEY(tenant_id,owner_id,idempotency_key))""")
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(peer_native_sessions)")
    }
    if "purpose" not in columns:
        conn.execute(
            "ALTER TABLE peer_native_sessions ADD COLUMN purpose TEXT NOT NULL DEFAULT 'peer'"
        )
    execute_schema_script(
        conn,
        """
    CREATE TRIGGER IF NOT EXISTS context_session_run_revocation
    AFTER UPDATE OF state,active_attempt_id ON runs
    BEGIN DELETE FROM peer_native_sessions WHERE purpose='context' AND run_id=NEW.run_id
      AND (NEW.state!='running' OR attempt_id!=COALESCE(NEW.active_attempt_id,'')); END;
    CREATE TRIGGER IF NOT EXISTS context_session_attempt_revocation
    AFTER UPDATE OF state,ended_at ON run_attempts
    WHEN NEW.state!='running' OR NEW.ended_at IS NOT NULL
    BEGIN DELETE FROM peer_native_sessions WHERE purpose='context' AND attempt_id=NEW.attempt_id; END;
    CREATE TRIGGER IF NOT EXISTS context_session_lease_revocation
    AFTER UPDATE OF status,released_at,attempt_id ON host_run_leases
    WHEN NEW.status!='active' OR NEW.released_at IS NOT NULL OR NEW.attempt_id!=OLD.attempt_id
    BEGIN DELETE FROM peer_native_sessions WHERE purpose='context' AND run_id=NEW.run_id; END;
    CREATE TRIGGER IF NOT EXISTS context_session_worker_revocation
    AFTER UPDATE OF state ON workers
    WHEN NEW.state IN ('terminating','termination_failed','terminated')
    BEGIN DELETE FROM peer_native_sessions WHERE purpose='context' AND worker_id=NEW.worker_id; END;
    """,
    )
    record_schema_version(conn, component="peer_collaboration", version=3)


def _worker(conn, worker_id, tenant_id, owner_id):
    row = conn.execute(
        "SELECT * FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",
        (worker_id, tenant_id, owner_id),
    ).fetchone()
    if row is None or row["state"] in CLOSED_WORKER_STATES:
        raise PeerError("peer_not_found", 404)
    return dict(row)


def _policy(conn, workspace_id, tenant_id, owner_id):
    row = conn.execute(
        """SELECT w.workspace_id,w.policy_revision,p.discovery,p.selected_json,p.access_enabled
      FROM execution_workspaces w LEFT JOIN peer_policies p ON p.workspace_id=w.workspace_id
      WHERE w.workspace_id=? AND w.tenant_id=? AND w.owner_id=?""",
        (workspace_id, tenant_id, owner_id),
    ).fetchone()
    if row is None:
        raise PeerError("peer_not_found", 404)
    return {
        "workspace_id": row["workspace_id"],
        "revision": row["policy_revision"],
        "discovery": row["discovery"] or "off",
        "selected_workspaces": json.loads(row["selected_json"] or "[]"),
        "access_enabled": bool(row["access_enabled"]),
    }


def _authorize(conn, grant_id, source, target, tenant, owner, scope, resource_id=""):
    a = _worker(conn, source, tenant, owner)
    b = _worker(conn, target, tenant, owner)
    ap = _policy(conn, a["workspace_id"], tenant, owner)
    bp = _policy(conn, b["workspace_id"], tenant, owner)
    row = conn.execute(
        "SELECT * FROM peer_grants WHERE grant_id=? AND tenant_id=? AND owner_id=?",
        (grant_id, tenant, owner),
    ).fetchone()
    if (
        not row
        or not ap["access_enabled"]
        or not bp["access_enabled"]
        or row["revoked_at"]
        or (
            row["expires_at"] is not None
            and _time(row["expires_at"]) <= datetime.now(timezone.utc)
        )
    ):
        raise PeerError("peer_access_denied")
    if (
        row["source_worker_id"],
        row["target_worker_id"],
        row["source_workspace_id"],
        row["target_workspace_id"],
        row["source_revision"],
        row["target_revision"],
    ) != (
        source,
        target,
        a["workspace_id"],
        b["workspace_id"],
        ap["revision"],
        bp["revision"],
    ):
        raise PeerError("peer_access_denied")
    if scope not in json.loads(row["scopes_json"]):
        raise PeerError("peer_access_denied")
    if scope in {
        "context_read",
        "artifact_read",
        "file_write",
        "control",
        "delegate",
    } and resource_id not in json.loads(row["resources_json"]):
        raise PeerError("peer_access_denied")
    return dict(row)


def guard_peer_run(
    conn, run_id: str, worker_id: str, *, instruction: str | None = None, invoked=False
):
    """Called inside the existing run reservation/invocation write transaction."""
    row = conn.execute(
        "SELECT * FROM peer_messages WHERE delivery_run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return
    if row["target_worker_id"] != worker_id or (
        instruction is not None and row["instruction"] != instruction
    ):
        raise PeerError("peer_delivery_scope_mismatch")
    if _time(row["expires_at"]) <= datetime.now(timezone.utc):
        raise PeerError("peer_message_expired", 409)
    _authorize(
        conn,
        row["grant_id"],
        row["source_worker_id"],
        row["target_worker_id"],
        row["tenant_id"],
        row["owner_id"],
        "message",
    )
    if invoked:
        conn.execute(
            "UPDATE peer_messages SET invoked_at=COALESCE(invoked_at,?) WHERE message_id=?",
            (utc_now(), row["message_id"]),
        )


class PeerCollaboration:
    def __init__(self, store, service):
        self.store = store
        self.service = service

    def workspace_choices(self, *, tenant_id, owner_id):
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT w.workspace_id,COALESCE(m.name,'Workspace') AS name FROM execution_workspaces w LEFT JOIN workers m ON m.worker_id=w.default_worker_id WHERE w.tenant_id=? AND w.owner_id=? ORDER BY w.created_at",
                (tenant_id, owner_id),
            )
            return {
                "items": [
                    {
                        "workspace_id": row["workspace_id"],
                        "name": _redacted(row["name"]),
                    }
                    for row in rows
                ]
            }

    def policy(self, workspace_id, *, tenant_id, owner_id):
        with self.store._connect() as conn:
            return _policy(conn, workspace_id, tenant_id, owner_id)

    def set_policy(
        self, workspace_id, *, tenant_id, owner_id, request: PeerPolicyUpdate
    ):
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = _policy(conn, workspace_id, tenant_id, owner_id)
            if old["revision"] != request.expected_revision:
                raise PeerError("peer_policy_stale", 409)
            if (
                old["discovery"] == request.discovery
                and old["access_enabled"] == request.access_enabled
                and set(old["selected_workspaces"]) == set(request.selected_workspaces)
            ):
                return old
            for target in request.selected_workspaces:
                _policy(conn, target, tenant_id, owner_id)
            conn.execute(
                """INSERT INTO peer_policies VALUES (?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET
              discovery=excluded.discovery,selected_json=excluded.selected_json,access_enabled=excluded.access_enabled""",
                (
                    workspace_id,
                    request.discovery,
                    _json(request.selected_workspaces),
                    int(request.access_enabled),
                ),
            )
            conn.execute(
                "UPDATE execution_workspaces SET policy_revision=policy_revision+1,updated_at=? WHERE workspace_id=?",
                (utc_now(), workspace_id),
            )
            return _policy(conn, workspace_id, tenant_id, owner_id)

    def discover(self, worker_id, *, tenant_id, owner_id):
        def visible(policy, own, target):
            return (
                policy["discovery"] == "account"
                or (policy["discovery"] == "workspace" and own == target)
                or (
                    policy["discovery"] == "selected"
                    and target in policy["selected_workspaces"]
                )
            )

        with self.store._connect() as conn:
            worker = _worker(conn, worker_id, tenant_id, owner_id)
            policy = _policy(conn, worker["workspace_id"], tenant_id, owner_id)
            items = []
            if policy["discovery"] != "off":
                for row in conn.execute(
                    "SELECT worker_id,workspace_id,name,role,profile,state FROM workers WHERE tenant_id=? AND owner_id=? AND worker_id<>? ORDER BY worker_id",
                    (tenant_id, owner_id, worker_id),
                ):
                    if row["state"] in CLOSED_WORKER_STATES:
                        continue
                    target_policy = _policy(
                        conn, row["workspace_id"], tenant_id, owner_id
                    )
                    if visible(
                        policy, worker["workspace_id"], row["workspace_id"]
                    ) and visible(
                        target_policy, row["workspace_id"], worker["workspace_id"]
                    ):
                        items.append(
                            {
                                **dict(row),
                                "name": _redacted(row["name"]),
                                "role": _redacted(row["role"]),
                                "policy_revision": target_policy["revision"],
                            }
                        )
            return {"items": items, "policy": policy, "discovery_grants_access": False}

    def grant(self, *, tenant_id, owner_id, request: PeerGrantRequest):
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._grant(
                conn, tenant_id=tenant_id, owner_id=owner_id, request=request
            )

    def _grant(self, conn, *, tenant_id, owner_id, request: PeerGrantRequest):
        expires = (
            _future(request.expires_at) if request.expires_at is not None else None
        )
        spec = _json(request.model_dump())
        if not set(request.scopes) <= SUPPORTED_SCOPES:
            raise PeerError("peer_scope_unavailable", 422)
        if request.source_worker_id == request.target_worker_id or len(
            set(request.scopes)
        ) != len(request.scopes):
            raise PeerError("peer_grant_invalid", 422)
        if (
            set(request.scopes)
            & {"context_read", "artifact_read", "file_write", "control", "delegate"}
            and not request.resource_ids
        ):
            raise PeerError("peer_resource_scope_required", 422)
        a = _worker(conn, request.source_worker_id, tenant_id, owner_id)
        b = _worker(conn, request.target_worker_id, tenant_id, owner_id)
        old = conn.execute(
            "SELECT * FROM peer_grants WHERE tenant_id=? AND owner_id=? AND idempotency_key=?",
            (tenant_id, owner_id, request.idempotency_key),
        ).fetchone()
        if old:
            if old["request_json"] != spec:
                raise PeerError("peer_idempotency_conflict", 409)
            return self._grant_view(old)
        ap = _policy(conn, a["workspace_id"], tenant_id, owner_id)
        bp = _policy(conn, b["workspace_id"], tenant_id, owner_id)
        if (ap["revision"], bp["revision"]) != (
            request.source_revision,
            request.target_revision,
        ):
            raise PeerError("peer_policy_stale", 409)
        if not ap["access_enabled"] or not bp["access_enabled"]:
            raise PeerError("peer_access_denied")
        grant_id = "pg_" + uuid.uuid4().hex
        conn.execute(
            """INSERT INTO peer_grants (grant_id,tenant_id,owner_id,source_worker_id,target_worker_id,
                source_workspace_id,target_workspace_id,source_revision,target_revision,scopes_json,
                resources_json,expires_at,revoked_at,created_at,idempotency_key,request_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)""",
            (
                grant_id,
                tenant_id,
                owner_id,
                a["worker_id"],
                b["worker_id"],
                a["workspace_id"],
                b["workspace_id"],
                ap["revision"],
                bp["revision"],
                _json(request.scopes),
                _json(request.resource_ids),
                expires,
                utc_now(),
                request.idempotency_key,
                spec,
            ),
        )
        return self._grant_view(
            conn.execute(
                "SELECT * FROM peer_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
        )

    @staticmethod
    def _grant_view(row):
        return {
            key: row[key]
            for key in (
                "grant_id",
                "source_worker_id",
                "target_worker_id",
                "source_revision",
                "target_revision",
                "expires_at",
                "revoked_at",
            )
        } | {
            "scopes": json.loads(row["scopes_json"]),
            "resource_ids": json.loads(row["resources_json"]),
        }

    @staticmethod
    def _grant_status(conn, row, tenant_id, owner_id):
        if row["revoked_at"]:
            return "revoked"
        if row["expires_at"] is not None and _time(row["expires_at"]) <= datetime.now(
            timezone.utc
        ):
            return "expired"
        try:
            scopes = json.loads(row["scopes_json"])
            resources = json.loads(row["resources_json"])
            _authorize(
                conn,
                row["grant_id"],
                row["source_worker_id"],
                row["target_worker_id"],
                tenant_id,
                owner_id,
                scopes[0],
                resources[0] if resources else "",
            )
        except PeerError:
            return "policy_changed"
        return "active"

    @staticmethod
    def _grant_names(conn, row, tenant_id, owner_id):
        result = {}
        for direction in ("source", "target"):
            member = conn.execute(
                "SELECT name FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",
                (row[direction + "_worker_id"], tenant_id, owner_id),
            ).fetchone()
            result[direction + "_name"] = (
                _redacted(member["name"]) if member else "Unavailable worker"
            )
        return result

    def grants(self, worker_id, *, tenant_id, owner_id):
        with self.store._connect() as conn:
            _worker(conn, worker_id, tenant_id, owner_id)
            return {
                "items": [
                    self._grant_view(x)
                    | {
                        "status": self._grant_status(conn, x, tenant_id, owner_id),
                        **self._grant_names(conn, x, tenant_id, owner_id),
                    }
                    for x in conn.execute(
                        "SELECT * FROM peer_grants WHERE tenant_id=? AND owner_id=? AND (source_worker_id=? OR target_worker_id=?) ORDER BY created_at",
                        (tenant_id, owner_id, worker_id, worker_id),
                    )
                ]
            }

    def revoke(self, grant_id, *, tenant_id, owner_id):
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM peer_grants WHERE grant_id=? AND tenant_id=? AND owner_id=?",
                (grant_id, tenant_id, owner_id),
            ).fetchone()
            if not row:
                raise PeerError("peer_not_found", 404)
            conn.execute(
                "UPDATE peer_grants SET revoked_at=COALESCE(revoked_at,?) WHERE grant_id=?",
                (utc_now(), grant_id),
            )
            invoked = conn.execute(
                "SELECT COUNT(*) FROM peer_messages WHERE grant_id=? AND invoked_at IS NOT NULL",
                (grant_id,),
            ).fetchone()[0]
            return {
                "grant_id": grant_id,
                "revoked": True,
                "already_invoked_messages": invoked,
                "revocation_boundary": "next runtime invocation or peer read; already delivered context cannot be erased",
            }

    def authorize_access(
        self,
        source_worker_id,
        target_worker_id,
        grant_id,
        scope,
        *,
        tenant_id,
        owner_id,
        resource_id="",
    ):
        with self.store._connect() as conn:
            return _authorize(
                conn,
                grant_id,
                source_worker_id,
                target_worker_id,
                tenant_id,
                owner_id,
                scope,
                resource_id,
            )

    def send(
        self,
        worker_id,
        *,
        tenant_id,
        owner_id,
        request: PeerMessageRequest,
        source_run_id="",
        source_attempt_id="",
    ):
        if not request.message.strip():
            raise PeerError("peer_message_empty", 422)
        if _redacted(request.message) != request.message:
            raise PeerError("peer_message_contains_credential", 422)
        spec = _json(request.model_dump() | {"source_run_id": source_run_id})
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _worker(conn, worker_id, tenant_id, owner_id)
            target = _worker(conn, request.target_worker_id, tenant_id, owner_id)
            if source_run_id:
                active_source = conn.execute(
                    "SELECT active_attempt_id FROM runs WHERE run_id=? AND worker_id=? AND state='running'",
                    (source_run_id, worker_id),
                ).fetchone()
                if (
                    not active_source
                    or (active_source["active_attempt_id"] or "") != source_attempt_id
                ):
                    raise PeerError("peer_source_stale", 409)
            permission = _authorize(
                conn,
                request.grant_id,
                worker_id,
                target["worker_id"],
                tenant_id,
                owner_id,
                "message",
            )
            old = conn.execute(
                "SELECT * FROM peer_messages WHERE tenant_id=? AND owner_id=? AND source_worker_id=? AND idempotency_key=?",
                (tenant_id, owner_id, worker_id, request.idempotency_key),
            ).fetchone()
            if old:
                if old["request_json"] != spec:
                    raise PeerError("peer_idempotency_conflict", 409)
                row = dict(old)
            else:
                active = conn.execute(
                    "SELECT * FROM runs WHERE worker_id=? AND state IN ('running','admitted','claimed','queued') ORDER BY queued_at LIMIT 1",
                    (target["worker_id"],),
                ).fetchone()
                if not active:
                    _authorize(
                        conn,
                        request.grant_id,
                        worker_id,
                        target["worker_id"],
                        tenant_id,
                        owner_id,
                        "wake",
                    )
                if request.target_run_id and (
                    not active or active["run_id"] != request.target_run_id
                ):
                    raise PeerError("peer_target_stale", 409)
                if request.reply_to:
                    reply = conn.execute(
                        "SELECT * FROM peer_messages WHERE message_id=? AND tenant_id=? AND owner_id=?",
                        (request.reply_to, tenant_id, owner_id),
                    ).fetchone()
                    if not reply or {
                        reply["source_worker_id"],
                        reply["target_worker_id"],
                    } != {worker_id, target["worker_id"]}:
                        raise PeerError("peer_reply_scope_mismatch", 403)
                default_expiry = (
                    permission["expires_at"]
                    or (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                )
                expires = _future(request.expires_at or default_expiry)
                if permission["expires_at"] is not None and _time(expires) > _time(
                    permission["expires_at"]
                ):
                    raise PeerError("peer_expiry_invalid", 422)
                message_id = "pm_" + uuid.uuid4().hex
                run_id = self.service._idempotent_run_id(
                    target["worker_id"], "peer:" + message_id
                )
                # A typed data envelope, never an operator steer or authority grant.
                instruction = _json(
                    {
                        "kind": "untrusted_peer_message",
                        "version": 1,
                        "message_id": message_id,
                        "sender_worker_id": worker_id,
                        "recipient_worker_id": target["worker_id"],
                        "in_reply_to": request.reply_to,
                        "message": request.message,
                        "content_trust": "untrusted_peer_data",
                        "authority": "none",
                    }
                )
                previous = dict(active) if active else None
                if previous is None and target.get("last_run_id"):
                    last = conn.execute(
                        "SELECT * FROM runs WHERE run_id=? AND worker_id=?",
                        (target["last_run_id"], target["worker_id"]),
                    ).fetchone()
                    previous = dict(last) if last else None
                continuation = (
                    build_workspace_continuation_context(
                        previous_run=previous, continuation_goal=instruction
                    )
                    if previous
                    else None
                )
                if previous:
                    instruction = continuation_instruction(
                        previous_run=previous, continuation_context=continuation
                    )
                row = {
                    "message_id": message_id,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                    "source_worker_id": worker_id,
                    "target_worker_id": target["worker_id"],
                    "source_run_id": source_run_id,
                    "target_run_id": request.target_run_id or "",
                    "grant_id": request.grant_id,
                    "reply_to": request.reply_to,
                    "message": request.message,
                    "instruction": instruction,
                    "delivery_run_id": run_id,
                    "expires_at": expires,
                    "accepted_at": utc_now(),
                    "idempotency_key": request.idempotency_key,
                    "request_json": spec,
                    "continuation_context_json": _json(continuation or {}),
                }
                conn.execute(
                    """INSERT INTO peer_messages (message_id,tenant_id,owner_id,source_worker_id,target_worker_id,source_run_id,target_run_id,grant_id,reply_to,message,instruction,delivery_run_id,expires_at,accepted_at,idempotency_key,request_json,continuation_context_json)
                  VALUES (:message_id,:tenant_id,:owner_id,:source_worker_id,:target_worker_id,:source_run_id,:target_run_id,:grant_id,:reply_to,:message,:instruction,:delivery_run_id,:expires_at,:accepted_at,:idempotency_key,:request_json,:continuation_context_json)""",
                    row,
                )
                row = dict(
                    conn.execute(
                        "SELECT * FROM peer_messages WHERE message_id=?", (message_id,)
                    ).fetchone()
                )
        try:
            self.service.assign_run(
                row["target_worker_id"],
                row["instruction"],
                idempotency_key="peer:" + row["message_id"],
                event_type="peer.message_queued",
                resume_paused_worker=False,
                continuation_context=json.loads(row["continuation_context_json"])
                or None,
            )
        except PeerError:
            raise
        except Exception:  # noqa: BLE001 - provider errors become bounded unavailable receipts.
            # Never persist provider exception text or pretend native delivery happened.
            with self.store._connect() as conn:
                conn.execute(
                    "UPDATE peer_messages SET unavailable_code=? WHERE message_id=?",
                    ("recipient_unavailable", row["message_id"]),
                )
        return self.message(
            row["message_id"], worker_id, tenant_id=tenant_id, owner_id=owner_id
        )

    def _message_view(self, conn, row):
        view = {
            key: row[key]
            for key in (
                "sequence",
                "message_id",
                "source_worker_id",
                "target_worker_id",
                "source_run_id",
                "target_run_id",
                "grant_id",
                "reply_to",
                "delivery_run_id",
                "accepted_at",
                "expires_at",
                "invoked_at",
            )
        }
        try:
            guard_peer_run(conn, row["delivery_run_id"], row["target_worker_id"])
        except PeerError as error:
            view["delivery_state"] = (
                "expired" if error.code == "peer_message_expired" else "revoked"
            )
            view["reason"] = error.code
            view["already_delivered"] = bool(row["invoked_at"])
            return view
        run = conn.execute(
            "SELECT state,runtime_invoked_at FROM runs WHERE run_id=?",
            (row["delivery_run_id"],),
        ).fetchone()
        view["delivery_state"] = (
            "unavailable"
            if not run
            else (
                run["state"]
                if run["state"] in {"failed", "cancelled", "expired", "blocked"}
                else "invoked"
                if run["runtime_invoked_at"]
                else "queued"
            )
        )
        view["run_state"] = run["state"] if run else None
        view["model_read_confirmed"] = False
        view["message"] = row["message"]
        if not run:
            view["reason"] = row["unavailable_code"] or "delivery_not_queued"
        return view

    def message(self, message_id, worker_id, *, tenant_id, owner_id):
        with self.store._connect() as conn:
            _worker(conn, worker_id, tenant_id, owner_id)
            row = conn.execute(
                "SELECT * FROM peer_messages WHERE message_id=? AND tenant_id=? AND owner_id=? AND (source_worker_id=? OR target_worker_id=?)",
                (message_id, tenant_id, owner_id, worker_id, worker_id),
            ).fetchone()
            if not row:
                raise PeerError("peer_not_found", 404)
            return self._message_view(conn, row)

    def messages(self, worker_id, *, tenant_id, owner_id, after=0, limit=50):
        if not 0 <= after or not 1 <= limit <= 200:
            raise PeerError("peer_cursor_invalid", 422)
        with self.store._connect() as conn:
            _worker(conn, worker_id, tenant_id, owner_id)
            rows = conn.execute(
                "SELECT * FROM peer_messages WHERE tenant_id=? AND owner_id=? AND (source_worker_id=? OR target_worker_id=?) AND sequence>? ORDER BY sequence LIMIT ?",
                (tenant_id, owner_id, worker_id, worker_id, after, limit),
            ).fetchall()
            return {
                "items": [self._message_view(conn, row) for row in rows],
                "next_cursor": rows[-1]["sequence"] if rows else after,
            }

    def read_context(
        self, worker_id, target_worker_id, grant_id, run_id, *, tenant_id, owner_id
    ):
        with self.store._connect() as conn:
            _authorize(
                conn,
                grant_id,
                worker_id,
                target_worker_id,
                tenant_id,
                owner_id,
                "context_read",
                run_id,
            )
            row = conn.execute(
                "SELECT run_id,worker_id,state,output_text FROM runs WHERE run_id=? AND worker_id=? AND tenant_id=?",
                (run_id, target_worker_id, tenant_id),
            ).fetchone()
            if not row:
                raise PeerError("peer_not_found", 404)
            # Grant shares one result, never bootstrap, credentials, filesystem or other context.
            return dict(row) | {"output_text": _redacted(row["output_text"])}

    def mint_native_session(
        self, worker_id, run_id, *, lifetime_seconds=3600, purpose="peer"
    ):
        if purpose not in {"peer", "context"}:
            raise PeerError("peer_native_unauthorized", 401)
        if not 1 <= lifetime_seconds <= 3600:
            raise PeerError("peer_expiry_invalid", 422)
        token = secrets.token_urlsafe(32)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT r.*,w.owner_id FROM runs r JOIN workers w ON w.worker_id=r.worker_id WHERE r.run_id=? AND r.worker_id=? AND r.state='running'",
                (run_id, worker_id),
            ).fetchone()
            if not row:
                raise PeerError("peer_source_stale", 409)
            _worker(conn, worker_id, row["tenant_id"], row["owner_id"])
            if purpose == "context" and not self._context_attempt_live(
                conn, row, datetime.now(timezone.utc)
            ):
                raise PeerError("peer_native_unauthorized", 401)
            self._clean_context_sessions(conn, datetime.now(timezone.utc))
            conn.execute(
                "DELETE FROM peer_native_sessions WHERE purpose='peer' AND expires_at<=?",
                (utc_now(),),
            )
            conn.execute(
                "INSERT INTO peer_native_sessions(token_sha256,worker_id,run_id,attempt_id,expires_at,purpose) VALUES (?,?,?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    worker_id,
                    run_id,
                    row["active_attempt_id"] or "",
                    (
                        datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)
                    ).isoformat(),
                    purpose,
                ),
            )
        return token

    @staticmethod
    def _context_attempt_live(conn, row, now):
        """The runtime's exact invoked attempt and fresh admission lease own liveness."""
        attempt = row["active_attempt_id"]
        lease = conn.execute(
            """SELECT l.expires_at FROM run_attempts a
            JOIN host_run_leases l ON l.lease_id=a.lease_id AND l.attempt_id=a.attempt_id
            WHERE a.attempt_id=? AND a.run_id=? AND a.state='running'
            AND a.runtime_invoked_at IS NOT NULL AND a.ended_at IS NULL
            AND l.run_id=a.run_id AND l.worker_id=? AND l.tenant_id=? AND l.owner_id=?
            AND l.status='active' AND l.released_at IS NULL""",
            (
                attempt,
                row["run_id"],
                row["worker_id"],
                row["tenant_id"],
                row["owner_id"],
            ),
        ).fetchone()
        return bool(lease and _time(lease["expires_at"]) > now)

    def _clean_context_sessions(self, conn, now):
        rows = conn.execute(
            """SELECT s.*,r.state,r.active_attempt_id,r.tenant_id,w.owner_id,w.state AS worker_state
            FROM peer_native_sessions s JOIN runs r ON r.run_id=s.run_id
            JOIN workers w ON w.worker_id=s.worker_id WHERE s.purpose='context'"""
        ).fetchall()
        for row in rows:
            if (
                row["state"] != "running"
                or row["worker_state"] in CLOSED_WORKER_STATES
                or row["active_attempt_id"] != row["attempt_id"]
                or not self._context_attempt_live(conn, row, now)
            ):
                conn.execute(
                    "DELETE FROM peer_native_sessions WHERE token_sha256=?",
                    (row["token_sha256"],),
                )

    def native_principal(self, token, *, purpose="peer"):
        if (
            purpose not in {"peer", "context"}
            or not isinstance(token, str)
            or not 20 <= len(token) <= 128
        ):
            raise PeerError("peer_native_unauthorized", 401)
        principal = None
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            self._clean_context_sessions(conn, now)
            row = conn.execute(
                """SELECT s.*,r.state,r.active_attempt_id,r.tenant_id,w.owner_id,w.state AS worker_state
                FROM peer_native_sessions s JOIN runs r ON r.run_id=s.run_id AND r.worker_id=s.worker_id
                JOIN workers w ON w.worker_id=s.worker_id WHERE s.token_sha256=?""",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            valid = (
                row
                and row["state"] == "running"
                and row["active_attempt_id"] == row["attempt_id"]
                and row["worker_state"] not in CLOSED_WORKER_STATES
            )
            if valid and row["purpose"] == purpose:
                if purpose == "context":
                    # The credential covers only this live invocation. Extend the finite
                    # record on demand, including after idle expiry, only under a fresh lease.
                    conn.execute(
                        "UPDATE peer_native_sessions SET expires_at=? WHERE token_sha256=?",
                        ((now + timedelta(hours=1)).isoformat(), row["token_sha256"]),
                    )
                elif _time(row["expires_at"]) <= now:
                    valid = False
                if valid:
                    principal = {
                        key: row[key]
                        for key in (
                            "tenant_id",
                            "owner_id",
                            "worker_id",
                            "run_id",
                            "attempt_id",
                        )
                    }
        # Raise after the cleanup transaction commits; failed reads cannot roll back revocation.
        if principal is None:
            raise PeerError("peer_native_unauthorized", 401)
        return principal

    def recover_pending(self, limit=100):
        """Replay accepted envelopes on the existing startup recovery lane."""
        if not 1 <= limit <= 1000:
            raise PeerError("peer_recovery_limit_invalid", 422)
        results = []
        after = 0
        while True:
            with self.store._connect() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT m.* FROM peer_messages m LEFT JOIN runs r ON r.run_id=m.delivery_run_id WHERE r.run_id IS NULL AND m.sequence>? ORDER BY m.sequence LIMIT ?",
                        (after, limit),
                    )
                ]
            if not rows:
                return results
            for row in rows:
                try:
                    with self.store._connect() as conn:
                        guard_peer_run(
                            conn, row["delivery_run_id"], row["target_worker_id"]
                        )
                    self.service.assign_run(
                        row["target_worker_id"],
                        row["instruction"],
                        idempotency_key="peer:" + row["message_id"],
                        event_type="peer.message_queued",
                        resume_paused_worker=False,
                        continuation_context=json.loads(
                            row["continuation_context_json"]
                        )
                        or None,
                    )
                    results.append(
                        {"message_id": row["message_id"], "status": "queued"}
                    )
                except PeerError as error:
                    results.append(
                        {
                            "message_id": row["message_id"],
                            "status": "blocked",
                            "reason": error.code,
                        }
                    )
                except Exception:  # noqa: BLE001 - provider errors become bounded unavailable receipts.
                    results.append(
                        {"message_id": row["message_id"], "status": "unavailable"}
                    )
            after = rows[-1]["sequence"]
            if len(rows) < limit:
                return results

    def project_native_tools(self, worker, run):
        """Return a run-local tool binding; no secret or bundle enters durable worker state."""
        import os
        from urllib.parse import urlparse

        with self.store._connect() as conn:
            stored = _worker(
                conn, worker["worker_id"], worker["tenant_id"], worker["owner_id"]
            )
            policy = _policy(
                conn, stored["workspace_id"], worker["tenant_id"], worker["owner_id"]
            )
        if policy["discovery"] == "off" and not policy["access_enabled"]:
            return worker
        endpoint = (
            os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "").strip().rstrip("/")
        )
        if not endpoint:
            raise PeerError("peer_native_endpoint_unavailable", 503)
        parsed = urlparse(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise PeerError("peer_native_endpoint_invalid", 503)
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
            "host.docker.internal",
        }:
            raise PeerError("peer_native_endpoint_requires_tls", 503)
        token = self.mint_native_session(worker["worker_id"], run["run_id"])
        return {
            **worker,
            "_peer_native_projection": {
                "worker_id": worker["worker_id"],
                "run_id": run["run_id"],
                "url": endpoint + "/v1/native/peers/",
                "token": token,
            },
        }


def project_peer_bootstrap(worker, bundle):
    """Merge only the private run binder's structured MCP config after other grants."""
    projection = worker.get("_peer_native_projection")
    if not isinstance(projection, dict):
        return bundle
    if projection.get("worker_id") != worker.get("worker_id"):
        raise PeerError("peer_native_projection_mismatch")
    active_run = worker.get("_active_run_id")
    if active_run and projection.get("run_id") != active_run:
        raise PeerError("peer_native_projection_mismatch")
    result = json.loads(json.dumps(bundle))
    result.setdefault("env", {})["GLASSHIVE_PEER_TOKEN"] = projection["token"]
    mcp = result.get("claude_project_mcp") or {}
    servers = mcp.get("mcpServers", mcp)
    servers["xperfect-peers"] = {
        "type": "http",
        "url": projection["url"],
        "headers": {"Authorization": "Bearer ${GLASSHIVE_PEER_TOKEN}"},
    }
    result["claude_project_mcp"] = {"mcpServers": servers}
    from .bootstrap import _strip_codex_mcp_server_blocks

    append = _strip_codex_mcp_server_blocks(
        result.get("codex_config_append", ""), {"xperfect-peers"}
    ).rstrip()
    # Fixed server ID and typed URL; no prompt or intent routing.
    peer_config = (
        "[mcp_servers.xperfect-peers]\nurl = "
        + json.dumps(projection["url"])
        + '\nbearer_token_env_var = "GLASSHIVE_PEER_TOKEN"\n'
    )
    result["codex_config_append"] = append + "\n\n" + peer_config
    return result
