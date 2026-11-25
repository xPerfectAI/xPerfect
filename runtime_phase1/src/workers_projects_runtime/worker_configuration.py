"""Owner configuration narrows existing context and tool authority; it never grants it."""

from __future__ import annotations

import base64
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bootstrap import (
    bootstrap_bundle_for,
    sandbox_setup_for,
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    _strip_codex_mcp_server_blocks,
    _source_path_from_entry,
    resolve_authorized_bootstrap_source_path,
    native_input_file_projection,
)
from .deliverables import is_protected_artifact_path
from . import native_transport
from .failure_classification import FailureClassification
from .models import CLOSED_WORKER_STATES
from .schema_version import (
    execute_schema_script,
    record_schema_version,
    require_compatible_schema,
)


class ConfigurationError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


# Shown in settings; it describes the installation, so it never blocks saving a choice.
CONTEXT_ROUTE_ISSUE = "context_retrieval_unavailable"
CONTEXT_ROUTE_MESSAGE = (
    "Part of this background is beyond the inline limit, and this installation gives "
    "workers no way to read the rest. Raise the inline limit or clear some sources."
)


def context_route_unavailable() -> ConfigurationError:
    """A run refused before start because its remaining background has no route."""
    error = ConfigurationError("context_endpoint_unavailable")
    error.failure_classification = FailureClassification(
        failure_class="context_endpoint_unavailable",
        retryable=False,
        user_message=(
            "Part of this worker's background is beyond its inline limit, and this "
            "installation gives workers no way to read the rest."
        ),
        recommended_recovery=(
            "Raise the inline limit or clear some background sources in Workspace "
            "settings, then continue the worker. An operator can instead give workers "
            "a route to the runtime."
        ),
        diagnostic_summary="No worker route to the runtime's context endpoint.",
        structured=True,
    )
    return error


def configuration_needs_attention(issues) -> ConfigurationError:
    """A run refused before start, naming what its settings need."""
    error = ConfigurationError("configuration_needs_attention")
    messages = []
    for issue in issues:
        message = str(issue.get("message") or "")
        if message and message not in messages:
            messages.append(message)
    error.failure_classification = FailureClassification(
        failure_class="configuration_needs_attention",
        retryable=False,
        user_message="This worker's settings need attention before it can start: "
        + " ".join(messages),
        recommended_recovery="Open Workspace settings, resolve these items, then continue the worker.",
        diagnostic_summary="Configuration issues: "
        + ", ".join(sorted({str(issue.get("code") or "") for issue in issues})),
        structured=True,
    )
    return error


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextConfig(Strict):
    mode: Literal["inherit", "selected"] = "inherit"
    source_ids: list[str] = Field(default_factory=list, max_length=512)
    inline_chars: int = Field(default=24000, ge=0, le=262144, strict=True)


class ToolConfig(Strict):
    mode: Literal["inherit", "selected"] = "inherit"
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=256)


class BackgroundConfig(Strict):
    enabled: bool = Field(default=True, strict=True)
    max_parallel_runs: int = Field(default=32, ge=1, le=32, strict=True)


class PerformanceConfig(Strict):
    native_warm_servers: int = Field(default=0, ge=0, le=8, strict=True)
    native_warm_idle_seconds: int = Field(default=120, ge=10, le=3600, strict=True)


class WorkerConfig(Strict):
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    background: BackgroundConfig = Field(default_factory=BackgroundConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)

    @model_validator(mode="after")
    def selection(self):
        for value, ids in (
            (self.context, self.context.source_ids),
            (self.tools, self.tools.mcp_server_ids),
        ):
            if len(ids) != len(set(ids)) or any(not x or len(x) > 256 for x in ids):
                raise ValueError("Selections require unique bounded IDs")
            if value.mode == "inherit" and ids:
                raise ValueError("Inherited configuration has no explicit selection")
        return self


