from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Thread

import pytest

from workers_projects_runtime.store import Store


def _assert_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_store_operations_reuse_opened_connections_without_stale_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    project = store.create_project(
        owner_id="owner",
        title="Before",
        goal="Reuse connections",
        default_worker_profile="codex-cli",
    )
    opened: list[sqlite3.Connection] = []
    open_connection = store._open_connection

    def recording_open(**kwargs) -> sqlite3.Connection:
        connection = open_connection(**kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_open_connection", recording_open)
    try:
        for _ in range(5):
            store.health_check()
        writer = sqlite3.connect(db_path)
        try:
            writer.execute(
                "UPDATE projects SET title = ? WHERE project_id = ?",
                ("After", project["project_id"]),
            )
            writer.commit()
        finally:
            writer.close()
        assert store.get_project(project["project_id"])["title"] == "After"

        # A nested operation keeps its own transaction on a separate connection.
        with store._connect() as outer:
            with store._connect() as inner:
                assert inner is not outer
        worker_thread = Thread(target=store.health_check)
        worker_thread.start()
        worker_thread.join(timeout=10)

        assert len(opened) <= 2
    finally:
        store.close()
    for connection in opened:
        _assert_closed(connection)


def test_store_never_reuses_a_failed_or_reconfigured_connection(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "runtime.db"))
    try:
        with pytest.raises(RuntimeError, match="operation failed"):
            with store._connect() as failed:
                raise RuntimeError("operation failed")
        with store._connect() as foreign_keys_disabled:
            foreign_keys_disabled.execute("PRAGMA foreign_keys = OFF")
        with store._connect() as temporary_table:
            temporary_table.execute("CREATE TEMP TABLE scratch (value TEXT)")

        for connection in (failed, foreign_keys_disabled, temporary_table):
            _assert_closed(connection)
        with store._connect() as reused:
            assert reused.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert reused.execute("SELECT 1 FROM temp.sqlite_master").fetchone() is None
    finally:
        store.close()


def test_store_close_releases_idle_connections(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "runtime.db"))
    with store._connect() as connection:
        connection.execute("SELECT 1")

    store.close()

    _assert_closed(connection)
