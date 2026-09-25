"""GlassHive's credential-free, quiescent Native continuity boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .control_plane import CONTROL_PLANE_SCHEMA_VERSION, ControlPlaneStore
from .deliverables import NON_DELIVERABLE_DIR_NAMES
from .execution_profile import packaged_linux
from .models import utc_now
from .profile_runtime import ProfiledWorkerRuntime
from .service import (_bounded_int_env, _copy_regular_workspace_file,
                      _workspace_copy_plan, _workspace_duplicate_path_is_excluded)
from .store import (CALLBACK_TRACE_AUTHORITY_FIELDS, COMPUTE_OPERATION_CLEAR_FIELDS,
    NONTERMINAL_RUN_STATES, RUNTIME_STORE_SCHEMA_VERSION, TERMINAL_RUN_STATES, Store,
    _callback_trace_authority_values, _callback_trace_event_sha256_values,
    _text_sha256, canonical_parallel_clean_room_bootstrap, verified_callback_trace_snapshots)


_PEER_COLLABORATION_SCHEMA_VERSION = 3
_OPTIONAL_SCHEMA_VERSIONS = {"coordinator": 3, "worker_configuration": 2}
_KNOWN_COMPONENTS = {
    "runtime_store",
    "control_plane",
    "peer_collaboration",
    *_OPTIONAL_SCHEMA_VERSIONS,
}
_OPTIONAL_TABLES = {
    "coordinator": {
        "coordinator_conversations", "coordinator_turns", "coordinator_goals",
        "coordinator_actions", "coordinator_events",
    },
    "worker_configuration": {"worker_configurations", "worker_context_snapshots"},
}
# Owned tables recognized by presence alone: the default-off local-QA fault authority
# creates its complete set when enabled and records no schema-version row.
_LEDGERLESS_OPTIONAL_TABLES = {
    "local_qa_control": frozenset({
        "local_qa_fault_controls", "local_qa_fault_audit", "local_qa_fault_arm_ledger",
    }),
}


@contextmanager
def _sqlite_connection(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


# Tables the runtime writes by column position (INSERT ... VALUES or SELECT *
# without a column list). Their persisted column order is part of their meaning,
# so it must equal the current owner DDL exactly. A test keeps this complete.
_POSITIONAL_TABLES = frozenset({
    "peer_grant_batches", "peer_grants", "peer_messages", "peer_policies",
    "worker_context_snapshots", "workspace_file_pins", "workspace_file_policies",
    "workspace_file_projection_targets", "workspace_file_storage_config",
})


def _canonical_sql(sql: str) -> str:
    """Insignificant whitespace removed outside quoted text; anything else is kept."""
    out: list[str] = []
    quote = ""
    pending_space = False
    for character in sql:
        if quote:
            out.append(character)
            if character == quote:
                quote = ""
            continue
        if character in "'\"`[":
            quote = "]" if character == "[" else character
        elif character.isspace():
            pending_space = True
            continue
        if pending_space and out and out[-1] not in "(," and character not in "),":
            out.append(" ")
        pending_space = False
        out.append(character)
    return "".join(out)


def _table_definitions(sql: str) -> tuple[str, tuple[str, ...], str] | None:
    """Split CREATE TABLE text into prefix, its top-level definitions and suffix.

    Returns None when the text has comments or unbalanced quoting/parentheses,
    so that only the exact text can match.
    """
    text = _canonical_sql(sql)
    parts: list[str] = []
    depth, quote, start, prefix = 0, "", -1, ""
    for index, character in enumerate(text):
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "'\"`[":
            quote = "]" if character == "[" else character
        elif text.startswith(("--", "/*"), index):
            return None
        elif character == "(":
            depth += 1
            if depth == 1 and start < 0:
                prefix, start = text[:index], index + 1
        elif character == ")":
            depth -= 1
            if depth == 0 and start >= 0:
                parts.append(text[start:index])
                return prefix, tuple(parts), text[index + 1:]
            if depth < 0:
                return None
        elif character == "," and depth == 1:
            parts.append(text[start:index])
            start = index + 1
    return None


def _schema_shape(database: sqlite3.Connection) -> tuple[dict, dict]:
    """Return the complete owner-defined SQLite shape, including constraints and indexes.

    A table created by an earlier release, or extended in place by the owner's
    own migrations, can hold the same definitions in a different order. Every
    column definition (type, default, NOT NULL, CHECK, COLLATE, REFERENCES,
    generated expression), table constraint, foreign key and index still has
    to equal the current owner DDL, and tables written by position keep their
    exact column order.
    """

    tables: dict[str, dict] = {}
    definitions: dict[tuple[str, str, str], str | None] = {}
    objects = database.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    for kind, name, table, sql in objects:
        if kind == "table":
            columns = [
                tuple(row)[1:]
                for row in database.execute("SELECT * FROM pragma_table_xinfo(?)", (name,))
            ]
            # A foreign key's id is its clause position; group its column pairs instead.
            grouped: dict[int, list] = {}
            for row in database.execute("SELECT * FROM pragma_foreign_key_list(?)", (name,)):
                grouped.setdefault(row[0], []).append(tuple(row)[1:])
            foreign_keys = sorted(
                ((rows[0][1], rows[0][4], rows[0][5], rows[0][6],
                  tuple(sorted((item[0], item[2], item[3]) for item in rows)))
                 for rows in grouped.values()),
                key=repr,  # a referenced column can be NULL (the parent's key)
            )
            indexes = []
            for index in database.execute("SELECT * FROM pragma_index_list(?)", (name,)):
                index_name = str(index[1])
                indexes.append(
                    (
                        # List position and column ids follow table layout; names do not.
                        tuple(index)[1:],
                        tuple((item[0], item[2]) for item in database.execute(
                            "SELECT * FROM pragma_index_info(?)", (index_name,))),
                        database.execute(
                            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                            (index_name,),
                        ).fetchone()[0],
                    )
                )
            parsed = _table_definitions(str(sql or ""))
            if parsed is None or str(name) in _POSITIONAL_TABLES:
                table_sql = (re.sub(r"\s+", " ", str(sql or "")).strip(),)
                ordered_columns = tuple(columns)
            else:
                prefix, parts, suffix = parsed
                table_sql = (prefix, tuple(sorted(parts)), suffix)
                ordered_columns = tuple(sorted(columns, key=lambda value: str(value[0])))
            tables[str(name)] = {
                "columns": ordered_columns,
                "foreign_keys": tuple(foreign_keys),
                "indexes": tuple(sorted(indexes, key=lambda value: value[0][0])),
                # PRAGMA columns/FKs/indexes omit CHECK predicates and other
                # table guards, so every definition's text is compared too.
                "table_sql": table_sql,
            }
        else:
            definitions[(str(kind), str(name), str(table))] = sql
    return tables, definitions


def _schema_composition(connection: sqlite3.Connection) -> dict[str, int]:
    observed = dict(connection.execute("SELECT component, version FROM glasshive_schema_versions"))
    if set(observed) - _KNOWN_COMPONENTS:
        raise ValueError("GlassHive continuity contains an unknown schema component")
    required = {
        "runtime_store": RUNTIME_STORE_SCHEMA_VERSION,
        "control_plane": CONTROL_PLANE_SCHEMA_VERSION,
        "peer_collaboration": _PEER_COLLABORATION_SCHEMA_VERSION,
    }
    if any(observed.get(component) != version for component, version in required.items()):
        raise ValueError("GlassHive continuity requires the current reviewed component schema")
    composition = dict(required)
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for component, version in _OPTIONAL_SCHEMA_VERSIONS.items():
        present = tables.intersection(_OPTIONAL_TABLES[component])
        if present and present != _OPTIONAL_TABLES[component]:
            raise ValueError("GlassHive continuity contains a partial optional schema")
        ledger_present = component in observed
        if ledger_present != bool(present):
            raise ValueError("GlassHive continuity schema ledger does not match its owned tables")
        if ledger_present and observed[component] != version:
            raise ValueError("GlassHive continuity requires the current reviewed component schema")
        if ledger_present:
            composition[component] = version
    for component, owned in _LEDGERLESS_OPTIONAL_TABLES.items():
        present = tables.intersection(owned)
        if present and present != owned:
            raise ValueError("GlassHive continuity contains a partial optional schema")
        if present:
            composition[component] = 1
    return composition


def _apply_schema_owners(path: Path, components) -> Store:
    store = Store(str(path))
    try:
        ControlPlaneStore(str(path))
        if "coordinator" in components:
            from .coordinator import CoordinatorService

            CoordinatorService(store, object(), object())
        if "worker_configuration" in components:
            from .worker_configuration import WorkerConfiguration

            WorkerConfiguration(store, object())
        if "local_qa_control" in components:
            from .local_qa_control import initialize_local_qa_schema

            with store._connect() as connection:
                initialize_local_qa_schema(connection)
    except BaseException:
        store.close()
        raise
    return store


def _reference_shape(composition) -> tuple[dict, dict]:
    # Build the expected shape through the actual schema owners, not a duplicate
    # column registry. Ledger equality alone cannot authorize unknown persisted data.
    with tempfile.TemporaryDirectory(prefix="glasshive-continuity-schema-") as temporary:
        baseline = _apply_schema_owners(Path(temporary) / "schema.sqlite", composition)
        try:
            with baseline._connect() as reference:
                return _schema_shape(reference)
        finally:
            baseline.close()


_TABLE_CONSTRAINT = re.compile(r"(?:CONSTRAINT|PRIMARY|UNIQUE|CHECK|FOREIGN)\b", re.IGNORECASE)


def _only_added_columns(live: tuple[dict, dict], reference: tuple[dict, dict]) -> bool:
    """True when ``live`` is the reviewed shape minus columns added in place since.

    Everything else must already be exact: component tables, indexes, triggers,
    views, foreign keys, table constraints, every present column's definition,
    and every table written by position.
    """
    (live_tables, live_objects), (tables, objects) = live, reference
    if live_objects != objects or set(live_tables) != set(tables):
        return False
    for name, expected in tables.items():
        actual = live_tables[name]
        if actual == expected:
            continue
        if (name in _POSITIONAL_TABLES or len(actual["table_sql"]) != 3 or len(expected["table_sql"]) != 3
                or actual["foreign_keys"] != expected["foreign_keys"] or actual["indexes"] != expected["indexes"]
                or actual["table_sql"][0] != expected["table_sql"][0] or actual["table_sql"][2] != expected["table_sql"][2]
                or not set(actual["columns"]) < set(expected["columns"])):
            return False
        present, reviewed = set(actual["table_sql"][1]), set(expected["table_sql"][1])
        if not present < reviewed or any(_TABLE_CONSTRAINT.match(part) for part in reviewed - present):
            return False
    return True


def _require_schema(connection: sqlite3.Connection, *, incoming: bool = False) -> None:
    """Require the reviewed shape.

    ``incoming`` is for the release about to take over this state (an upgrade).
    It may also inherit the same schema generation with columns its own owners
    add in place (as a release of the same component versions left it), when
    those owners, applied to a private copy, then produce exactly the reviewed
    shape. Nothing else differs: component versions, tables, indexes, triggers,
    constraints and positional tables are exact either way.
    """
    composition = _schema_composition(connection)
    reference = _reference_shape(composition)
    live = _schema_shape(connection)
    if live == reference:
        return
    if not incoming or not _only_added_columns(live, reference):
        raise ValueError("GlassHive continuity contains an unreviewed schema shape")
    database = next((Path(row[2]) for row in connection.execute("PRAGMA database_list") if row[1] == "main"), None)
    try:
        size = sum(path.stat().st_size for path in (database, Path(f"{database}-wal")) if path.exists())
        with tempfile.TemporaryDirectory(prefix="glasshive-continuity-incoming-") as temporary:
            if shutil.disk_usage(temporary).free < 3 * size + (64 << 20):
                raise ValueError("GlassHive earlier state is too large to review in the available scratch space")
            path = Path(temporary) / "inherited.sqlite"
            copy = sqlite3.connect(path)
            try:
                connection.backup(copy)
            finally:
                copy.close()
            migrated = _apply_schema_owners(path, composition)
            try:
                with migrated._connect() as inherited:
                    if (_schema_composition(inherited) != composition
                            or _schema_shape(inherited) != reference):
                        raise ValueError("GlassHive continuity contains an unreviewed schema shape")
            finally:
                migrated.close()
    except ValueError:
        raise
    except Exception:  # copy or owner migration failed: not a reviewable earlier state
        raise ValueError("GlassHive continuity cannot review this earlier state") from None


def _private_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        metadata = path.lstat()
        if path.is_symlink() or not (path.is_file() or path.is_dir()) or metadata.st_uid != os.getuid() or (path.is_file() and metadata.st_nlink != 1):
            raise ValueError("GlassHive continuity contains unsafe state")
        path.chmod(0o700 if path.is_dir() or metadata.st_mode & stat.S_IXUSR else 0o600)


def _worker_paths(runtime: ProfiledWorkerRuntime, worker: dict, root: Path) -> tuple[Path, Path]:
    worker_id = str(worker["worker_id"])
    if not worker_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in worker_id):
        raise ValueError("GlassHive continuity worker identity is unsafe")
    owner = runtime._runtime_for_worker(worker)
    state, workspace = owner._state_dir(worker_id), owner._workspace_dir(worker_id)
    state.relative_to(root)
    workspace.relative_to(root)
    return state, workspace


def _workspace_directory_plan(source_root: Path) -> list[tuple[Path, Path]]:
    """Retain ordinary and Trash empty folders within the existing copy exclusions."""
    max_depth = _bounded_int_env("GLASSHIVE_DUPLICATE_MAX_DEPTH", 64, min_value=1, max_value=1024)
    deadline = time.monotonic() + 300
    directories: list[tuple[Path, Path]] = []
    pending = [source_root]
    while pending:
        if time.monotonic() > deadline:
            raise ValueError("GlassHive workspace directory copy exceeded its time limit")
        parent = pending.pop()
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("GlassHive workspace directory changed during capture")
        for item in sorted(parent.iterdir(), key=lambda child: child.name.casefold()):
            relative = item.relative_to(source_root)
            if (_workspace_duplicate_path_is_excluded(relative)
                    or any(part.casefold() in NON_DELIVERABLE_DIR_NAMES for part in relative.parts)):
                continue
            if len(relative.parts) > max_depth:
                raise ValueError("GlassHive workspace directory exceeds its depth limit")
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                # The shared file copy preflight already checked safe internal
                # links and deliberately skipped their target bytes.
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.append((item, relative))
                if len(directories) > 100_000:
                    raise ValueError("GlassHive workspace directory count exceeds its limit")
                pending.append(item)
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError("GlassHive workspace contains an unsupported item")
    return sorted(directories, key=lambda item: (len(item[1].parts), item[1].as_posix()))


def _shared_box(connection: sqlite3.Connection, worker: dict, *, data_root: Path, control_root: Path):
    """Resolve a persisted member with the Native owner's pure path mapping."""

    from .workspace_box import WorkspaceBox, WorkspaceMemberBinding

    workspace = connection.execute(
        "SELECT * FROM execution_workspaces WHERE workspace_id=? AND tenant_id=? AND owner_id=?",
        (worker["workspace_id"], worker["tenant_id"], worker["owner_id"]),
    ).fetchone()
    identity = connection.execute(
        "SELECT * FROM execution_workspace_identities WHERE workspace_id=? AND worker_id=? "
        "AND tenant_id=? AND owner_id=?",
        (worker["workspace_id"], worker["worker_id"], worker["tenant_id"], worker["owner_id"]),
    ).fetchone()
    from .execution_profile import packaged_linux
    if (workspace is None or identity is None
            or (workspace["mode"] != "shared" and not packaged_linux())
            or workspace["execution_mode"] != "docker" or worker["execution_mode"] != "docker"):
        raise ValueError("GlassHive Native workspace member binding is unavailable")
    binding = WorkspaceMemberBinding(
        str(worker["workspace_id"]), str(worker["worker_id"]),
        str(worker["tenant_id"]), str(worker["owner_id"]), int(identity["member_uid"]),
    )
    return WorkspaceBox(
        volume_root=data_root, control_root=control_root, volume_name="continuity-path-map",
        image="continuity-path-map", binding=binding, memory_bytes=1, pids_limit=1,
        file_placement=str(workspace["file_placement"]),
    )