class ConfigurationUpdate(WorkerConfig):
    expected_revision: int = Field(ge=1, strict=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _worker(conn, worker_id, tenant, owner):
    row = conn.execute(
        "SELECT * FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",
        (worker_id, tenant, owner),
    ).fetchone()
    if row is None or row["state"] in CLOSED_WORKER_STATES:
        raise ConfigurationError("worker_unavailable", 404)
    return dict(row)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_configurations (
 worker_id TEXT PRIMARY KEY REFERENCES workers(worker_id), revision INTEGER NOT NULL,
 config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_context_snapshots (
 run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, worker_id TEXT NOT NULL,
 revision INTEGER NOT NULL, source_id TEXT NOT NULL, sha256 TEXT NOT NULL,
 content TEXT NOT NULL, PRIMARY KEY(run_id,attempt_id,source_id)
);
"""


def mcp_servers(bundle):
    raw = bundle.get("claude_project_mcp") or {}
    if not isinstance(raw, dict):
        raise ConfigurationError("tool_configuration_invalid")
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict) or any(
        not isinstance(v, dict) for v in servers.values()
    ):
        raise ConfigurationError("tool_configuration_invalid")
    return servers


def configured_connections(bundle, *, profile=None):
    """Catalog declared MCP connections the selected native harness can project."""
    try:
        codex = tomllib.loads(str(bundle.get("codex_config_append") or "")).get(
            "mcp_servers", {}
        )
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError("tool_configuration_invalid") from exc
    if not isinstance(codex, dict) or any(
        not isinstance(v, dict) for v in codex.values()
    ):
        raise ConfigurationError("tool_configuration_invalid")
    grok = {}
    raw_grok = bundle.get("grok_mcp_servers")
    if raw_grok is not None:
        if not isinstance(raw_grok, list) or any(
            not isinstance(item, dict) for item in raw_grok
        ):
            raise ConfigurationError("tool_configuration_invalid")
        for item in raw_grok:
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise ConfigurationError("tool_configuration_invalid")
            if name in grok:
                raise ConfigurationError("tool_configuration_invalid")
            grok[name] = item
    claude = mcp_servers(bundle)
    if profile is None:
        # Mutation/cleanup paths need every declared connection, including
        # declarations for other harnesses in the same portable bundle.
        return dict(claude) | codex | grok
    def enabled(declarations):
        return {
            name: value for name, value in declarations.items()
            if value.get("disabled") is not True and value.get("enabled") is not False
        }
    if profile in {"direct-openai", "direct-anthropic", "direct-xai"}:
        return enabled(claude) | enabled(codex) | enabled(grok)
    if profile == "claude-code":
        return enabled(claude)
    if profile == "codex-cli":
        return enabled(codex)
    if profile == "grok-build":
        # ACP accepts explicit Grok servers and compatible Claude MCP entries.
        disabled_explicit = set(grok) - set(enabled(grok))
        return {
            name: value for name, value in enabled(claude).items()
            if name not in disabled_explicit
        } | enabled(grok)
    return {}


def restrict_codex_connections(text, removed):
    """Fail closed if legacy serialization cannot faithfully narrow parsed TOML."""
    try:
        before = tomllib.loads(text)
        result = _strip_codex_mcp_server_blocks(text, removed)
        after = tomllib.loads(result)
        servers = before.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("invalid MCP table")
        before["mcp_servers"] = {k: v for k, v in servers.items() if k not in removed}
        after.setdefault("mcp_servers", {})
        if before != after:
            raise ValueError("unsupported MCP serialization")
        return result
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("tool_selection_unsupported") from exc


def _run_context_worker(worker: dict, run: dict) -> dict:
    """Only the owner-admitted continuation supplies newer conversation evidence."""
    worker = {**worker, "_context_run_id": str(run.get("run_id") or "")}
    raw = run.get("continuation_context_json")
    context = json.loads(raw) if isinstance(raw, str) and raw else raw
    inherited = context.get("inherited_conversation") if isinstance(context, dict) else None
    if inherited is None:
        return worker
    bundle = dict(bootstrap_bundle_for(worker))
    bundle["inherited_conversation"] = inherited
    return {**worker, "bootstrap_bundle_json": _json(bundle)}


class WorkerConfiguration:
    def __init__(self, store, peers, *, source_resolver=None, performance_runtime=None):
        self.store, self.peers = store, peers
        self.performance_runtime = performance_runtime
        # Trusted host extension may supply additional currently-authorized source bytes.
        # The same resolver is called for each retrieval, not just at projection time.
        self.source_resolver = source_resolver
        with store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_compatible_schema(
                conn, component="worker_configuration", target_version=2
            )
            execute_schema_script(conn, _SCHEMA)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(worker_configurations)")
            }
            if "context_revision" not in columns:
                conn.execute(
                    "ALTER TABLE worker_configurations ADD COLUMN context_revision INTEGER NOT NULL DEFAULT 1"
                )
                conn.execute(
                    "UPDATE worker_configurations SET context_revision=revision"
                )
            record_schema_version(conn, component="worker_configuration", version=2)

    def restore_native_preparation(self):
        """Restore only saved positive options; preparation does no model work."""
        # Orphan ownership is independent of count, profile, credentials or pause.
        recover = getattr(self.performance_runtime, "recover_native_preparation", None)
        if callable(recover):
            recover()
        configure = getattr(self.performance_runtime, "configure_native_warm", None)
        counts = {"requested": 0, "unavailable": 0}
        if not callable(configure):
            return counts
        with self.store._connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT w.*, c.config_json FROM workers w JOIN worker_configurations c USING(worker_id)"
            ).fetchall()]
        for worker in rows:
            if worker["state"] in CLOSED_WORKER_STATES or worker["state"] == "paused":
                continue
            config = WorkerConfig.model_validate_json(worker.pop("config_json"))
            if not config.performance.native_warm_servers:
                continue
            counts["requested"] += 1
            try:
                status = configure(worker, config.performance.model_dump())
                if not status.get("supported"):
                    counts["unavailable"] += 1
            except (OSError, ValueError, RuntimeError):
                counts["unavailable"] += 1
        return counts

    @staticmethod
    def _config(conn, worker_id):
        row = conn.execute(
            "SELECT * FROM worker_configurations WHERE worker_id=?", (worker_id,)
        ).fetchone()
        return (
            (row["revision"], WorkerConfig.model_validate_json(row["config_json"]))
            if row
            else (1, WorkerConfig())
        )

    @staticmethod
    def _context_revision(conn, worker_id):
        row = conn.execute(
            "SELECT context_revision FROM worker_configurations WHERE worker_id=?",
            (worker_id,),
        ).fetchone()
        return row[0] if row else 1

    def _native_input_home(self, worker):
        # The performance runtime is optional, as everywhere else in this class.
        placement_reader = getattr(getattr(self, "performance_runtime", None), "native_workspace_placement", None)
        try:
            placement = placement_reader(worker) if callable(placement_reader) else None
        except (OSError, RuntimeError, ValueError):
            return None
        if not isinstance(placement, dict) or placement.get("host_native") is not True:
            return None
        home = Path(str(placement.get("home_dir") or ""))
        workspace = Path(str(placement.get("workspace_dir") or ""))
        if (not home.is_absolute() or not workspace.is_absolute()
                or not is_protected_artifact_path(workspace)
                or is_protected_artifact_path(home / ".tmp")):
            return None
        return home

    def sources(self, worker, *, materialize_inputs=False, admitted_inputs=None):
        bundle = bootstrap_bundle_for(worker)
        native_input_home = self._native_input_home(worker) if bundle.get("files") else None
        selected_library = bundle.get("library_selection")
        selected_uploads = selected_library.get("upload_ids", []) if isinstance(selected_library, dict) else []
        if not isinstance(selected_uploads, list) or any(not isinstance(value, str) for value in selected_uploads):
            raise ConfigurationError("library_selection_invalid")
        library_upload_ids = set(selected_uploads)
        result = []
        if isinstance(selected_library, dict) and selected_library.get("items"):
            result.append({"id": "library:selection", "label": "Selected Library items",
                           "kind": "library_selection", "inline_priority": 4,
                           "text": _json({key: selected_library.get(key, []) for key in ("refs", "items", "pending")})})
        # These are exact owner-authored bootstrap values, not descriptors or summaries.
        for key in ("project_definition",):
            text = bundle.get(key)
            if isinstance(text, str) and text:
                result.append(
                    {
                        "id": "bootstrap:" + key,
                        "label": key.replace("_", " ").capitalize(),
                        "kind": "bootstrap",
                        "text": text,
                    }
                )
        for index, entry in enumerate(bundle.get("files") or []):
            if not isinstance(entry, dict):
                continue
            unavailable = False
            text = entry.get("content")
            data = None
            original_source = {"kind": "inline"}
            if isinstance(text, str):
                data = text.encode("utf-8")
                if (str(entry.get("encoding") or "").strip().lower() == "base64"
                        or "content_base64" in entry):
                    try:
                        data = base64.b64decode(
                            str(entry.get("content_base64") or text or ""), validate=True
                        )
                    except ValueError:
                        data = None
            if not isinstance(text, str):
                try:
                    if isinstance(entry.get("content_base64"), str):
                        data = base64.b64decode(
                            entry["content_base64"], validate=True
                        )
                    else:
                        source = _source_path_from_entry(entry)
                        if source is not None:
                            source = resolve_authorized_bootstrap_source_path(
                                entry, source, worker
                            )
                            data = source.read_bytes()
                            original_source = {"kind": "authorized_file", "path": str(source)}
                    if data is not None:
                        text = data.decode("utf-8")
                except UnicodeDecodeError:
                    # Binary files retain their existing native Files path; do not invent a
                    # text extraction or describe a filename as model-readable content.
                    text = None
                except (OSError, PermissionError, ValueError):
                    text = ""
                    unavailable = True
                    data = None
            if isinstance(text, str):
                result.append(
                    {
                        "id": f"bootstrap:file:{index}",
                        "label": str(entry.get("path") or "File"),
                        "kind": "bootstrap",
                        "text": text,
                        "status": "unavailable" if unavailable else "available",
                        # Only an explicit source descriptor changes packing. Native
                        # instructions and other files keep their existing behavior.
                        **({"packing": "lookup"} if entry.get("context_delivery") == "lookup"
                           or entry.get("managed_upload_id") in library_upload_ids else {}),
                    }
                )
            if data is not None and str(entry.get("scope") or "workspace") == "workspace":
                # This is verified input provenance, not permission to publish the file.
                # Keep binary inputs out of text content while exposing their existing source.
                native_input = None
                file_id = str(entry.get("file_id") or "")
                input_path = str(entry.get("path") or "")
                admitted = (admitted_inputs or {}).get((file_id, input_path))
                if admitted is not None:
                    if ((admitted.get("sha256") is not None
                         and admitted["sha256"] != hashlib.sha256(data).hexdigest())
                            or (admitted.get("size_bytes") is not None
                                and admitted["size_bytes"] != len(data))):
                        raise ConfigurationError("context_input_changed")
                if (native_input_home is not None and file_id and input_path
                        and not (original_source["kind"] == "authorized_file"
                                 and is_protected_artifact_path(Path(original_source["path"])))):
                    native_input = native_input_file_projection(
                        native_input_home, input_path, data,
                        materialize=materialize_inputs and admitted is not None,
                    )
                result.append({
                    "id": f"bootstrap:file:{index}:provenance",
                    "label": str(entry.get("path") or "File") + " source",
                    "kind": "file_provenance",
                    "text": _json({
                        "version": 1,
                        "scope": "workspace",
                        "materialized_path": str(entry.get("path") or ""),
                        "original_source": original_source,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        **({"native_input": {"root": "managed_tmp", "path": str(native_input)}}
                           if native_input is not None else {}),
                    }),
                    "status": "available",
                    **({"native_input_path": str(native_input), "input_file_id": file_id,
                        "workspace_input_path": input_path} if native_input is not None else {}),
                })
        if self.source_resolver:
            result.extend(self.source_resolver(worker))
        ids = set()
        for source in result:
            if source["id"] in ids or not isinstance(source.get("text"), str):
                raise ConfigurationError("context_source_invalid")
            ids.add(source["id"])
            source.update(
                sha256=_hash(source["text"]),
                chars=len(source["text"]),
                status=source.get("status", "available"),
            )
        return result

    @staticmethod
    def _descriptor(source):
        return {
            key: source[key]
            for key in ("id", "label", "kind", "sha256", "chars", "status")
        } | ({"packing": "lookup"} if source.get("packing") == "lookup" else {})

    def _effective(self, worker, config, *, materialize_inputs=False, admitted_inputs=None):
        sources = self.sources(worker, materialize_inputs=materialize_inputs, admitted_inputs=admitted_inputs)
        bundle = bootstrap_bundle_for(worker)
        profile = str(worker.get("profile") or "").strip()
        servers = configured_connections(bundle, profile=profile)
        if not isinstance(servers, dict):
            raise ConfigurationError("tool_configuration_invalid")
        selected = (
            sources
            if config.context.mode == "inherit"
            else [s for s in sources if s["id"] in config.context.source_ids]
        )
        # Source metadata controls packing, never authority or which sources remain readable.
        selected = sorted(selected, key=lambda source: source.get("inline_priority", 4))
        selected_tools = (
            list(servers)
            if config.tools.mode == "inherit"
            else [s for s in servers if s in config.tools.mcp_server_ids]
        )
        supported_profile = profile in {"claude-code", "codex-cli", "grok-build", "direct-openai", "direct-anthropic", "direct-xai"}
        fixed_broker = bundle.get("execution_policy") == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        selection_supported = supported_profile and not fixed_broker
        unsupported_message = (
            "Connected-tool selection is unavailable for this worker profile."
            if not supported_profile
            else "This runtime requires its scoped connection. Use inherited connected tools."
        )
        issues = []
        if config.tools.mode == "selected" and selection_supported:
            try:
                restrict_codex_connections(
                    str(bundle.get("codex_config_append") or ""),
                    set(servers) - set(config.tools.mcp_server_ids),
                )
            except ConfigurationError:
                selection_supported = False
                unsupported_message = "This connection configuration cannot be narrowed safely."
        if config.tools.mode == "selected" and not selection_supported:
            issues.append(
                {
                    "code": "tool_selection_unsupported",
                    "message": unsupported_message,
                }
            )
        for source in selected:
            if source["status"] != "available":
                issues.append(
                    {
                        "code": "context_unavailable",
                        "source_id": source["id"],
                        "message": "This source cannot be read. Check Files or update Context settings.",
                    }
                )
        for source_id in set(config.context.source_ids) - {s["id"] for s in sources}:
            issues.append(
                {
                    "code": "context_unavailable",
                    "source_id": source_id,
                    "message": "This source is no longer available. Update Context settings.",
                }
            )
        for server in set(config.tools.mcp_server_ids) - set(servers):
            issues.append(
                {
                    "code": "tool_unavailable",
                    "source_id": server,
                    "message": "This tool connection is no longer available. Check Connections.",
                }
            )
        budget, inline = config.context.inline_chars, 0
        descriptors = []
        for source in selected:
            count = 0 if source.get("packing") == "lookup" else min(budget, source["chars"])
            budget -= count
            inline += count
            descriptors.append(
                self._descriptor(source)
                | {
                    "inline_chars": count,
                    "delivery": "unavailable"
                    if source["status"] != "available"
                    else "inline"
                    if count == source["chars"]
                    else "mixed"
                    if count
                    else "retrieval",
                }
            )
        retrievable = sum(s["chars"] for s in selected) - inline
        if retrievable:
            route = (native_transport.worker_route(execution_mode="docker")
                     if worker.get("execution_mode") == "docker" else native_transport.worker_route())
            if route is None:
                issues.append({"code": CONTEXT_ROUTE_ISSUE, "message": CONTEXT_ROUTE_MESSAGE})
        return (
            sources,
            selected,
            servers,
            {
                "context": {
                    "sources": descriptors,
                    "inline_chars": inline,
                    "retrievable_chars": retrievable,
                },
                "tools": {
                    "mcp_server_ids": selected_tools,
                    "scope": "worker_mcp_connections",
                    "selection_supported": selection_supported,
                    "enforcement": "projected_configuration",
                    "native_capabilities": "not_restricted_by_this_setting",
                },
                "background": config.background.model_dump(),
                "performance": config.performance.model_dump(),
                "issues": issues,
            },
        )

    def get(self, tenant, owner, worker_id):
        with self.store._connect() as conn:
            worker = _worker(conn, worker_id, tenant, owner)
            revision, config = self._config(conn, worker_id)
        sources, _, servers, effective = self._effective(worker, config)
        status = getattr(self.performance_runtime, "native_warm_status", None)
        effective["performance"]["native_warm_status"] = (
            status(worker, config.performance.model_dump()) if callable(status) else {
                "supported": False, "state": "unavailable", "idle": 0,
                "reason": "Native warm execution is unavailable in this runtime",
            }
        )
        return {
            "worker_id": worker_id,
            "revision": revision,
            "requested": config.model_dump(),
            "effective": effective,
            "available_sources": [self._descriptor(s) for s in sources],
            "available_tools": [
                {"id": s, "label": s, "status": "available"} for s in servers
            ],
        }

    def put(self, tenant, owner, worker_id, update):
        config = WorkerConfig.model_validate(
            update.model_dump(exclude={"expected_revision"})
        )
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = _worker(conn, worker_id, tenant, owner)
            revision, previous = self._config(conn, worker_id)
            if revision != update.expected_revision:
                raise ConfigurationError("configuration_changed")
            # Older clients save Context/Tools/Background without this new
            # optional section. Preserve their existing performance choice.
            if "performance" not in update.model_fields_set:
                config.performance = previous.performance
            if any(
                issue["code"] != CONTEXT_ROUTE_ISSUE
                for issue in self._effective(worker, config)[3]["issues"]
            ):
                raise ConfigurationError("selection_unavailable")
            generation = self._context_revision(conn, worker_id)
            if (previous.context.mode, set(previous.context.source_ids)) != (
                config.context.mode,
                set(config.context.source_ids),
            ):
                generation += 1
            conn.execute(
                "INSERT INTO worker_configurations(worker_id,revision,config_json,context_revision) VALUES(?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET revision=excluded.revision,config_json=excluded.config_json,context_revision=excluded.context_revision",
                (worker_id, revision + 1, _json(config.model_dump()), generation),
            )
        prepare = getattr(self.performance_runtime, "configure_native_warm", None)
        if callable(prepare):
            prepare(worker, config.performance.model_dump())
        return self.get(tenant, owner, worker_id)

    @staticmethod
    def _page(source, offset, max_chars):
        if not 0 <= offset <= len(source["text"]) or not 1 <= max_chars <= 65536:
            raise ConfigurationError("context_page_invalid", 422)
        end = min(len(source["text"]), offset + max_chars)
        return {
            "source_id": source["id"],
            "sha256": _hash(source["text"]),
            "text": source["text"][offset:end],
            "chars": len(source["text"]),
            "offset": offset,
            "next_offset": end if end < len(source["text"]) else None,
        }

    def read(self, tenant, owner, worker_id, source_id, offset=0, max_chars=12000):
        with self.store._connect() as conn:
            worker = _worker(conn, worker_id, tenant, owner)
        source = next((s for s in self.sources(worker) if s["id"] == source_id), None)
        if source is None or source["status"] != "available":
            raise ConfigurationError("context_unavailable", 404)
        return self._page(source, offset, max_chars)

    def prepare_run(self, worker, run):
        from .provider_network import configured_proxy

        provider_only = configured_proxy() is not None
        with self.store._connect() as conn:
            fresh = _worker(
                conn, worker["worker_id"], worker["tenant_id"], worker["owner_id"]
            )
            _, config = self._config(conn, worker["worker_id"])
            revision = self._context_revision(conn, worker["worker_id"])
        # Transient scoped broker/peer MCP projection is trusted, but context must stay fresh.
        projected = dict(worker)
        # The service captures saved setup before projecting any run authority.
        # A concurrent saved edit cannot label a stale seed as current setup.
        setup = sandbox_setup_for(worker)
        if provider_only and setup != sandbox_setup_for(fresh):
            raise ConfigurationError("configuration_changed")
        projected["_sandbox_setup"] = setup
        source_worker = _run_context_worker(dict(fresh), run)
        runtime_bundle = bootstrap_bundle_for(worker)
        input_manifest_path = str(worker.get("_run_input_manifest_path") or "")
        input_manifest = next((entry for entry in runtime_bundle.get("files", [])
            if isinstance(entry, dict) and entry.get("scope", "workspace") == "workspace"
            and entry.get("path") == input_manifest_path and isinstance(entry.get("content"), str)), None)
        admitted_index = json.loads(input_manifest["content"]) if input_manifest else {}
        admitted_inputs = {(str(entry.get("source_file_id") or ""), str(entry.get("path") or "")): entry
            for entry in admitted_index.get("files", []) if isinstance(entry, dict)
            and entry.get("available") is not False and entry.get("source_file_id")}
        sources, selected, _, effective = self._effective(
            source_worker, config, materialize_inputs=True, admitted_inputs=admitted_inputs,
        )
        # The route belongs to the transport; its binder refuses a run that needs one.
        blocking = [i for i in effective["issues"] if i["code"] != CONTEXT_ROUTE_ISSUE]
        if blocking:
            raise configuration_needs_attention(blocking)
        bundle = json.loads(_json(bootstrap_bundle_for(worker)))
        native_inputs = {(source["input_file_id"], source["workspace_input_path"]): source["native_input_path"]
            for source in sources if source.get("native_input_path")}
        if input_manifest and native_inputs:
            for entry in admitted_index.get("files", []):
                key = (str(entry.get("source_file_id") or ""), str(entry.get("path") or ""))
                if entry.get("available") is not False and key in native_inputs:
                    entry["path"] = native_inputs[key]
            for entry in bundle.get("files", []):
                if (isinstance(entry, dict) and entry.get("scope", "workspace") == "workspace"
                        and entry.get("path") == input_manifest_path):
                    entry["content"] = _json(admitted_index)
        # Derived execution settings have one owner: this worker's saved config.
        # Both conversation/provider-session and ordinary workers use this path.
        bundle["runtime_performance"] = config.performance.model_dump()
        # Restrict only owner-configured external servers; keep required runtime control tools.
        servers = mcp_servers(bundle)
        if config.tools.mode == "selected":
            base_ids = set(configured_connections(bootstrap_bundle_for(fresh)))
            bundle["claude_project_mcp"] = {
                "mcpServers": {
                    k: v
                    for k, v in servers.items()
                    if k not in base_ids or k in config.tools.mcp_server_ids
                }
            }
            bundle["_configured_removed_mcp_servers"] = sorted(
                base_ids - set(config.tools.mcp_server_ids)
            )
            bundle["codex_config_append"] = restrict_codex_connections(
                str(bundle.get("codex_config_append") or ""),
                base_ids - set(config.tools.mcp_server_ids),
            )
        attempt = str(
            run.get("active_attempt_id") or worker.get("_run_attempt_id") or ""
        )
        if not attempt:
            raise ConfigurationError("context_attempt_unavailable")
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if provider_only:
                current = _worker(conn, worker["worker_id"], worker["tenant_id"], worker["owner_id"])
                if setup != sandbox_setup_for(current):
                    raise ConfigurationError("configuration_changed")
            if self._context_revision(conn, worker["worker_id"]) != revision:
                raise ConfigurationError("configuration_changed")
            for source in selected:
                old = conn.execute(
                    "SELECT sha256,revision FROM worker_context_snapshots WHERE run_id=? AND attempt_id=? AND source_id=?",
                    (run["run_id"], attempt, source["id"]),
                ).fetchone()
                if old and (
                    old["sha256"] != source["sha256"] or old["revision"] != revision
                ):
                    raise ConfigurationError("context_snapshot_changed")
                conn.execute(
                    "INSERT OR IGNORE INTO worker_context_snapshots VALUES(?,?,?,?,?,?,?)",
                    (
                        run["run_id"],
                        attempt,
                        worker["worker_id"],
                        revision,
                        source["id"],
                        source["sha256"],
                        source["text"],
                    ),
                )
        inline = []
        for source, desc in zip(selected, effective["context"]["sources"]):
            inline.append(
                {"id": source["id"], "text": source["text"][: desc["inline_chars"]]}
            )
        # Native projection consumes these structured values. No synthesized semantic prompt.
        projected["_context_projection"] = {
            "revision": revision,
            "manifest": effective,
            "inline": inline,
            "inherited_conversation": any(
                source.get("context_origin") == "inherited_conversation" for source in selected
            ),
            "run_id": run["run_id"],
            "attempt_id": attempt,
        }
        projected["_context_tool_policy"] = {
            "mode": config.tools.mode,
            "selected": config.tools.mcp_server_ids,
            "runtime_ids": sorted(
                set(configured_connections(bundle))
                - set(configured_connections(bootstrap_bundle_for(fresh)))
            ),
        }
        projected["bootstrap_bundle_json"] = _json(bundle)
        return projected

    def native_read(self, token, source_id, offset=0, max_chars=12000):
        principal = self.peers.native_principal(token, purpose="context")
        with self.store._connect() as conn:
            worker = _worker(
                conn,
                principal["worker_id"],
                principal["tenant_id"],
                principal["owner_id"],
            )
            _, config = self._config(conn, worker["worker_id"])
            revision = self._context_revision(conn, worker["worker_id"])
            run = conn.execute("SELECT run_id,continuation_context_json FROM runs WHERE run_id=? AND worker_id=?",
                               (principal["run_id"], worker["worker_id"])).fetchone()
            worker = _run_context_worker(worker, dict(run) if run else {})
            row = conn.execute(
                "SELECT * FROM worker_context_snapshots WHERE run_id=? AND attempt_id=? AND source_id=? AND worker_id=?",
                (
                    principal["run_id"],
                    principal["attempt_id"],
                    source_id,
                    worker["worker_id"],
                ),
            ).fetchone()
        if row is None or row["revision"] != revision:
            raise ConfigurationError("context_snapshot_unavailable", 403)
        selected = self._effective(worker, config)[1]
        # Revoked or changed source bytes are never returned from an old snapshot.
        if not any(
            s["id"] == source_id
            and s["sha256"] == row["sha256"]
            and s["status"] == "available"
            for s in selected
        ):
            raise ConfigurationError("context_authority_changed", 403)
        return self._page({"id": source_id, "text": row["content"]}, offset, max_chars)

    def native_list(self, token):
        principal = self.peers.native_principal(token, purpose="context")
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT source_id FROM worker_context_snapshots WHERE run_id=? AND attempt_id=? AND worker_id=?",
                (principal["run_id"], principal["attempt_id"], principal["worker_id"]),
            ).fetchall()
        result = []
        for row in rows:
            page = self.native_read(token, row["source_id"], 0, 1)
            result.append({k: page[k] for k in ("source_id", "sha256", "chars")})
        return result


def guard_worker_configuration(conn, run_id):
    """Call inside the existing runtime-invocation transaction, after capacity fences."""
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_configurations'"
        ).fetchone()
        is None
    ):
        return
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ConfigurationError("run_unavailable", 404)
    # Foreground conversation is governed by its own responsive turn/control primitives.
    worker = conn.execute(
        "SELECT bootstrap_bundle_json,trusted_run_lane FROM workers WHERE worker_id=?",
        (run["worker_id"],),
    ).fetchone()
    if worker["trusted_run_lane"] == "conversation":
        return
    _, config = WorkerConfiguration._config(conn, run["worker_id"])
    if not config.background.enabled:
        raise ConfigurationError("background_disabled")
    count = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE worker_id=? AND state='running' AND runtime_invoked_at IS NOT NULL AND run_id<>?",
        (run["worker_id"], run_id),
    ).fetchone()[0]
    if count >= config.background.max_parallel_runs:
        raise ConfigurationError("background_capacity")


def contextual_instruction(instruction: str, worker: dict) -> str:
    """A faithful data envelope; policy/developer authority remains in its native channel."""
    projection = worker.get("_context_projection")
    if not projection:
        return instruction
    manifest = dict(projection["manifest"]["context"])
    # The runtime binds this identity before projection. A warm native process has no
    # per-turn environment, so recovery must use this current envelope, not its shell.
    manifest.pop("run_id", None)
    run_id = str(worker.get("_active_run_id") or "").strip()
    if run_id:
        manifest["run_id"] = run_id
    envelope = {
        "instruction": instruction,
        "context": projection["inline"],
        "context_manifest": manifest,
    }
    if projection.get("inherited_conversation"):
        from .worker_context_mcp import context_tool_manifest

        envelope["context_contract"] = context_tool_manifest()["inherited_context_contract"]
    return _json(envelope)


def enforce_context_tools(worker: dict) -> dict:
    policy = worker.get("_context_tool_policy")
    if not policy or policy["mode"] == "inherit":
        return worker
    bundle = json.loads(_json(bootstrap_bundle_for(worker)))
    allowed = set(policy["selected"]) | set(policy["runtime_ids"])
    servers = mcp_servers(bundle)
    removed = set(configured_connections(bundle)) - allowed
    bundle["claude_project_mcp"] = {
        "mcpServers": {k: v for k, v in servers.items() if k in allowed}
    }
    bundle["codex_config_append"] = restrict_codex_connections(
        str(bundle.get("codex_config_append") or ""), removed
    )
    bundle["_configured_removed_mcp_servers"] = sorted(
        set(bundle.get("_configured_removed_mcp_servers") or []) | removed
    )
    return worker | {"bootstrap_bundle_json": _json(bundle)}
