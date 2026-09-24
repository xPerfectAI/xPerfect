"""Explicit current-member permission batches; no future-member authority."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import CLOSED_WORKER_STATES, utc_now
from .peer_collaboration import (
    PeerError,
    PeerGrantRequest,
    _future,
    _json,
    _policy,
    _redacted,
    _worker,
)


class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workspace_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    member_ids: list[str] = Field(min_length=1, max_length=100)


class PeerGrantBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_worker_id: str = Field(min_length=1, max_length=128)
    workspaces: list[WorkspaceSnapshot] = Field(min_length=1, max_length=101)
    target_worker_ids: list[str] = Field(min_length=1, max_length=100)
    direction: Literal["send", "receive", "both"]
    wake: bool = False
    enable_access: bool
    expires_at: str | None
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.target_worker_ids)) != len(self.target_worker_ids):
            raise ValueError("Targets must be unique")
        if len({item.workspace_id for item in self.workspaces}) != len(self.workspaces):
            raise ValueError("Workspaces must be unique")
        for item in self.workspaces:
            if len(set(item.member_ids)) != len(item.member_ids):
                raise ValueError("Members must be unique")
        return self


def preserve_v1_grants(conn):
    """Rebuild the referenced pair inside Store's existing migration transaction."""
    columns = conn.execute("PRAGMA table_info(peer_grants)").fetchall()
    if not any(row[1] == "expires_at" and row[3] for row in columns):
        return None
    if not conn.in_transaction:
        raise RuntimeError("Peer migration requires the owning transaction")
    # Keep the AUTOINCREMENT high-water mark even when earlier messages were deleted.
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='peer_messages'"
    ).fetchone()
    conn.execute("CREATE TEMP TABLE peer_grants_v1 AS SELECT * FROM peer_grants")
    conn.execute("CREATE TEMP TABLE peer_messages_v1 AS SELECT * FROM peer_messages")
    conn.execute("DROP TABLE peer_messages")
    conn.execute("DROP TABLE peer_grants")
    return int(sequence[0]) if sequence else 0


def restore_v1_grants(conn, sequence):
    if sequence is None:
        return
    conn.execute("INSERT INTO peer_grants SELECT * FROM peer_grants_v1")
    conn.execute("INSERT INTO peer_messages SELECT * FROM peer_messages_v1")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='peer_messages'")
    conn.execute(
        "INSERT INTO sqlite_sequence(name,seq) VALUES ('peer_messages',?)", (sequence,)
    )
    conn.execute("DROP TABLE peer_messages_v1")
    conn.execute("DROP TABLE peer_grants_v1")
    if conn.execute("PRAGMA foreign_key_check(peer_messages)").fetchone():
        raise RuntimeError("Peer migration failed referential integrity")


def _members(conn, workspace_id, tenant_id, owner_id):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT worker_id,name,state FROM workers WHERE workspace_id=? AND tenant_id=? AND owner_id=? ORDER BY worker_id LIMIT 101",
            (workspace_id, tenant_id, owner_id),
        )
        if row["state"] not in CLOSED_WORKER_STATES
    ]


def access_options(peers, source_worker_id, *, tenant_id, owner_id):
    with peers.store._connect() as conn:
        source = _worker(conn, source_worker_id, tenant_id, owner_id)
        # This catalog is for the authenticated owner, never the native discovery tool.
        closed_states = tuple(sorted(CLOSED_WORKER_STATES))
        closed_slots = ",".join("?" for _ in closed_states)
        items = []
        for workspace in conn.execute(
            f"""SELECT w.workspace_id,w.default_worker_id FROM execution_workspaces w
            WHERE w.tenant_id=? AND w.owner_id=? AND EXISTS (
              SELECT 1 FROM workers m WHERE m.workspace_id=w.workspace_id
                AND m.tenant_id=w.tenant_id AND m.owner_id=w.owner_id
                AND m.state NOT IN ({closed_slots})
            ) ORDER BY w.workspace_id LIMIT 102""",
            (tenant_id, owner_id, *closed_states),
        ):
            members = _members(conn, workspace["workspace_id"], tenant_id, owner_id)
            if not members:
                continue
            if len(items) >= 101 or len(members) > 100:
                raise PeerError("peer_selection_too_large", 422)
            policy = _policy(conn, workspace["workspace_id"], tenant_id, owner_id)
            items.append(
                {
                    "workspace_id": workspace["workspace_id"],
                    "revision": policy["revision"],
                    "access_enabled": policy["access_enabled"],
                    "name": _redacted(
                        next(
                            (
                                m["name"]
                                for m in members
                                if m["worker_id"] == workspace["default_worker_id"]
                            ),
                            members[0]["name"],
                        )
                    ),
                    "members": [
                        {"worker_id": m["worker_id"], "name": _redacted(m["name"])}
                        for m in members
                    ],
                }
            )
        return {
            "source_worker_id": source_worker_id,
            "source_workspace_id": source["workspace_id"],
            "workspaces": items,
            "max_targets": 100,
            "includes_future_members": False,
        }