def _shared_roots() -> tuple[Path, Path]:
    data = Path(os.environ.get("XPERFECT_SHARED_VOLUME_ROOT") or "")
    from .execution_profile import packaged_linux

    if not data.is_absolute() or not data.is_dir() or data != data.resolve():
        raise ValueError("GlassHive shared Native data root is unavailable")
    raw_control = os.environ.get("XPERFECT_CONTROL_ROOT") if packaged_linux() else ""
    control = Path(raw_control) / "workspaces" if raw_control else data / "workspace-control"
    if not control.is_absolute() or not control.is_dir() or control != control.resolve():
        raise ValueError("GlassHive shared Native control root is unavailable")
    return data, control


def _source_box_roots(connection: sqlite3.Connection, worker: dict) -> tuple[Path, Path]:
    quota = bool(connection.execute(
        "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
    ).fetchone()[0])
    if not quota:
        return _shared_roots()
    registry = Path(str(os.environ.get("XPERFECT_STORAGE_REGISTRY_PATH") or ""))
    data = _quota_owner_root(connection, _quota_backend(), worker["tenant_id"], worker["owner_id"])
    control = registry.parent / "runtime-storage" / "workspaces"
    if not control.is_absolute() or not control.is_dir() or control != control.resolve():
        raise ValueError("GlassHive quota Native control root is unavailable")
    return data, control


def _archive_box_roots(connection: sqlite3.Connection, worker: dict, archive_root: Path) -> tuple[Path, Path]:
    quota = bool(connection.execute(
        "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
    ).fetchone()[0])
    if quota:
        digest = hashlib.sha256(f"{worker['tenant_id']}\0{worker['owner_id']}".encode()).hexdigest()
        data = archive_root / "data" / "owners" / digest
        control = archive_root / "control" / "storage" / "runtime-storage" / "workspaces"
    else:
        data = archive_root / "data"
        control = archive_root / "control" / "workspaces"
    return data, control


def _shared_process_absent(box) -> bool:
    """An unavailable or present Docker inspection never proves absence."""
    try:
        return box._inspect() is None
    except Exception:
        return False


def _quota_backend():
    from .storage_quota import XfsProjectStorage

    root = os.environ.get("XPERFECT_STORAGE_ROOT")
    registry = os.environ.get("XPERFECT_STORAGE_REGISTRY_PATH")
    if not root or not registry:
        raise ValueError("GlassHive quota continuity requires the native owner storage backend")
    try:
        return XfsProjectStorage.open_existing(Path(root), Path(registry))
    except Exception as exc:
        raise ValueError("GlassHive quota continuity cannot attest native owner storage") from exc


