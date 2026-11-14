from __future__ import annotations

import sqlite3

import pytest

from workers_projects_runtime.store import Store


@pytest.mark.parametrize("upgrade", [False, True], ids=["new-database", "older-schema"])
def test_older_writer_inherits_configured_standard_memory(tmp_path, monkeypatch, upgrade):
    database = tmp_path / "runtime.sqlite3"
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "3072" if upgrade else "4096")
    store = Store(str(database))
    project = store.create_project("owner-a", "Synthetic project", "Keep memory policy", "codex-cli")
    worker = store.create_worker(
        project["project_id"], "owner-a", "Synthetic worker", "assistant",
        "codex-cli", "seed", "codex-cli", "synthetic-model",
    )
    store.close()
    if upgrade:
        with sqlite3.connect(database) as connection:
            connection.execute("ALTER TABLE workers DROP COLUMN resource_memory_bytes")
            connection.execute("ALTER TABLE workers DROP COLUMN resource_class")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "4096")
    store = Store(str(database))

    # An older writer supplies its known fields and lets the newly added
    # resource columns use their database defaults.
    with sqlite3.connect(database) as connection:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(workers)")
            if row[1] not in {"resource_class", "resource_memory_bytes"}
        ]
        names = ", ".join('"' + name + '"' for name in columns)
        values = ", ".join(
            "?" if name in {"worker_id", "alias"} else '"' + name + '"'
            for name in columns
        )
        connection.execute(
            f"INSERT INTO workers ({names}) SELECT {values} FROM workers WHERE worker_id = ?",
            ("wrk_older_writer", "older-writer", worker["worker_id"]),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    created = store.get_worker("wrk_older_writer")
    assert created["resource_class"] == "standard"
    assert created["resource_memory_bytes"] == 4096 * 1024**2
    store.close()