def grant_batch(peers, request: PeerGrantBatchRequest, *, tenant_id, owner_id):
    spec = _json(request.model_dump())
    with peers.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = _worker(conn, request.source_worker_id, tenant_id, owner_id)
        old = conn.execute(
            "SELECT * FROM peer_grant_batches WHERE tenant_id=? AND owner_id=? AND idempotency_key=?",
            (tenant_id, owner_id, request.idempotency_key),
        ).fetchone()
        if old:
            if old["request_json"] != spec:
                raise PeerError("peer_idempotency_conflict", 409)
            ids = json.loads(old["grant_ids_json"])
        else:
            targets = [
                _worker(conn, target, tenant_id, owner_id)
                for target in request.target_worker_ids
            ]
            if request.source_worker_id in request.target_worker_ids:
                raise PeerError("peer_grant_invalid", 422)
            needed = {
                source["workspace_id"],
                *(target["workspace_id"] for target in targets),
            }
            if {item.workspace_id for item in request.workspaces} != needed:
                raise PeerError("peer_snapshot_stale", 409)
            policies = {}
            for item in request.workspaces:
                policy = _policy(conn, item.workspace_id, tenant_id, owner_id)
                members = _members(conn, item.workspace_id, tenant_id, owner_id)
                if policy["revision"] != item.revision or {
                    m["worker_id"] for m in members
                } != set(item.member_ids):
                    raise PeerError("peer_snapshot_stale", 409)
                if not policy["access_enabled"]:
                    if not request.enable_access:
                        raise PeerError("peer_access_denied")
                    conn.execute(
                        """INSERT INTO peer_policies(workspace_id,access_enabled) VALUES (?,1)
                      ON CONFLICT(workspace_id) DO UPDATE SET access_enabled=1""",
                        (item.workspace_id,),
                    )
                    conn.execute(
                        "UPDATE execution_workspaces SET policy_revision=policy_revision+1,updated_at=? WHERE workspace_id=?",
                        (utc_now(), item.workspace_id),
                    )
                    policy = _policy(conn, item.workspace_id, tenant_id, owner_id)
                policies[item.workspace_id] = policy
            ids = []
            pairs = []
            for target in targets:
                if request.direction in {"send", "both"}:
                    pairs.append((source, target))
                if request.direction in {"receive", "both"}:
                    pairs.append((target, source))
            for index, (sender, target) in enumerate(pairs):
                scopes = ["message", "wake"] if request.wake else ["message"]
                expiry = (
                    _future(request.expires_at)
                    if request.expires_at is not None
                    else None
                )
                existing = conn.execute(
                    """SELECT * FROM peer_grants WHERE tenant_id=? AND owner_id=?
                    AND source_worker_id=? AND target_worker_id=? AND source_workspace_id=? AND target_workspace_id=?
                    AND source_revision=? AND target_revision=? AND scopes_json=? AND resources_json='[]'
                    AND expires_at IS ? AND revoked_at IS NULL ORDER BY created_at LIMIT 1""",
                    (
                        tenant_id,
                        owner_id,
                        sender["worker_id"],
                        target["worker_id"],
                        sender["workspace_id"],
                        target["workspace_id"],
                        policies[sender["workspace_id"]]["revision"],
                        policies[target["workspace_id"]]["revision"],
                        _json(scopes),
                        expiry,
                    ),
                ).fetchone()
                if existing is not None:
                    ids.append(existing["grant_id"])
                    continue
                permission = peers._grant(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    request=PeerGrantRequest(
                        source_worker_id=sender["worker_id"],
                        target_worker_id=target["worker_id"],
                        source_revision=policies[sender["workspace_id"]]["revision"],
                        target_revision=policies[target["workspace_id"]]["revision"],
                        scopes=["message", "wake"] if request.wake else ["message"],
                        expires_at=request.expires_at,
                        idempotency_key="batch:"
                        + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
                        + ":"
                        + str(index),
                    ),
                )
                ids.append(permission["grant_id"])
            conn.execute(
                "INSERT INTO peer_grant_batches VALUES (?,?,?,?,?)",
                (tenant_id, owner_id, request.idempotency_key, spec, _json(ids)),
            )
        items = []
        for grant_id in ids:
            row = conn.execute(
                "SELECT * FROM peer_grants WHERE grant_id=? AND tenant_id=? AND owner_id=?",
                (grant_id, tenant_id, owner_id),
            ).fetchone()
            if row:
                items.append(
                    peers._grant_view(row)
                    | {"status": peers._grant_status(conn, row, tenant_id, owner_id)}
                )
        return {"items": items, "includes_future_members": False}