def _quota_owner_root(connection: sqlite3.Connection, backend, tenant: str, owner: str) -> Path:
    policy = connection.execute(
        "SELECT storage_limit_bytes FROM workspace_file_policies WHERE tenant_id=? AND owner_id=?",
        (tenant, owner),
    ).fetchone()
    if policy is not None:
        limit = policy["storage_limit_bytes"]
    else:
        # Owners without their own row use the deployment limit, as the runtime
        # does. Offline callers must pass that exact limit; nothing is guessed.
        configured = os.environ.get("GLASSHIVE_OWNER_STORAGE_BYTES", "")
        if not re.fullmatch(r"[0-9]+", configured):
            raise ValueError("GlassHive quota continuity requires a durable owner storage policy")
        limit = int(configured)
    try:
        return Path(backend.snapshot(tenant, owner, limit).root)
    except Exception as exc:
        raise ValueError("GlassHive quota continuity owner storage identity is unavailable") from exc


def _managed_file_versions(store: Store, archive_root: Path, source_root: Path | None = None) -> tuple[int, int]:
    """Copy or verify complete local Files versions without opening a live owner."""

    from .workspace_file_storage import manifests

    with store._connect() as connection:
        quota_required = bool(connection.execute(
            "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
        ).fetchone()[0])
        rows = [dict(row) for row in connection.execute(
            "SELECT upload_id,tenant_id,owner_id,size_bytes,sha256,state "
            "FROM workspace_file_uploads ORDER BY tenant_id,owner_id,upload_id"
        )]
        owners = {(row["tenant_id"], row["owner_id"]) for row in rows}
        owners.update(tuple(row) for row in connection.execute(
            "SELECT DISTINCT tenant_id,owner_id FROM workspace_file_bindings"
        ))
        ready = {(row["tenant_id"], row["owner_id"], row["upload_id"]): row
                 for row in rows if row["state"] == "ready"}
        if any(row["state"] in {"receiving", "cancelling"} for row in rows):
            raise ValueError("GlassHive Files transfers must settle before continuity")
        if connection.execute("SELECT 1 FROM workspace_file_operations WHERE state='prepared' LIMIT 1").fetchone():
            raise ValueError("GlassHive Files mutations must settle before continuity")
        for row in connection.execute("SELECT upload_id FROM workspace_file_pins"):
            if not any(key[2] == row[0] for key in ready):
                raise ValueError("GlassHive Files pinned version is unavailable")
        for row in connection.execute("SELECT version_id FROM workspace_file_entries WHERE version_id!=''"):
            if not any(key[2] == row[0] for key in ready):
                raise ValueError("GlassHive Files indexed version is unavailable")
        for tenant, owner in owners:
            for _, item in manifests(connection, tenant, owner):
                version = ready.get((tenant, owner, str(item.get("upload_id") or "")))
                if (version is None or version["sha256"] != item.get("sha256")
                        or version["size_bytes"] != item.get("size_bytes")):
                    raise ValueError("GlassHive Files referenced version is unavailable")

    copied_count = copied_bytes = 0
    deadline = time.monotonic() + 300
    from .execution_profile import packaged_linux
    packaged = packaged_linux()
    backend = _quota_backend() if quota_required else None
    for tenant, owner in sorted(owners):
        owner_digest = hashlib.sha256(f"{tenant}\0{owner}".encode()).hexdigest()
        if quota_required:
            target_dir = archive_root / "data" / "owners" / owner_digest / "managed-files"
            with store._connect() as connection:
                source_owner = _quota_owner_root(connection, backend, tenant, owner)
            location = source_owner / "managed-files" if source_root is not None else target_dir
        elif packaged:
            target_dir = archive_root / "data" / "managed-files" / owner_digest
            location = Path(os.environ["XPERFECT_SHARED_VOLUME_ROOT"]) / "managed-files" / owner_digest if source_root is not None else target_dir
        else:
            target_dir = archive_root / "managed-files" / owner_digest
            location = source_root / "managed-files" / owner_digest if source_root is not None else target_dir
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        expected = {f"{row['upload_id']}.blob": row for row in rows
                    if (row["tenant_id"], row["owner_id"], row["state"]) == (tenant, owner, "ready")}
        if source_root is not None and location != location.resolve(strict=False):
            raise ValueError("GlassHive Files source storage root is unsafe")
        if location.exists() or location.is_symlink():
            if location.is_symlink() or not location.is_dir():
                raise ValueError("GlassHive Files owner root is unsafe")
            actual = {entry.name for entry in location.iterdir()}
            legacy = {name for name in actual - set(expected)
                      if re.fullmatch(r"prj_[0-9a-f]{32}\.receipt", name)}
            if source_root is None and legacy:
                raise ValueError("GlassHive Files archive contains source projection authority")
            if source_root is not None and legacy:
                with store._connect() as connection:
                    accepted = {str(row[0]) + ".receipt" for row in connection.execute(
                        "SELECT projection_id FROM workspace_file_projection_targets "
                        "WHERE tenant_id=? AND owner_id=?", (tenant, owner)
                    )}
                if not legacy <= accepted:
                    raise ValueError("GlassHive Files legacy projection receipt has no owner")
                for name in legacy:
                    info = (location / name).lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size or info.st_uid != os.geteuid():
                        raise ValueError("GlassHive Files legacy projection receipt is invalid")
            if actual != (set(expected) | legacy):
                raise ValueError("GlassHive Files versions contain missing or unsettled payloads")
        elif expected:
            raise ValueError("GlassHive Files ready payload is missing")
        for name, row in expected.items():
            if not re.fullmatch(r"fil_[0-9a-f]{32}\.blob", name):
                raise ValueError("GlassHive Files version identity is invalid")
            if (not isinstance(row["size_bytes"], int) or row["size_bytes"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"]))):
                raise ValueError("GlassHive Files version metadata is invalid")
            source_file = location / name
            if source_root is not None:
                copied = _copy_regular_workspace_file(
                    source_file, target_dir / name, location,
                    max_bytes=20 * 1024**3 - copied_bytes, deadline=deadline,
                )
                copied_bytes += copied
            file_to_verify = target_dir / name
            metadata = file_to_verify.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != row["size_bytes"]:
                raise ValueError("GlassHive Files ready payload has changed")
            digest = hashlib.sha256()
            with file_to_verify.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                raise ValueError("GlassHive Files ready payload digest disagrees")
            copied_count += 1
    return copied_count, copied_bytes


def _projection_targets(connection: sqlite3.Connection, database: Path) -> list[dict]:
    """Every Files projection target with its lineage verified and its control paths.

    A target whose lineage, owner root, workspace or control paths disagree is refused.
    ``receipt`` is the committed receipt a completed target has, or None.
    """
    rows = [dict(row) for row in connection.execute("SELECT * FROM workspace_file_projection_targets")]
    if not rows:
        return []
    quota = bool(connection.execute(
        "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
    ).fetchone()[0])
    backend = _quota_backend() if quota else None
    if quota:
        receipt_control = Path(os.environ["XPERFECT_STORAGE_REGISTRY_PATH"]).parent / "runtime-storage" / "files"
    else:
        receipt_control = database.parent / "file-control"
    targets = []
    for row in rows:
        tenant, owner = str(row["tenant_id"]), str(row["owner_id"])
        if not re.fullmatch(r"prj_[0-9a-f]{32}", str(row["projection_id"])):
            raise ValueError("GlassHive Files projection identity is invalid")
        digest = hashlib.sha256(f"{tenant}\0{owner}".encode()).hexdigest()
        upload = connection.execute(
            "SELECT sha256,size_bytes,state,created_at FROM workspace_file_uploads "
            "WHERE upload_id=? AND tenant_id=? AND owner_id=?",
            (row["upload_id"], tenant, owner),
        ).fetchone()
        if (upload is None or upload["state"] != "ready" or upload["sha256"] != row["sha256"]
                or upload["size_bytes"] != row["size_bytes"]):
            raise ValueError("GlassHive Files projection version lineage disagrees")
        if quota:
            owner_root = _quota_owner_root(connection, backend, tenant, owner) / "managed-files"
        elif packaged_linux():
            owner_root = Path(os.environ["XPERFECT_SHARED_VOLUME_ROOT"]) / "managed-files" / digest
        else:
            owner_root = database.parent / "managed-files" / digest
        if Path(row["source_path"]) != owner_root / f"{row['upload_id']}.blob":
            raise ValueError("GlassHive Files projection source owner disagrees")
        workspace = Path(row["workspace_path"])
        allowed = connection.execute(
            "SELECT 1 FROM workers WHERE tenant_id=? AND owner_id=? AND workspace_dir=? LIMIT 1",
            (tenant, owner, str(workspace)),
        ).fetchone()
        if not allowed or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("GlassHive Files projection workspace owner disagrees")
        metadata = workspace.stat()
        if (metadata.st_dev, metadata.st_ino) != (row["workspace_device"], row["workspace_inode"]):
            raise ValueError("GlassHive Files projection workspace identity changed")
        control = receipt_control / digest
        receipt = control / f"{row['projection_id']}.receipt"
        if receipt.parent != receipt.parent.resolve(strict=False):
            raise ValueError("GlassHive Files projection control root is unsafe")
        legacy = owner_root / f"{row['projection_id']}.receipt"
        if not receipt.exists() and not receipt.is_symlink():
            if legacy.parent != legacy.parent.resolve(strict=False):
                raise ValueError("GlassHive Files legacy projection root is unsafe")
            cutoff = connection.execute(
                "SELECT receipt_migration_at FROM workspace_file_storage_config WHERE singleton=1"
            ).fetchone()[0]
            if legacy.exists() or legacy.is_symlink():
                legacy_info = legacy.lstat()
                if upload["created_at"] <= cutoff and legacy_info.st_mtime <= cutoff:
                    receipt = legacy
        try:
            info = receipt.lstat()
        except FileNotFoundError:
            committed = None
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size or info.st_uid != os.geteuid():
                raise ValueError("GlassHive Files projection receipt is invalid")
            committed = receipt
        targets.append({"row": row, "owner": (tenant, owner), "owner_root": owner_root, "workspace": workspace,
                        "control": control, "legacy_receipt": legacy, "receipt": committed})
    return targets


def _require_settled_file_projections(connection: sqlite3.Connection, database: Path) -> None:
    """Accept only completed Files receipts; copied rows are later made inert."""
    if any(target["receipt"] is None for target in _projection_targets(connection, database)):
        raise ValueError("GlassHive Files projection must settle before continuity")


def _projection_destination(workspace: Path, relative: str) -> Path:
    """The accepted workspace path, refused unless every existing part is a real directory."""
    parts = Path(relative).parts
    if (not relative or Path(relative).is_absolute() or "\\" in relative
            or any(part in {"", ".", ".."} for part in parts)):
        raise ValueError("GlassHive Files projection path is unsafe")
    current = workspace
    for part in parts[:-1]:
        current = current / part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise ValueError("GlassHive Files projection path is unsafe")
    return workspace / Path(*parts)


def _unpublished_evidence(target: dict) -> str | None:
    """Why this unsettled target may have published something, or None when nothing exists."""
    row, projection = target["row"], target["row"]["projection_id"]
    if os.path.lexists(target["control"] / f"{projection}.receipt") or os.path.lexists(target["legacy_receipt"]):
        return "a projection receipt exists"
    staged = f".xperfect-transfer-{projection}"
    for root in (target["owner_root"], target["workspace"] / ".xperfect-file-staging"):
        if os.path.lexists(root / staged):
            return "a staged copy exists"
    if os.path.lexists(_projection_destination(target["workspace"], str(row["relative_path"]))):
        return "the workspace destination exists"
    return None


def _accepted_by_owner(connection: sqlite3.Connection, target: dict) -> bool:
    """The target is exactly one accepted item of this owner's own Files manifests."""
    from .workspace_file_storage import manifests

    row = target["row"]
    tenant, owner = target["owner"]
    expected = (row["upload_id"], row["relative_path"], row["sha256"], row["size_bytes"])
    matches = [item for _worker_id, item in manifests(connection, tenant, owner)
               if item.get("projection_id") == row["projection_id"]]
    return bool(matches) and all(
        (item.get("upload_id"), item.get("path"), item.get("sha256"), item.get("size_bytes")) == expected
        for item in matches)


def _lock_projection(target: dict) -> int:
    """The runtime's own exclusive projection lock, taken without waiting."""
    import fcntl

    control = target["control"]
    control.mkdir(mode=0o700, exist_ok=True)
    info = control.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("GlassHive Files projection control root is unsafe")
    parent = os.open(control, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(f"{target['row']['projection_id']}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        lock = os.fstat(descriptor)
        if not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1:
            raise ValueError("GlassHive Files projection lock is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("GlassHive Files projection is in use") from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _publication_impossible() -> bool:
    """This package's runtime cannot authorize any stored-file source, so it cannot publish.

    Every release checks a projection's signed source before it stages or links any
    byte. A multi-user runtime configured without its source signing key refuses every
    projection there, now and in any request already waiting for the projection lock.
    """
    from .auth import multi_user_security_enabled
    from .bootstrap import sign_bootstrap_source_path

    return multi_user_security_enabled() and not sign_bootstrap_source_path(
        "/", tenant_id="probe", owner_id="probe")


def reconcile_unpublished_projections(database: Path, *, apply: bool = False, expect: list[str] | None = None,
                                      incoming: bool = False) -> dict:
    """Retire Files projection targets registered by a runtime that could never publish them.

    An earlier release registered a target before checking its signed source. Without
    the source key that check always failed, so nothing was staged or published, yet
    only publication writes the receipt that settles a target: continuity stays blocked.
    A target is retired only while this package's runtime still cannot authorize any
    source (so no waiting request can publish it either), when its lineage is this
    owner's accepted manifest item, and when no receipt, staged copy or workspace
    destination exists, checked again under the runtime's projection lock. Anything
    else refuses; nothing changes unless every unsettled target qualifies and, with
    ``expect``, is exactly the reviewed set. The accepted binding, pinned upload and
    bundle entry stay, so once the key is present the same file can be attached again.
    The runtime gate reads this process's environment, so it must be the package's own
    runtime configuration, as the upgrade helper provides.
    """
    metadata = database.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ValueError("GlassHive database is not an owned regular file")
    uri = database.resolve().as_uri() + ("" if apply else "?mode=ro")
    with _sqlite_connection(uri, uri=True, timeout=30) as connection:
        _require_schema(connection, incoming=incoming)
        connection.row_factory = sqlite3.Row
        unsettled = [target for target in _projection_targets(connection, database) if target["receipt"] is None]
        if unsettled and not _publication_impossible():
            raise ValueError("GlassHive Files projection could still be published by this runtime")
        for target in unsettled:
            if not _accepted_by_owner(connection, target):
                raise ValueError("GlassHive Files projection is not an accepted owner manifest item")
            reason = _unpublished_evidence(target)
            if reason:
                raise ValueError(f"GlassHive Files projection may be published ({reason})")
        report = {"unsettled": len(unsettled), "applied": False, "targets": [
            {key: target["row"][key] for key in ("projection_id", "upload_id", "sha256", "size_bytes")}
            for target in sorted(unsettled, key=lambda item: item["row"]["projection_id"])]}
        if expect is not None and sorted(expect) != [item["projection_id"] for item in report["targets"]]:
            raise ValueError("GlassHive Files unpublished targets changed since they were reviewed")
        if not apply or not unsettled:
            return report
        locks = []
        try:
            for target in sorted(unsettled, key=lambda item: item["row"]["projection_id"]):
                locks.append(_lock_projection(target))
            # A staged or linked copy cannot appear while these locks are held; a request
            # already waiting for one still fails its source check (see above).
            for target in unsettled:
                reason = _unpublished_evidence(target)
                if reason:
                    raise ValueError(f"GlassHive Files projection may be published ({reason})")
            connection.execute("BEGIN IMMEDIATE")
            for target in unsettled:
                current = connection.execute(
                    "SELECT * FROM workspace_file_projection_targets WHERE projection_id=?",
                    (target["row"]["projection_id"],),
                ).fetchone()
                if current is None or dict(current) != target["row"]:
                    raise ValueError("GlassHive Files projection changed during reconciliation")
                connection.execute("DELETE FROM workspace_file_projection_targets WHERE projection_id=?",
                                   (target["row"]["projection_id"],))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            for descriptor in locks:
                os.close(descriptor)
        report["applied"] = True
        return report


def _require_quiescent(connection: sqlite3.Connection, database: Path) -> None:
    placeholders = ",".join("?" for _ in NONTERMINAL_RUN_STATES)
    if connection.execute(f"SELECT 1 FROM runs WHERE state IN ({placeholders}) LIMIT 1", tuple(NONTERMINAL_RUN_STATES)).fetchone():
        raise ValueError("GlassHive active work must be quiesced before continuity")
    if connection.execute("SELECT 1 FROM workers WHERE state IN ('running','starting','resuming','terminating','termination_failed') LIMIT 1").fetchone():
        raise ValueError("GlassHive worker compute must be quiesced before continuity")
    if connection.execute("SELECT 1 FROM host_run_leases WHERE status IN ('active','reserved') LIMIT 1").fetchone():
        raise ValueError("GlassHive live worker leases must be quiesced before continuity")
    if connection.execute("SELECT 1 FROM workspace_gc_tombstones WHERE phase!='completed' LIMIT 1").fetchone():
        raise ValueError("GlassHive workspace cleanup must settle before continuity")
    if connection.execute(
        "SELECT 1 FROM workspace_file_uploads WHERE state IN ('receiving','cancelling') "
        "OR state NOT IN ('pending','ready','failed','cancelled','expired') LIMIT 1"
    ).fetchone():
        raise ValueError("GlassHive Files transfers must settle before continuity")
    if connection.execute(
        "SELECT 1 FROM workspace_file_operations WHERE state='prepared' "
        "OR state NOT IN ('applied','undone','aborted') LIMIT 1"
    ).fetchone():
        raise ValueError("GlassHive Files mutations must settle before continuity")
    from .execution_profile import packaged_linux
    packaged = packaged_linux()
    quota_required = bool(connection.execute(
        "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
    ).fetchone()[0])
    connection.row_factory = sqlite3.Row
    _require_settled_file_projections(connection, database)
    workers = [dict(row) for row in connection.execute("SELECT * FROM workers")]
    if not workers:
        return
    runtime = None
    for worker in workers:
        workspace_row = connection.execute(
            "SELECT mode FROM execution_workspaces WHERE workspace_id=? AND tenant_id=? AND owner_id=?",
            (worker["workspace_id"], worker["tenant_id"], worker["owner_id"]),
        ).fetchone()
        if workspace_row is not None and (workspace_row["mode"] == "shared" or packaged or quota_required):
            data_root, control_root = _source_box_roots(connection, worker)
            box = _shared_box(connection, worker, data_root=data_root, control_root=control_root)
            paths = box.paths()
            if (worker.get("state_dir") and Path(worker["state_dir"]) != paths["state_dir"]
                    or worker.get("workspace_dir") and Path(worker["workspace_dir"]) != paths["workspace_dir"]):
                raise ValueError("GlassHive recorded Native path differs from its workspace owner")
            if not _shared_process_absent(box):
                raise ValueError("GlassHive Native workspace process absence is unproved")
            continue
        if runtime is None:
            runtime = ProfiledWorkerRuntime(base_dir=str(database.parent), create_directories=False)
        owner = runtime._runtime_for_worker(worker)
        state_dir = owner._state_dir(worker["worker_id"])
        if worker.get("state_dir") and Path(worker["state_dir"]) != state_dir:
            raise ValueError("GlassHive recorded state path differs from its Native runtime owner")
        if worker["execution_mode"] == "host":
            session_path = owner._active_session_meta_path(worker["worker_id"])
            if session_path.is_symlink() or (session_path.exists() and not owner._read_active_session(worker["worker_id"])):
                raise ValueError("GlassHive worker session state is unreadable; absence is unproved")
            absent = runtime.host_active_process_status(worker).get("state") == "absent"
        else:
            absent = runtime.host_process_absence(worker, str(worker.get("last_run_id") or ""))
        if not absent:
            raise ValueError("GlassHive worker process absence is unproved; continuity cannot replace its state")


def check_quiescent(database: Path, *, incoming: bool = False) -> None:
    metadata = database.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ValueError("GlassHive database is not an owned regular file")
    with _sqlite_connection(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        _require_schema(connection, incoming=incoming)
        # Idle is always read from the live state itself, never from a migrated copy:
        # schema owners may rewrite rows while migrating.
        try:
            if incoming:
                # An earlier release's run states must all be known to this one.
                known = tuple(NONTERMINAL_RUN_STATES | TERMINAL_RUN_STATES)
                marks = ",".join("?" for _ in known)
                if connection.execute(f"SELECT 1 FROM runs WHERE state NOT IN ({marks}) LIMIT 1", known).fetchone():
                    raise ValueError("GlassHive earlier state has a run state this release does not know")
            _require_quiescent(connection, database)
        except (sqlite3.Error, KeyError, TypeError, IndexError):
            if not incoming:
                raise
            raise ValueError("GlassHive earlier state cannot be proved idle by this release") from None


def _durable_claude_mcp(value: object) -> dict:
    """Project typed Claude MCP routes without carrying headers or tokens."""

    if not isinstance(value, dict):
        if value in (None, ""):
            return {}
        raise ValueError("GlassHive coordinator MCP configuration is not a typed object")
    servers = value.get("mcpServers", value)
    if not isinstance(servers, dict):
        raise ValueError("GlassHive coordinator MCP server map is invalid")
    result: dict[str, dict[str, str]] = {}
    for name, config in servers.items():
        if not isinstance(name, str) or not isinstance(config, dict):
            # Legacy malformed entries are not executable routes.  Drop them
            # instead of copying their opaque value into a portable archive.
            continue
        unknown = set(config) - {"type", "url", "headers"}
        if unknown:
            raise ValueError("GlassHive coordinator MCP route contains an unhandled typed field")
        url = str(config.get("url") or "").strip()
        if not url:
            continue
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("GlassHive coordinator MCP route URL is not credential-free")
        server_type = str(config.get("type") or "http").strip()
        if server_type not in {"http", "sse"}:
            raise ValueError("GlassHive coordinator MCP route type is unsupported")
        # headers is a reviewed credential-bearing shape.  It is consumed by the
        # live provider owner and therefore deliberately does not cross continuity.
        result[name] = {"type": server_type, "url": url}
    return {"mcpServers": result} if "mcpServers" in value else result


def _durable_bootstrap(value: object) -> dict:
    """Retain task meaning through existing typed projection, without grants."""
    source = value if isinstance(value, dict) else {}
    projected = canonical_parallel_clean_room_bootstrap(source)
    if "claude_project_mcp" in source:
        projected["claude_project_mcp"] = _durable_claude_mcp(source.get("claude_project_mcp"))
    result = {key: projected[key] for key in (
        "project_definition", "developer_instructions", "system_instructions",
        "agents_md", "claude_md", "codex_md", "env", "execution_policy",
        "viventium_constraint_source", "viventium_run_liveness",
        "viventium_delegation_context", "viventium_delegation_identity",
        "viventium_feelings_projection",
    ) if key in projected}
    if "viventium_delegation_packet" in projected:
        packet = projected["viventium_delegation_packet"]
        result["viventium_delegation_packet"] = {key: packet[key] for key in ("version", "task", "explicit_constraints", "selected_files") if key in packet}
    if "claude_project_mcp" in projected:
        result["claude_project_mcp"] = projected["claude_project_mcp"]
    return result


def _durable_coordinator_config(value: object) -> str:
    """Project a CoordinatorConfig and each typed route bootstrap bundle."""

    from .coordinator import CoordinatorConfig

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("GlassHive coordinator configuration is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("GlassHive coordinator configuration is invalid")
    try:
        config = CoordinatorConfig.model_validate(value)
    except Exception as exc:
        raise ValueError("GlassHive coordinator configuration is invalid") from exc
    projected = config.model_dump()
    projected["bootstrap_bundle"] = _durable_bootstrap(config.bootstrap_bundle)
    projected["routes"] = []
    for route in config.routes:
        item = route.model_dump()
        item["bootstrap_bundle"] = _durable_bootstrap(route.bootstrap_bundle)
        projected["routes"].append(item)
    return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _project_coordinator_payload(value: object) -> str:
    """Project the provider packet's typed bootstrap, preserving message bytes."""

    if not isinstance(value, str) or not value:
        return value or ""
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value

    if not isinstance(decoded, dict):
        return value
    metadata = decoded.get("metadata")
    if not isinstance(metadata, dict) or "bootstrap_bundle" not in metadata:
        return value
    bundle = metadata["bootstrap_bundle"]
    if not isinstance(bundle, dict):
        raise ValueError("GlassHive coordinator bootstrap payload is invalid")
    projected_bundle = _durable_bootstrap(bundle)
    if projected_bundle == bundle:
        return value
    projected = {**decoded, "metadata": {**metadata, "bootstrap_bundle": projected_bundle}}
    return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _project_coordinator_state(connection: sqlite3.Connection) -> None:
    """Fence coordinator-owned route/bootstrap authority before hold assignment."""

    conversations = connection.execute(
        "SELECT conversation_id, config_json FROM coordinator_conversations"
    ).fetchall()
    for row in conversations:
        projected = _durable_coordinator_config(row["config_json"])
        if projected != row["config_json"]:
            connection.execute(
                "UPDATE coordinator_conversations SET config_json=? WHERE conversation_id=?",
                (projected, row["conversation_id"]),
            )
    for row in connection.execute(
        "SELECT conversation_id,turn_id,payload_json FROM coordinator_turns"
    ).fetchall():
        projected = _project_coordinator_payload(row["payload_json"])
        if projected != row["payload_json"]:
            connection.execute(
                "UPDATE coordinator_turns SET payload_json=? WHERE conversation_id=? AND turn_id=?",
                (projected, row["conversation_id"], row["turn_id"]),
            )


def _require_exportable_trace(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT snapshot_json FROM callback_trace_events"):
        if json.loads(row[0]).get("deliveryLeaseToken"):
            raise ValueError("GlassHive portable callback history contains delivery authority")


def _require_exportable_control_plane(connection: sqlite3.Connection) -> None:
    """Reject an archive while a provider projection still owns executable authority."""

    pending = connection.execute(
        "SELECT lease_id FROM provider_account_projections WHERE state != 'complete' LIMIT 1"
    ).fetchone()
    if pending:
        raise ValueError("GlassHive continuity cannot capture an unfinished provider projection")


def _restore_hold_digest(items: list[str]) -> str:
    encoded = json.dumps(
        sorted({str(item) for item in items if str(item)}),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reset_restore_authority(connection: sqlite3.Connection) -> None:
    """Make restored native work inert while retaining all history and user intent."""

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='peer_native_sessions'"
    ).fetchone():
        connection.execute("DELETE FROM peer_native_sessions")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='peer_grants'"
    ).fetchone():
        connection.execute(
            "UPDATE peer_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE revoked_at IS NULL",
            (time.time(),),
        )

    # Provider requests are retained, but every pre-existing transition/fallback/result
    # path is fenced until the owner explicitly resumes an exact pending set.
    connection.execute(
        "UPDATE provider_requests SET restore_hold=1, restore_hold_set_hash=''"
    )

    has_coordinator = bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_conversations'"
        ).fetchone()
    )
    if not has_coordinator:
        rows = connection.execute(
            "SELECT request_id FROM provider_requests WHERE restore_hold=1 ORDER BY request_id"
        ).fetchall()
        connection.execute(
            "UPDATE provider_requests SET restore_hold_set_hash=? WHERE restore_hold=1",
            (_restore_hold_digest([f"request:{row[0]}" for row in rows]),),
        )
        return

    _project_coordinator_state(connection)
    connection.execute(
        "UPDATE coordinator_conversations SET restore_hold=1, restore_hold_set_hash=''"
    )
    connection.execute("UPDATE coordinator_turns SET restore_hold=1")
    connection.execute("UPDATE coordinator_goals SET restore_hold=1")
    for conversation in connection.execute(
        "SELECT conversation_id FROM coordinator_conversations ORDER BY conversation_id"
    ).fetchall():
        conversation_id = str(conversation[0])
        items = [
            f"turn:{row[0]}"
            for row in connection.execute(
                "SELECT turn_id FROM coordinator_turns WHERE conversation_id=? AND restore_hold=1 ORDER BY turn_id",
                (conversation_id,),
            )
        ]
        items.extend(
            f"goal:{row[0]}"
            for row in connection.execute(
                "SELECT goal_id FROM coordinator_goals WHERE conversation_id=? AND restore_hold=1 ORDER BY goal_id",
                (conversation_id,),
            )
        )
        items.extend(
            f"request:{row[0]}"
            for row in connection.execute(
                "SELECT p.request_id FROM provider_requests p JOIN provider_sessions s ON s.session_id=p.session_id "
                "WHERE s.conversation_id=? AND p.restore_hold=1 ORDER BY p.request_id",
                (conversation_id,),
            )
        )
        hold_hash = _restore_hold_digest(items)
        connection.execute(
            "UPDATE coordinator_conversations SET restore_hold_set_hash=? WHERE conversation_id=?",
            (hold_hash, conversation_id),
        )
        connection.execute(
            "UPDATE provider_requests SET restore_hold_set_hash=? WHERE restore_hold=1 AND session_id IN "
            "(SELECT session_id FROM provider_sessions WHERE conversation_id=?)",
            (hold_hash, conversation_id),
        )

    # Requests without a coordinator session remain inert history. They receive their own
    # deterministic receipt so later maintenance cannot mistake an empty hash for permission.
    unscoped = connection.execute(
        "SELECT request_id FROM provider_requests WHERE restore_hold=1 AND restore_hold_set_hash='' ORDER BY request_id"
    ).fetchall()
    for row in unscoped:
        connection.execute(
            "UPDATE provider_requests SET restore_hold_set_hash=? WHERE request_id=?",
            (_restore_hold_digest([f"request:{row[0]}"]), row[0]),
        )


def _clear_completed_projection_metadata(connection: sqlite3.Connection) -> None:
    for row in connection.execute(
        "SELECT lease_id, account_id, tenant_id, owner_id, worker_id, receipt_hash "
        "FROM provider_account_projections WHERE state='complete'"
    ).fetchall():
        binding = json.dumps(
            {
                "version": 1,
                "kind": "inert_restore_projection",
                "lease_id": str(row["lease_id"]),
                "account_id": str(row["account_id"]),
                "tenant_id": str(row["tenant_id"]),
                "owner_id": str(row["owner_id"]),
                "worker_id": str(row["worker_id"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        metadata = json.dumps(
            {
                "version": 1,
                "kind": "inert_restore_audit",
                "state": "complete",
                "receipt_hash": str(row["receipt_hash"] or ""),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE provider_account_projections SET binding_json=?, metadata_json=?, "
            "recovery_token='', recovery_expires_at=NULL WHERE lease_id=?",
            (binding, metadata, row["lease_id"]),
        )


def _verified_traces(store: Store) -> tuple[dict[str, list], dict[str, list]]:
    """Use the live readers on the exact SQLite copy before changing any history."""
    callbacks: dict[str, list] = {}
    work: dict[str, list] = {}
    with store._connect() as connection:
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("GlassHive continuity has invalid record references")
        for row in connection.execute("SELECT * FROM callback_trace_events ORDER BY run_id, run_sequence"):
            callbacks.setdefault(row["run_id"], []).append(row)
        for row in connection.execute("SELECT * FROM work_trace_events ORDER BY run_id, sequence"):
            work.setdefault(row["run_id"], []).append(row)
        projections = connection.execute("SELECT run_id, payload_json FROM events WHERE event_type='continuity.projected'").fetchall()
    for run_id, rows in callbacks.items():
        verified_callback_trace_snapshots(rows, run_id)
    for run_id, rows in work.items():
        if store.work_trace_detail(run_id=run_id, tenant_id=rows[0]["tenant_id"], owner_id=rows[0]["owner_id"]) is None:
            raise ValueError("GlassHive work trace has no authorized owner")
        ledger = {"sha256:" + row["event_sha256"]: row for row in callbacks.get(run_id, [])}
        for event in rows:
            if event["event_type"] != "callback.delivery":
                continue
            receipt = json.loads(event["payload_json"])
            callback = ledger.get(receipt.get("eventSha256"))
            if callback is None:
                raise ValueError("GlassHive work trace references missing callback history")
            expected = {"authoritySha256": callback["authority_sha256"], "payloadSha256": callback["payload_sha256"],
                "ledgerSequence": callback["run_sequence"], "callbackRevision": callback["callback_sequence"],
                "callbackRef": "callback_sha256:" + _text_sha256(callback["callback_id"]),
                "previousEventSha256": "sha256:" + callback["previous_event_sha256"] if callback["previous_event_sha256"] else None}
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError("GlassHive work trace callback evidence disagrees")
    for row in projections:
        value = json.loads(row["payload_json"])
        if not isinstance(value, dict):
            raise ValueError("GlassHive continuity trace provenance is invalid")
        run_callbacks, run_work = callbacks.get(row["run_id"], []), work.get(row["run_id"], [])
        for prefix, rows in (("Callback", run_callbacks), ("Work", run_work)):
            count = value.get(f"exported{prefix}EventCount")
            if (value.get("version") != 1 or not isinstance(count, int) or isinstance(count, bool)
                or count < 0 or count > len(rows)):
                raise ValueError("GlassHive continuity trace provenance is invalid")
            expected = "sha256:" + (rows[count - 1]["event_sha256"] if count else "0" * 64)
            if value.get(f"exported{prefix}HeadSha256") != expected:
                raise ValueError("GlassHive continuity trace provenance is invalid")
            original = value.get(f"source{prefix}HeadSha256", "")
            if not isinstance(original, str) or len(original) != 71 or not original.startswith("sha256:") or any(c not in "0123456789abcdef" for c in original[7:]):
                raise ValueError("GlassHive continuity trace provenance is invalid")
        matching = [event for event in run_work if event["event_type"] == "continuity.projected"
                    and json.loads(event["payload_json"]) == value]
        if run_work and len(matching) != 1:
            raise ValueError("GlassHive continuity trace provenance is not linked")
    for run_id, rows in work.items():
        for event in rows:
            if event["event_type"] == "continuity.projected" and sum(
                row["run_id"] == run_id and json.loads(row["payload_json"]) == json.loads(event["payload_json"])
                for row in projections
            ) != 1:
                raise ValueError("GlassHive continuity trace provenance is not linked")
    return callbacks, work


def _project_callback_history(store: Store) -> set[str]:
    """Project only the disposable capture DB; live append-only triggers never change."""
    callbacks, work = _verified_traces(store)
    tokens = {str(json.loads(row["snapshot_json"])["deliveryLeaseToken"])
              for rows in callbacks.values() for row in rows
              if json.loads(row["snapshot_json"])["deliveryLeaseToken"]}
    if not tokens:
        return tokens

    def redact(value: str) -> str:
        def replace(text: str) -> str:
            for token in sorted(tokens, key=len, reverse=True):
                text = text.replace(token, "[REDACTED_CALLBACK_LEASE]")
            return text
        return _map_json_text(value, replace)

    changed_runs = {run_id for run_id, rows in callbacks.items()
                    if any(json.loads(row["snapshot_json"])["deliveryLeaseToken"] for row in rows)}
    changed_fields = {"callback_trace_events.snapshot_json", "callback_outbox.delivery_lease_token"}
    replacements: dict[str, dict] = {}
    trigger_names = ("callback_trace_events_append_only_update", "work_trace_events_append_only_update",
                     "callback_outbox_trace_update", "callback_outbox_authority_immutable")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        triggers = {name: connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()[0]
                    for name in trigger_names}
        for name in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        # These are existing copies of callback observations, not a second history representation.
        for table, key, columns in (("callback_outbox", "callback_id", ("url", "payload_json", "last_error")),
                                    ("events", "event_id", ("message", "payload_json")),
                                    ("provider_activity", "sequence_id", ("summary", "payload_json"))):
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall():
                for column in columns:
                    original = row[column]
                    projected = redact(original) if isinstance(original, str) else original
                    if projected != original:
                        connection.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{key}"=?', (projected, row[key]))
                        changed_fields.add(f"{table}.{column}")
        connection.execute("UPDATE callback_outbox SET delivery_lease_token='', delivery_lease_expires_at=NULL")
        for run_id, rows in callbacks.items():
            previous = ""
            for row in rows:
                snapshot = json.loads(redact(row["snapshot_json"]))
                if snapshot["deliveryLeaseToken"]:
                    snapshot["deliveryLeaseExpiresAt"] = None
                snapshot["deliveryLeaseToken"] = ""
                encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                event_sha = _callback_trace_event_sha256_values(row["callback_id"], row["callback_sequence"], row["run_sequence"], encoded, previous)
                payload_sha = "sha256:" + _text_sha256(snapshot["payloadJson"])
                authority_sha = "sha256:" + _text_sha256(_callback_trace_authority_values(*(snapshot[field] for field in CALLBACK_TRACE_AUTHORITY_FIELDS)))
                if encoded != row["snapshot_json"]:
                    changed_runs.add(run_id)
                replacements[row["event_sha256"]] = {"eventSha256": "sha256:" + event_sha,
                    "previousEventSha256": "sha256:" + previous if previous else None,
                    "payloadSha256": payload_sha, "authoritySha256": authority_sha}
                connection.execute("UPDATE callback_trace_events SET snapshot_json=?, payload_sha256=?, authority_sha256=?, previous_event_sha256=?, event_sha256=? WHERE callback_trace_event_id=?",
                    (encoded, payload_sha, authority_sha, previous, event_sha, row["callback_trace_event_id"]))
                previous = event_sha
        for run_id, rows in work.items():
            previous = ""
            for row in rows:
                payload = json.loads(redact(row["payload_json"]))
                if row["event_type"] == "callback.delivery":
                    old_sha = str(payload.get("eventSha256") or "").removeprefix("sha256:")
                    if old_sha not in replacements:
                        raise ValueError("GlassHive work trace references missing callback history")
                    payload.update(replacements[old_sha])
                event_sha = store._work_trace_event_sha256(trace_event_id=row["trace_event_id"], run_id=run_id,
                    work_ref=row["work_ref"], sequence=row["sequence"], event_type=row["event_type"], payload=payload,
                    previous_event_sha256=previous, created_at=row["created_at"])
                if event_sha != row["event_sha256"]:
                    changed_runs.add(run_id)
                    changed_fields.add("work_trace_events.payload_json")
                connection.execute("UPDATE work_trace_events SET payload_json=?, previous_event_sha256=?, event_sha256=? WHERE trace_event_id=?",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), previous, event_sha, row["trace_event_id"]))
                previous = event_sha
        for name, sql in triggers.items():
            connection.execute(sql)
        for run_id in sorted(changed_runs):
            source_callbacks, source_work = callbacks.get(run_id, []), work.get(run_id, [])
            value = {"version": 1, "redactedFields": sorted(changed_fields)}
            for prefix, table, rows in (("Callback", "callback_trace_events", source_callbacks), ("Work", "work_trace_events", source_work)):
                order = "run_sequence" if prefix == "Callback" else "sequence"
                latest = connection.execute(f'SELECT event_sha256 FROM "{table}" WHERE run_id=? ORDER BY "{order}" DESC LIMIT 1', (run_id,)).fetchone()
                value[f"source{prefix}HeadSha256"] = "sha256:" + (rows[-1]["event_sha256"] if rows else "0" * 64)
                value[f"exported{prefix}HeadSha256"] = "sha256:" + (latest[0] if latest else "0" * 64)
                value[f"exported{prefix}EventCount"] = len(rows)
            now = utc_now()
            identity = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            connection.execute("INSERT INTO events(event_id, project_id, worker_id, tenant_id, run_id, event_type, message, payload_json, created_at) VALUES (?, ?, ?, ?, ?, 'continuity.projected', 'Credential-free history captured; source heads retained', ?, ?)",
                ("evt_continuity_" + identity, run["project_id"], run["worker_id"], run["tenant_id"], run_id, json.dumps(value, sort_keys=True), now))
            if source_work:
                scope = source_work[0]
                store._append_work_trace_event_conn(connection, trace_event_id="trace_continuity_" + identity,
                    run_id=run_id, work_ref=scope["work_ref"], tenant_id=scope["tenant_id"], owner_id=scope["owner_id"],
                    event_type="continuity.projected", payload=value, created_at=now)
    _verified_traces(store)
    return tokens


def _map_json_text(value: str, transform) -> str:
    """Visit decoded values, keys and embedded JSON before inspecting serialized bytes."""
    def visit(item):
        if isinstance(item, str):
            return _map_json_text(item, transform)
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            projected = {visit(key): visit(child) for key, child in item.items()}
            if len(projected) != len(item):
                raise ValueError("GlassHive callback projection would merge history fields")
            return projected
        return item

    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return transform(value)
    projected = visit(decoded)
    if projected != decoded:
        return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return transform(value)


def _require_no_callback_credentials(root: Path, tokens: set[str] | frozenset[str] = frozenset()) -> None:
    """Reject unhandled duplicates in decoded state and serialized/free-page/file bytes."""
    known = tuple(token.encode() for token in tokens)
    def inspect_bytes(value: bytes) -> None:
        if any(token in value for token in known) or re.search(rb"cbdel_[0-9a-f]{32}", value):
            raise ValueError("GlassHive portable state contains a callback credential copy")

    def inspect_text(value: str) -> str:
        inspect_bytes(value.encode())
        return value

    with _sqlite_connection(f"{(root / 'runtime.sqlite').resolve().as_uri()}?mode=ro", uri=True) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            quoted = table.replace('"', '""')
            for row in connection.execute(f'SELECT * FROM "{quoted}"'):
                for value in row:
                    if isinstance(value, str):
                        _map_json_text(value, inspect_text)
    # Decode Unicode escapes conservatively in any file, including serialized JSON
    # strings and SQLite free pages. Semantic JSON traversal above is unbounded by chunks.
    overlap = max(4096, max((len(value) * 6 for value in known), default=0))
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        tail = b""
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                block = tail + block
                inspect_bytes(block)
                decoded = block
                while True:
                    projected = re.sub(rb"\\+u00([0-9a-fA-F]{2})", lambda match: bytes([int(match[1], 16)]), decoded)
                    if projected == decoded:
                        break
                    decoded = projected
                inspect_bytes(decoded)
                tail = block[-overlap:]


def _reset_authority(store: Store) -> None:
    """Invalidate execution authority without turning unfinished work into success."""
    now = utc_now()
    for worker in store.list_all_workers():
        store.update_worker(
            worker["worker_id"], **COMPUTE_OPERATION_CLEAR_FIELDS,
            state=worker["state"] if worker["state"] in {"terminated", "failed"} else "paused",
            pid=None, gateway_token=None, session_key=None, gateway_url=None,
            gateway_port=None, takeover_url=None, control_url=None,
            bootstrap_bundle_json=json.dumps(_durable_bootstrap(json.loads(worker.get("bootstrap_bundle_json") or "null"))),
        )
    with store._connect() as connection:
        connection.execute("PRAGMA secure_delete=ON")
        _require_exportable_control_plane(connection)
        connection.execute("UPDATE provider_accounts SET status=CASE WHEN status='disconnected' THEN status ELSE 'action_required' END, secret_locator='', last_verified_at=NULL, reconnect_reason=CASE WHEN status='disconnected' THEN reconnect_reason ELSE 'restore_reauthentication_required' END")
        connection.execute("UPDATE control_plane_connections SET status='action_required', secret_locator='', last_verified_at=NULL, error_code='restore_reauthentication_required'")
        connection.execute("UPDATE provider_account_leases SET released_at=COALESCE(released_at, ?)", (time.time(),))
        connection.execute("UPDATE workspace_capability_grants SET revoked_at=COALESCE(revoked_at, ?), prior_bootstrap_bundle_json='{}', applied_bootstrap_bundle_json='{}'", (time.time(),))
        connection.execute("UPDATE control_plane_pending_changes SET status='expired', confirmation_hash='' WHERE status='pending'")
        connection.execute("UPDATE schedule_principal_authority SET enabled=0, authority_epoch=authority_epoch+1")
        connection.execute("UPDATE host_run_leases SET status='released', pid=NULL, process_group=NULL, process_start_identity='', startup_token='', startup_state='aborted', released_at=COALESCE(released_at, ?), release_reason='restore_reauthentication_required'", (now,))
        connection.execute("UPDATE preflight_capacity_reservations SET status='released', released_at=COALESCE(released_at, ?), release_reason='restore_reauthentication_required'", (now,))
        connection.execute("DELETE FROM internal_assertion_replay_cache")
        connection.execute("DELETE FROM service_assertion_nonces")
        for row in connection.execute("SELECT run_id, runtime_bundle_json FROM runs WHERE runtime_bundle_json IS NOT NULL").fetchall():
            connection.execute("UPDATE runs SET runtime_bundle_json=? WHERE run_id=?", (json.dumps(_durable_bootstrap(json.loads(row["runtime_bundle_json"]))), row["run_id"]))
        connection.execute("UPDATE runs SET native_capabilities_json='{}', capacity_reservation_json='{}', native_session_id=''")
        callbacks = connection.execute("SELECT callback_id FROM callback_outbox WHERE status NOT IN ('delivered','dead_lettered','superseded') OR delivery_lease_token!=''").fetchall()
        for callback in callbacks:
            connection.execute("UPDATE callback_outbox SET status=CASE WHEN status IN ('delivered','dead_lettered','superseded') THEN status ELSE 'dead_lettered' END, delivery_lease_token='', delivery_lease_expires_at=NULL, last_error='restore_reauthentication_required', updated_at=? WHERE callback_id=?", (now, callback["callback_id"]))
            store._append_callback_trace_conn(connection, callback_id=callback["callback_id"])
        connection.execute("UPDATE active_work_action_uses SET status='failed', executor_id='', lease_expires_at=NULL, last_error='restore_reauthentication_required' WHERE status NOT IN ('completed','failed')")
        connection.execute("UPDATE capability_grant_revocations SET status='failed', lease_owner='', lease_expires_at=NULL, last_error_code='restore_reauthentication_required' WHERE status!='applied'")
        # Historical manifests retain file identity. Source workspace inode and
        # projection control receipts cannot grant authority on the destination.
        connection.execute("DELETE FROM workspace_file_projection_targets")
        # Historical effects remain auditable but must not replay external callbacks.
        connection.execute("UPDATE lifecycle_operation_effects SET status='failed', lease_owner='', lease_expires_at=NULL, next_attempt_at=NULL, last_error_code='restore_reauthentication_required' WHERE status!='applied'")
        _clear_completed_projection_metadata(connection)
        _reset_restore_authority(connection)
    # Remove old credential bytes from free pages; WAL/SHM are never artifacts.
    with store._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")


def capture_state(database: Path, output: Path) -> dict[str, int]:
    """Capture stable work and existing safe workspace files, never provider homes."""
    metadata = database.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ValueError("GlassHive database is not an owned regular file")
    if output.exists() or output.is_symlink():
        raise ValueError("GlassHive continuity output already exists")
    output.mkdir(parents=True, mode=0o700)
    store = None
    try:
        with _sqlite_connection(f"{database.resolve().as_uri()}?mode=ro", uri=True) as source:
            _require_schema(source)
            _require_quiescent(source, database)
            _require_exportable_control_plane(source)
            with _sqlite_connection(output / "runtime.sqlite") as target:
                source.backup(target)
        store = Store(str(output / "runtime.sqlite"))
        callback_tokens = _project_callback_history(store)
        runtime = None
        with store._connect() as connection:
            workers = [dict(row) for row in connection.execute("SELECT * FROM workers")]
        copied_files = 0
        copied_directories = 0
        copied_bytes = 0
        deadline = time.monotonic() + 300
        copied_roots: set[Path] = set()
        for worker in workers:
            with store._connect() as connection:
                workspace_row = connection.execute(
                    "SELECT mode FROM execution_workspaces WHERE workspace_id=? AND tenant_id=? AND owner_id=?",
                    (worker["workspace_id"], worker["tenant_id"], worker["owner_id"]),
                ).fetchone()
                quota_required = bool(connection.execute(
                    "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
                ).fetchone()[0])
                box_layout = workspace_row is not None and (workspace_row["mode"] == "shared" or packaged_linux() or quota_required)
                if box_layout:
                    source_data, source_control = _source_box_roots(connection, worker)
                    source_box = _shared_box(connection, worker, data_root=source_data, control_root=source_control)
                    archive_data, archive_control = _archive_box_roots(connection, worker, output)
                    target_box = _shared_box(connection, worker, data_root=archive_data, control_root=archive_control)
                    state = target_box.paths()["state_dir"]
                    workspace = target_box.paths()["workspace_dir"]
                    raw_source = str(source_box.paths()["workspace_dir"])
                else:
                    if runtime is None:
                        runtime = ProfiledWorkerRuntime(base_dir=str(output), provider_account_db_path=str(output / "runtime.sqlite"))
                    state, workspace = _worker_paths(runtime, worker, output)
                    raw_source = str(worker.get("workspace_dir") or "")
            state.mkdir(parents=True, mode=0o700, exist_ok=True)
            workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
            if raw_source and workspace not in copied_roots:
                source_root = Path(raw_source)
                files, _, result = _workspace_copy_plan(source_root)
                if result == "missing":
                    raise ValueError("GlassHive recorded workspace is missing; recover it before capture")
                for source_directory, relative in _workspace_directory_plan(source_root):
                    before = source_directory.lstat()
                    if not stat.S_ISDIR(before.st_mode):
                        raise ValueError("GlassHive workspace directory changed during capture")
                    (workspace / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
                    after = source_directory.lstat()
                    if ((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
                        raise ValueError("GlassHive workspace directory changed during capture")
                    copied_directories += 1
                for source_file, relative in files:
                    if any(part.casefold() in NON_DELIVERABLE_DIR_NAMES for part in relative.parts):
                        continue
                    source_mode = source_file.lstat().st_mode
                    copied_bytes += _copy_regular_workspace_file(
                        source_file, workspace / relative, source_root,
                        max_bytes=20 * 1024**3 - copied_bytes, deadline=deadline,
                    )
                    if source_mode & stat.S_IXUSR:
                        (workspace / relative).chmod(0o700)
                    copied_files += 1
                copied_roots.add(workspace)
            with store._connect() as connection:
                connection.execute(
                    "UPDATE workers SET state_dir=?, workspace_dir=?, workspace_root=? WHERE worker_id=?",
                    (str(state.relative_to(output)), str(workspace.relative_to(output)), str(workspace.relative_to(output)), worker["worker_id"]),
                )
        managed_files, managed_bytes = _managed_file_versions(store, output, database.parent)
        from .workspace_file_continuity import project_applied_deletes
        bound_deletes = project_applied_deletes(database, store, output)
        _reset_authority(store)
        _verified_traces(store)
        store.close()
        with _sqlite_connection(output / "runtime.sqlite") as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("GlassHive continuity failed SQLite integrity checking")
        _private_tree(output)
        _require_no_callback_credentials(output, callback_tokens)
        return {"workers": len(workers), "workspace_files": copied_files, "workspace_bytes": copied_bytes,
                "workspace_directories": copied_directories,
                "managed_files": managed_files, "managed_bytes": managed_bytes,
                "file_deletes_needing_target_binding": bound_deletes}
    except BaseException:
        if store is not None:
            store.close()
        shutil.rmtree(output)
        raise


def prepare_restored_state(stage: Path, final_root: Path, *, data_root: Path | None = None,
                           control_root: Path | None = None) -> None:
    """Bind portable workspace references to the destination before activation."""
    _private_tree(stage)
    if not final_root.is_absolute():
        raise ValueError("GlassHive restore destination must be absolute")
    if packaged_linux():
        data_root = data_root or Path(str(os.environ.get("XPERFECT_SHARED_VOLUME_ROOT") or ""))
        control_root = control_root or Path(str(os.environ.get("XPERFECT_CONTROL_ROOT") or ""))
    else:
        data_root = data_root or final_root / "data"
        control_root = control_root or final_root / "control"
    if any(not root.is_absolute() or root != root.resolve(strict=False) for root in (data_root, control_root)):
        raise ValueError("GlassHive restore Native roots are unavailable")
    database = stage / "runtime.sqlite"
    _require_no_callback_credentials(stage)
    with _sqlite_connection(database) as connection:
        _require_schema(connection)
        _require_exportable_trace(connection)
        _require_exportable_control_plane(connection)
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("GlassHive restore failed SQLite integrity checking")
        connection.row_factory = sqlite3.Row
        from .workspace_file_continuity import require_inert_applied_deletes
        require_inert_applied_deletes(connection, stage)
        quota_required = bool(connection.execute(
            "SELECT quota_required FROM workspace_file_storage_config WHERE singleton=1"
        ).fetchone()[0])
        if quota_required:
            backend = _quota_backend()
            owners = {tuple(row) for row in connection.execute(
                "SELECT DISTINCT tenant_id,owner_id FROM workers"
            )}
            owners.update(tuple(row) for row in connection.execute(
                "SELECT DISTINCT tenant_id,owner_id FROM workspace_file_uploads"
            ))
            for tenant, owner in sorted(owners):
                digest = hashlib.sha256(f"{tenant}\0{owner}".encode()).hexdigest()
                expected = data_root / "owners" / digest
                if _quota_owner_root(connection, backend, tenant, owner) != expected:
                    raise ValueError("GlassHive quota continuity target owner root differs from its native binding")
        for row in connection.execute("SELECT worker_id, state_dir, workspace_dir, workspace_root FROM workers").fetchall():
            values = []
            for field in ("state_dir", "workspace_dir", "workspace_root"):
                relative = Path(row[field])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("GlassHive restored workspace reference is unsafe")
                if relative.parts and relative.parts[0] == "data":
                    destination = data_root.joinpath(*relative.parts[1:])
                elif relative.parts and relative.parts[0] == "control":
                    destination = control_root.joinpath(*relative.parts[1:])
                else:
                    destination = final_root / relative
                values.append(str(destination))
            connection.execute("UPDATE workers SET state_dir=?, workspace_dir=?, workspace_root=? WHERE worker_id=?", (*values, row["worker_id"]))
        connection.execute("UPDATE provider_sessions SET workspace_dir=(SELECT workspace_dir FROM workers WHERE workers.worker_id=provider_sessions.worker_id)")
    # Apply the same component-owned authority reset even to a supplied archive.
    store = Store(str(database))
    try:
        _managed_file_versions(store, stage)
        _verified_traces(store)
        _reset_authority(store)
        _verified_traces(store)
    finally:
        store.close()
    with _sqlite_connection(database) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and checkpoint[0]:
            raise ValueError("GlassHive restored SQLite stage still has a WAL writer")
    with _sqlite_connection(database) as connection:
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise ValueError("GlassHive restored SQLite stage could not settle its WAL")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("GlassHive restored SQLite stage is invalid")
    _private_tree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("database", type=Path)
    capture.add_argument("output", type=Path)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("stage", type=Path)
    prepare.add_argument("destination", type=Path)
    prepare.add_argument("--data-root", type=Path)
    prepare.add_argument("--control-root", type=Path)
    quiescence = commands.add_parser("quiescent")
    quiescence.add_argument("database", type=Path)
    quiescence.add_argument("--incoming", action="store_true",
                            help="Run by the release about to inherit this state (an upgrade)")
    unpublished = commands.add_parser(
        "reconcile-unpublished",
        help="Run only with the package's own runtime configuration (the upgrade's helper loads it)")
    unpublished.add_argument("database", type=Path)
    unpublished.add_argument("--apply", action="store_true",
                             help="Retire them; without it, only report what qualifies")
    unpublished.add_argument("--expect", default=None,
                             help="Comma-separated projection IDs reviewed before; any other set refuses")
    unpublished.add_argument("--incoming", action="store_true",
                             help="Run by the release about to inherit this state (an upgrade)")
    args = parser.parse_args()
    if args.operation == "capture":
        capture_state(args.database, args.output)
    elif args.operation == "prepare":
        prepare_restored_state(args.stage, args.destination, data_root=args.data_root,
                               control_root=args.control_root)
    elif args.operation == "reconcile-unpublished":
        expect = None if args.expect is None else [item for item in args.expect.split(",") if item]
        print(json.dumps(reconcile_unpublished_projections(args.database, apply=args.apply, expect=expect,
                                                           incoming=args.incoming), sort_keys=True))
    else:
        check_quiescent(args.database, incoming=args.incoming)


if __name__ == "__main__":
    main()
