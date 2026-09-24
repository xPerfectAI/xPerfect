"""Permission lifetime, exact roster batch and migration causal contracts."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from test_peer_collaboration import enable, grant
from test_peer_collaboration import peers as peer_fixture
from workers_projects_runtime import peer_collaboration as mod
from workers_projects_runtime.peer_permissions import (
    PeerGrantBatchRequest,
    access_options,
    grant_batch,
)
from workers_projects_runtime.schema_version import begin_schema_migration

OWNER = {"tenant_id": "tenant-a", "owner_id": "owner-a"}


@pytest.fixture
def peers(tmp_path):
    yield from peer_fixture.__wrapped__(tmp_path)


def batch(service, a, targets, **changes):
    options = access_options(service, a["worker_id"], **OWNER)
    wanted = {a["worker_id"], *(w["worker_id"] for w in targets)}
    data = {
        "source_worker_id": a["worker_id"],
        "target_worker_ids": [w["worker_id"] for w in targets],
        "workspaces": [
            {
                "workspace_id": w["workspace_id"],
                "revision": w["revision"],
                "member_ids": [m["worker_id"] for m in w["members"]],
            }
            for w in options["workspaces"]
            if any(m["worker_id"] in wanted for m in w["members"])
        ],
        "direction": "both",
        "wake": True,
        "enable_access": True,
        "expires_at": None,
        "idempotency_key": "batch-synthetic",
    }
    data.update(changes)
    return PeerGrantBatchRequest(**data)


def authority(service, a, b, g):
    return service.authorize_access(
        a["worker_id"], b["worker_id"], g["grant_id"], "message", **OWNER
    )


def test_persistent_grant_survives_time_and_noop_policy_save(peers, monkeypatch):
    service, workers = enable(peers)
    a, b = workers[:2]
    permission = grant(service, a, b, expires_at=None)
    future = datetime.now(timezone.utc) + timedelta(days=31)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(mod, "datetime", Clock)
    assert authority(service, a, b, permission)
    policy = service.set_policy(
        a["workspace_id"],
        **OWNER,
        request=mod.PeerPolicyUpdate(
            expected_revision=2, discovery="account", access_enabled=True
        ),
    )
    assert policy["revision"] == 2
    assert authority(service, a, b, permission)
    assert service.grants(a["worker_id"], **OWNER)["items"][0]["status"] == "active"
    service.set_policy(
        a["workspace_id"],
        **OWNER,
        request=mod.PeerPolicyUpdate(
            expected_revision=2, discovery="off", access_enabled=False
        ),
    )
    with pytest.raises(mod.PeerError, match="peer_access_denied"):
        authority(service, a, b, permission)
    assert (
        service.grants(a["worker_id"], **OWNER)["items"][0]["status"]
        == "policy_changed"
    )


def test_finite_grants_remain_finite_and_expiry_must_be_explicit(peers, monkeypatch):
    service, workers = enable(peers)
    a, b = workers[:2]
    permission = grant(service, a, b)
    payload = {
        "source_worker_id": a["worker_id"],
        "target_worker_id": b["worker_id"],
        "scopes": ["message"],
        "source_revision": 2,
        "target_revision": 2,
        "idempotency_key": "missing",
    }
    with pytest.raises(ValidationError):
        mod.PeerGrantRequest(**payload)
    future = datetime.now(timezone.utc) + timedelta(hours=2)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(mod, "datetime", Clock)
    with pytest.raises(mod.PeerError):
        authority(service, a, b, permission)
    assert service.grants(a["worker_id"], **OWNER)["items"][0]["status"] == "expired"


def test_persistent_grant_message_deadline_and_revocation_stay_finite(peers):
    service, workers = enable(peers)
    a, b = workers[:2]
    permission = grant(service, a, b, expires_at=None)
    sent = service.send(
        a["worker_id"],
        **OWNER,
        request=mod.PeerMessageRequest(
            target_worker_id=b["worker_id"],
            grant_id=permission["grant_id"],
            message="Synthetic",
            idempotency_key="one",
        ),
    )
    with service.store._connect() as conn:
        expiry = conn.execute(
            "SELECT expires_at FROM peer_messages WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()[0]
        assert (
            datetime.now(timezone.utc)
            < datetime.fromisoformat(expiry)
            <= datetime.now(timezone.utc) + timedelta(days=30)
        )
    service.revoke(permission["grant_id"], **OWNER)
    with service.store._connect() as conn, pytest.raises(mod.PeerError):
        mod.guard_peer_run(conn, sent["delivery_run_id"], b["worker_id"])
    assert not service.messages(b["worker_id"], **OWNER)["items"][0].get("message")


def test_native_token_expiry_and_attempt_unchanged_by_persistent_grant(peers):
    service, workers = enable(peers)
    a, b = workers[:2]
    permission = grant(service, a, b, expires_at=None)
    run = service.store.create_run(a["worker_id"], a["project_id"], "Synthetic")
    with service.store._connect() as conn:
        conn.execute(
            "UPDATE runs SET state='running',active_attempt_id='attempt-a' WHERE run_id=?",
            (run["run_id"],),
        )
    token = service.mint_native_session(a["worker_id"], run["run_id"])
    assert service.native_principal(token)["worker_id"] == a["worker_id"]
    with pytest.raises(mod.PeerError):
        service.mint_native_session(
            a["worker_id"], run["run_id"], lifetime_seconds=3601
        )
    with service.store._connect() as conn:
        conn.execute(
            "UPDATE peer_native_sessions SET expires_at=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
    with pytest.raises(mod.PeerError, match="peer_native_unauthorized"):
        service.native_principal(token)
    assert authority(service, a, b, permission)
    token = service.mint_native_session(a["worker_id"], run["run_id"])
    with service.store._connect() as conn:
        conn.execute(
            "UPDATE runs SET active_attempt_id='attempt-b' WHERE run_id=?",
            (run["run_id"],),
        )
    with pytest.raises(mod.PeerError, match="peer_native_unauthorized"):
        service.native_principal(token)


@pytest.mark.parametrize("direction,count", [("send", 1), ("receive", 1), ("both", 2)])
def test_batch_is_explicit_direction_and_never_future_members(peers, direction, count):
    service, workers, _ = peers
    a, b, c = workers
    req = batch(service, a, [b], direction=direction, wake=False)
    result = grant_batch(service, req, **OWNER)
    assert len(result["items"]) == count
    assert all(
        x["scopes"] == ["message"]
        and x["expires_at"] is None
        and x["status"] == "active"
        for x in result["items"]
    )
    assert {x["source_worker_id"] for x in result["items"]} == (
        {a["worker_id"]}
        if direction == "send"
        else {b["worker_id"]}
        if direction == "receive"
        else {a["worker_id"], b["worker_id"]}
    )
    assert service.policy(c["workspace_id"], **OWNER)["access_enabled"] is False
    assert service.policy(a["workspace_id"], **OWNER)["discovery"] == "off"
    assert service.grants(c["worker_id"], **OWNER)["items"] == []
    assert grant_batch(service, req, **OWNER) == result
    for g in result["items"]:
        service.revoke(g["grant_id"], **OWNER)
    assert all(
        x["status"] == "revoked" for x in grant_batch(service, req, **OWNER)["items"]
    )


def test_future_member_and_wrong_owner_never_gain_batch_permission(peers):
    service, workers, _ = peers
    a, b, _c = workers
    req = batch(service, a, [b])
    grant_batch(service, req, **OWNER)
    fresh = service.store.create_worker(
        project_id=a["project_id"],
        owner_id="owner-a",
        tenant_id="tenant-a",
        name="New member",
        role="worker",
        profile="codex-cli",
        backend="test",
        runtime="stub",
        model="test",
        execution_mode="host",
    )
    assert service.grants(fresh["worker_id"], **OWNER)["items"] == []
    with pytest.raises(mod.PeerError):
        access_options(
            service, a["worker_id"], tenant_id="tenant-a", owner_id="other-owner"
        )
    with pytest.raises(mod.PeerError):
        grant_batch(service, req, tenant_id="other-tenant", owner_id="owner-a")
    changed = batch(service, a, [b], idempotency_key="fresh-key")
    assert len(grant_batch(service, changed, **OWNER)["items"]) == 2
    assert service.grants(fresh["worker_id"], **OWNER)["items"] == []


def test_closed_workspace_history_does_not_hide_current_peer_choices(peers):
    service, workers, _ = peers
    for index in range(99):
        closed = service.store.create_worker(
            project_id=workers[0]["project_id"],
            owner_id="owner-a",
            tenant_id="tenant-a",
            name=f"Closed {index}",
            role="worker",
            profile="codex-cli",
            backend="test",
            runtime="stub",
            model="test",
            execution_mode="host",
        )
        with service.store._connect() as conn:
            conn.execute(
                "UPDATE workers SET state='terminated' WHERE worker_id=?",
                (closed["worker_id"],),
            )
    options = access_options(service, workers[0]["worker_id"], **OWNER)
    assert len(options["workspaces"]) == 3
    assert {
        member["worker_id"]
        for workspace in options["workspaces"]
        for member in workspace["members"]
    } == {worker["worker_id"] for worker in workers}


def test_batch_idempotency_payload_change_cannot_expand_authority(peers):
    service, workers, _ = peers
    a, b, _c = workers
    req = batch(service, a, [b], wake=False)
    grant_batch(service, req, **OWNER)
    with pytest.raises(mod.PeerError, match="peer_idempotency_conflict"):
        grant_batch(service, req.model_copy(update={"wake": True}), **OWNER)
    assert all(
        g["scopes"] == ["message"]
        for g in service.grants(a["worker_id"], **OWNER)["items"]
    )
    with pytest.raises(ValidationError):
        PeerGrantBatchRequest(**(req.model_dump() | {"scopes": ["control"]}))


def test_message_deadline_expires_under_still_active_persistent_permission(peers):
    service, workers = enable(peers)
    a, b = workers[:2]
    permission = grant(service, a, b, expires_at=None)
    sent = service.send(
        a["worker_id"],
        **OWNER,
        request=mod.PeerMessageRequest(
            target_worker_id=b["worker_id"],
            grant_id=permission["grant_id"],
            message="Synthetic",
            idempotency_key="expiring",
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
        ),
    )
    with service.store._connect() as conn:
        conn.execute(
            "UPDATE peer_messages SET expires_at=? WHERE message_id=?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                sent["message_id"],
            ),
        )
        with pytest.raises(mod.PeerError, match="peer_message_expired"):
            mod.guard_peer_run(conn, sent["delivery_run_id"], b["worker_id"])
    assert authority(service, a, b, permission)


@pytest.mark.parametrize(
    "failure", ["revision", "roster", "owner", "enable", "expiry", "extra_workspace"]
)
def test_invalid_batch_rolls_back_every_policy_and_grant(peers, failure):
    service, workers, _ = peers
    a, b, c = workers
    req = batch(service, a, [b])
    data = req.model_dump()
    if failure == "revision":
        data["workspaces"][-1]["revision"] = 999
    if failure == "roster":
        data["workspaces"][-1]["member_ids"].append("new-member")
    if failure == "owner":
        data["target_worker_ids"] = ["foreign-worker"]
    if failure == "enable":
        data["enable_access"] = False
    if failure == "expiry":
        data["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
    if failure == "extra_workspace":
        data["workspaces"].append(
            {
                "workspace_id": c["workspace_id"],
                "revision": 1,
                "member_ids": [c["worker_id"]],
            }
        )
    with pytest.raises(mod.PeerError):
        grant_batch(service, PeerGrantBatchRequest(**data), **OWNER)
    for w in workers:
        assert service.policy(w["workspace_id"], **OWNER)["revision"] == 1
    with service.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM peer_grants").fetchone()[0] == 0


def test_v1_migration_preserves_finite_rows_fk_and_sequence(tmp_path, monkeypatch):
    # Frozen pre-migration v1 DDL; changing production DDL does not change this fixture.
    sql = """
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
      scopes_json TEXT NOT NULL, resources_json TEXT NOT NULL, expires_at TEXT NOT NULL,
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
    """
    conn = sqlite3.connect(tmp_path / "migration.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        "CREATE TABLE execution_workspaces(workspace_id TEXT PRIMARY KEY); CREATE TABLE workers(worker_id TEXT PRIMARY KEY); CREATE TABLE runs(run_id TEXT PRIMARY KEY);"
    )
    # Core runtime tables predate peer v1; context revocation uses their
    # existing lifecycle columns when the peer schema is upgraded.
    conn.executescript("""
      ALTER TABLE workers ADD COLUMN state TEXT;
      ALTER TABLE runs ADD COLUMN state TEXT;
      ALTER TABLE runs ADD COLUMN active_attempt_id TEXT;
      CREATE TABLE run_attempts(attempt_id TEXT PRIMARY KEY,state TEXT,ended_at TEXT);
      CREATE TABLE host_run_leases(run_id TEXT,status TEXT,released_at TEXT,attempt_id TEXT);
    """)
    conn.executescript(sql)
    conn.execute("INSERT INTO workers(worker_id) VALUES ('a')")
    conn.execute("INSERT INTO workers(worker_id) VALUES ('b')")
    conn.execute(
        "INSERT INTO peer_grants VALUES ('g','t','o','a','b','wa','wb',2,2,'[\"message\"]','[]','2026-01-01T00:00:00+00:00',NULL,'created','key','original request')"
    )
    conn.execute(
        "INSERT INTO peer_messages(sequence,message_id,tenant_id,owner_id,source_worker_id,target_worker_id,grant_id,message,instruction,delivery_run_id,expires_at,accepted_at,idempotency_key,request_json) VALUES (7,'m','t','o','a','b','g','payload','instruction','r','2026-01-01T00:00:00+00:00','accepted','mk','message request')"
    )
    before = [tuple(row) for row in conn.execute("SELECT * FROM peer_messages")]
    conn.execute("UPDATE sqlite_sequence SET seq=12 WHERE name='peer_messages'")
    conn.commit()
    execute_schema = mod.execute_schema_script

    def fail_after_old_tables_are_preserved(*_args):
        raise sqlite3.OperationalError("synthetic migration interruption")

    monkeypatch.setattr(
        mod, "execute_schema_script", fail_after_old_tables_are_preserved
    )
    with (
        pytest.raises(
            sqlite3.OperationalError, match="synthetic migration interruption"
        ),
        conn,
    ):
        begin_schema_migration(conn)
        mod.ensure_peer_schema(conn)
    assert [tuple(row) for row in conn.execute("SELECT * FROM peer_messages")] == before
    assert (
        next(
            row
            for row in conn.execute("PRAGMA table_info(peer_grants)")
            if row[1] == "expires_at"
        )[3]
        == 1
    )
    monkeypatch.setattr(mod, "execute_schema_script", execute_schema)
    begin_schema_migration(conn)
    mod.ensure_peer_schema(conn)
    conn.commit()
    assert [tuple(row) for row in conn.execute("SELECT * FROM peer_messages")] == before
    row = conn.execute("SELECT * FROM peer_grants").fetchone()
    assert (
        row["expires_at"] == "2026-01-01T00:00:00+00:00"
        and row["request_json"] == "original request"
    )
    assert (
        conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='peer_messages'"
        ).fetchone()[0]
        == 12
    )
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("UPDATE peer_grants SET expires_at=NULL WHERE grant_id='g'")
    conn.commit()
    begin_schema_migration(conn)
    mod.ensure_peer_schema(conn)
    conn.commit()
    assert conn.execute("SELECT expires_at FROM peer_grants").fetchone()[0] is None
    conn.close()


def test_repeating_group_action_reuses_identical_active_permission(peers):
    service, workers, _ = peers
    a, b, _c = workers
    first = grant_batch(service, batch(service, a, [b]), **OWNER)
    second = grant_batch(
        service, batch(service, a, [b], idempotency_key="another-owner-click"), **OWNER
    )
    assert first == second
    assert len(service.grants(a["worker_id"], **OWNER)["items"]) == 2
