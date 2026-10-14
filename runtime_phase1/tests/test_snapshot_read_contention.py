from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

import workers_projects_runtime.signed_links as links
from workers_projects_runtime.store import Store
from test_host_run_leases import _active_worker_and_run, _expire_lease


@pytest.mark.parametrize("running", [False, True])
def test_healthy_running_reconciliation_does_not_wait_for_unrelated_writer(
    tmp_path, monkeypatch, running
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    if running:
        _active_worker_and_run(store, "healthy-snapshot", run_state="running")
    original_connect = store._connect
    statements = []

    @contextmanager
    def bounded_connect():
        with original_connect() as conn:
            conn.execute("PRAGMA busy_timeout=20")
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(store, "_connect", bounded_connect)
    writer = sqlite3.connect(tmp_path / "runtime.sqlite3")
    writer.execute("BEGIN IMMEDIATE")
    try:
        assert store.reconcile_invalid_running_runs() == 0
        assert not any("BEGIN IMMEDIATE" in statement for statement in statements)
        assert not any("SELECT * FROM runs" in statement for statement in statements)
    finally:
        writer.rollback()
        writer.close()
        store.close()


def test_invalid_running_reconciliation_keeps_exact_corrective_transaction(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    try:
        _project, worker, run = _active_worker_and_run(
            store, "expired-snapshot", run_state="running"
        )
        _expire_lease(store, run["run_id"])
        assert store.reconcile_invalid_running_runs() == 1
        assert store.get_run(run["run_id"])["state"] == "queued"
        assert store.get_worker(worker["worker_id"])["state"] == "starting"
    finally:
        store.close()


def test_invalid_read_snapshot_is_rechecked_after_writer_reservation(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    _project, _worker, run = _active_worker_and_run(
        store, "renewed-snapshot", run_state="running"
    )
    lease = _expire_lease(store, run["run_id"])
    original_connect = store._connect
    renewed = []

    class ConnectionAtWriteBoundary:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE" and not renewed:
                with sqlite3.connect(tmp_path / "runtime.sqlite3") as other:
                    other.execute(
                        "UPDATE host_run_leases SET expires_at = ? WHERE lease_id = ?",
                        ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), lease["lease_id"]),
                    )
                renewed.append(True)
            return self.conn.execute(sql, parameters)

    @contextmanager
    def boundary_connect():
        with original_connect() as conn:
            yield ConnectionAtWriteBoundary(conn)

    monkeypatch.setattr(store, "_connect", boundary_connect)
    try:
        assert store.reconcile_invalid_running_runs() == 0
        assert renewed == [True]
        assert store.get_run(run["run_id"])["state"] == "running"
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["worker_view", "artifact_download"])
def test_valid_scoped_link_reuse_does_not_wait_for_unrelated_writer(
    tmp_path, monkeypatch, kind
):
    state = tmp_path / "links.sqlite3"
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(state))
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-secret")
    token = links.sign_link_token(
        kind=kind, worker_id="wrk_0123456789", tenant_id="synthetic",
        owner_id="synthetic-owner", path="output.txt",
    )
    ref = links.create_signed_link_ref(token=token)
    original_connect = sqlite3.connect
    statements = []

    def bounded_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.execute("PRAGMA busy_timeout=20")
        conn.set_trace_callback(statements.append)
        return conn

    writer = original_connect(state)
    writer.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(links.sqlite3, "connect", bounded_connect)
    try:
        assert links.create_signed_link_ref(token=token) == ref
        assert not any("DELETE FROM signed_link_refs" in statement for statement in statements)
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("expiry", ["ref", "worker_view_token"])
def test_expired_link_reuse_keeps_cleanup_and_fresh_token(tmp_path, monkeypatch, expiry):
    state = tmp_path / "links.sqlite3"
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(state))
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-secret")
    clock = [1000]
    monkeypatch.setattr(links.time, "time", lambda: clock[0])
    kwargs = dict(kind="worker_view", worker_id="wrk_0123456789", tenant_id="synthetic", owner_id="synthetic-owner")
    token = links.sign_link_token(**kwargs, ttl_seconds=60)
    ref = links.create_signed_link_ref(token=token)
    if expiry == "ref":
        with sqlite3.connect(state) as conn:
            conn.execute("UPDATE signed_link_refs SET expires_at = 999 WHERE ref_id = ?", (ref,))
    else:
        clock[0] = 1061
    fresh = links.sign_link_token(**kwargs, ttl_seconds=60)
    replacement = links.create_signed_link_ref(token=fresh)
    assert replacement != ref
    assert links.resolve_signed_link_ref(replacement) is not None
    if expiry == "ref":
        with sqlite3.connect(state) as conn:
            assert conn.execute("SELECT 1 FROM signed_link_refs WHERE ref_id = ?", (ref,)).fetchone() is None
