from __future__ import annotations

import sqlite3


SCHEMA_LEDGER_TABLE = "glasshive_schema_versions"


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised before startup mutates a database created by newer code."""


def begin_schema_migration(connection: sqlite3.Connection) -> None:
    """Serialize check/migrate/record across competing startup processes."""

    connection.execute("BEGIN IMMEDIATE")


def execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute DDL without ``executescript`` implicitly committing first."""

    statement_lines: list[str] = []
    for line in str(script or "").splitlines():
        statement_lines.append(line)
        statement = "\n".join(statement_lines).strip()
        if statement and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement_lines = []
    remainder = "\n".join(statement_lines).strip()
    if remainder:
        raise sqlite3.OperationalError("Incomplete GlassHive schema migration statement")


def require_compatible_schema(
    connection: sqlite3.Connection,
    *,
    component: str,
    target_version: int,
) -> int:
    clean_component = str(component or "").strip()
    if not clean_component or target_version < 1:
        raise ValueError("A schema component and positive target version are required")
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_LEDGER_TABLE} (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL CHECK(version >= 1)
        )
        """
    )
    row = connection.execute(
        f"SELECT version FROM {SCHEMA_LEDGER_TABLE} WHERE component = ?",
        (clean_component,),
    ).fetchone()
    current_version = int(row[0]) if row is not None else 0
    if current_version > target_version:
        raise UnsupportedSchemaVersionError(
            f"GlassHive database component {clean_component!r} is schema version "
            f"{current_version}, newer than this runtime supports ({target_version})"
        )
    return current_version


def record_schema_version(
    connection: sqlite3.Connection,
    *,
    component: str,
    version: int,
) -> None:
    clean_component = str(component or "").strip()
    if not clean_component or version < 1:
        raise ValueError("A schema component and positive version are required")
    row = connection.execute(
        f"SELECT version FROM {SCHEMA_LEDGER_TABLE} WHERE component = ?",
        (clean_component,),
    ).fetchone()
    current_version = int(row[0]) if row is not None else 0
    if current_version > version:
        raise UnsupportedSchemaVersionError(
            f"Refusing to downgrade GlassHive database component {clean_component!r} "
            f"from schema version {current_version} to {version}"
        )
    connection.execute(
        f"""
        INSERT INTO {SCHEMA_LEDGER_TABLE} (component, version)
        VALUES (?, ?)
        ON CONFLICT(component) DO UPDATE SET version = MAX(version, excluded.version)
        """,
        (clean_component, version),
    )
