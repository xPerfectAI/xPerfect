"""Scoped native scheduling over the existing exact-attempt authority binder."""
from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import os
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .application_boundary import canonical_schedule_access
from .models import CreateTargetScheduleRequest, UpdateRecurringScheduleRequest
from .peer_collaboration import PeerError


def schedule_access_grant(worker: dict) -> dict | None:
    raw = worker.get("bootstrap_bundle_json") or "{}"
    try:
        bundle = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        raise PeerError("schedule_grant_invalid", 403) from None
    if not isinstance(bundle, dict):
        raise PeerError("schedule_grant_invalid", 403)
    grant = bundle.get("schedule_access")
    if "schedule_access" not in bundle:
        if os.environ.get("WPR_NATIVE_SELF_SCHEDULE_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        from .profile_registry import require_worker_profile
        if require_worker_profile(str(worker.get("profile") or "")).native_transport == "api":
            return None
        return canonical_schedule_access({"allow_worker_target": True})
    try:
        return canonical_schedule_access(grant)
    except ValueError:
        raise PeerError("schedule_grant_invalid", 403) from None


def bind_schedule_projection(service, worker: dict, run: dict) -> dict:
    """Only the runtime binds a token; persisted grants contain no credential."""
    raw = worker.get("bootstrap_bundle_json") or "{}"
    bundle = json.loads(raw) if isinstance(raw, str) else raw
    if bundle.get("run_mode") == "conversation" and bundle.get("conversation_tool_mode") == "coordination":
        return worker
    grant = schedule_access_grant(worker)
    if not grant or not (grant.get("destinations") or grant.get("allow_worker_target") or grant.get("manage_destination_ids")):
        return worker
    from . import native_transport
    if not native_transport.worker_route():
        # Optional default capability cannot break otherwise valid native work.
        # Explicit grants require an actual native route and fail visibly.
        if "schedule_access" not in bundle:
            return worker
        raise PeerError("schedule_endpoint_unavailable", 503)
    from .worker_context_mcp import bind_native_tool_projection
    projected = bind_native_tool_projection(
        service.peers, worker, run, name="xperfect-schedules", purpose="schedule",
        token_env="GLASSHIVE_SCHEDULE_TOKEN", endpoint_path="/v1/native/schedules/",
    )
    bundle = json.loads(projected["bootstrap_bundle_json"])
    projected["_active_run_id"] = run["run_id"]
    projected["_schedule_native_projection"] = {
        "worker_id": worker["worker_id"], "run_id": run["run_id"],
        "token": bundle["env"]["GLASSHIVE_SCHEDULE_TOKEN"],
        "server": bundle["claude_project_mcp"]["mcpServers"]["xperfect-schedules"],
    }
    return projected


class NativeScheduleAccess:
    def __init__(self, service):
        self.service = service

    @contextmanager
    def _scope(self, token):
        principal = self.service.peers.native_principal(token, purpose="schedule")
        # Existing lifecycle fencing lets Stop/Close/retry revoke the attempt
        # before a schedule mutation, without a transaction held over network IO.
        with self.service._worker_compute_release_lock(principal["worker_id"]):
            principal = self.service.peers.native_principal(token, purpose="schedule")
            worker = self.service.require_worker(principal["worker_id"])
            if (str(worker.get("owner_id")) != principal["owner_id"]
                    or str(worker.get("tenant_id") or "local") != principal["tenant_id"]):
                raise PeerError("schedule_scope_unavailable", 403)
            grant = schedule_access_grant(worker)
            if not grant:
                raise PeerError("schedule_scope_unavailable", 403)
            yield principal, grant

    @staticmethod
    def _allowed(target, principal, grant):
        if not isinstance(target, dict):
            return False
        if target.get("kind") == "worker":
            return (set(target) == {"kind", "worker_id"} and grant.get("allow_worker_target") is True
                    and target["worker_id"] == principal["worker_id"])
        if target.get("kind") == "webhook":
            return (set(target) == {"kind", "destination_id", "request_data"}
                    and any(target["destination_id"] == item["destination_id"]
                            and target["request_data"] == item["request_data"]
                            for item in grant.get("destinations", [])))
        return False

    @classmethod
    def _can_manage(cls, target, principal, grant):
        return cls._allowed(target, principal, grant) or bool(
            isinstance(target, dict) and target.get("kind") == "webhook"
            and target.get("destination_id") in grant.get("manage_destination_ids", [])
        )

    def _definition(self, definition_id, principal, grant):
        definition = self.service.get_recurring_schedule(
            definition_id, tenant_id=principal["tenant_id"], owner_id=principal["owner_id"],
        )
        target = (definition or {}).get("target")
        if target is None and (definition or {}).get("worker_id"):
            target = {"kind": "worker", "worker_id": definition["worker_id"]}
        if not definition or not self._can_manage(target, principal, grant):
            raise PeerError("schedule_scope_unavailable", 403)
        return definition

    def create(self, token: str, payload: CreateTargetScheduleRequest):
        values = payload.model_dump(mode="json")
        if values.pop("bootstrap_bundle", None) is not None:
            raise PeerError("schedule_bootstrap_override_forbidden", 403)
        target = values.pop("target")
        with self._scope(token) as (principal, grant):
            if not self._allowed(target, principal, grant):
                raise PeerError("schedule_scope_unavailable", 403)
            for key, value in grant.get("defaults", {}).get(payload.recurrence_type, {}).items():
                if key not in payload.model_fields_set:
                    values[key] = value
            return self.service.create_target_schedule(
                tenant_id=principal["tenant_id"], owner_id=principal["owner_id"], target=target, **values,
            )

    def list(self, token: str, include_inactive: bool = False, limit: int = 100):
        if not 1 <= limit <= 100:
            raise PeerError("schedule_limit_invalid", 422)
        with self._scope(token) as (principal, grant):
            definitions = self.service.list_recurring_schedules(
                tenant_id=principal["tenant_id"], owner_id=principal["owner_id"],
                include_inactive=include_inactive, limit=1000,
            )
            items = []
            for definition in definitions:
                target = definition.get("target") or {"kind": "worker", "worker_id": definition.get("worker_id")}
                if self._can_manage(target, principal, grant):
                    items.append(definition)
            targets = [{"target": {"kind": "webhook", "destination_id": item["destination_id"],
                                   "request_data": item["request_data"]}, "label": item.get("label", "")}
                       for item in grant.get("destinations", [])]
            if grant.get("allow_worker_target") is True:
                targets.append({"target": {"kind": "worker", "worker_id": principal["worker_id"]}, "label": "This worker"})
            return {"targets": targets, "manage_destination_ids": grant.get("manage_destination_ids", []), "defaults": grant.get("defaults", {}), "items": items[:limit]}

    def get(self, token: str, definition_id: str):
        with self._scope(token) as (principal, grant):
            return self._definition(definition_id, principal, grant)

    def update(self, token: str, definition_id: str, updates: UpdateRecurringScheduleRequest):
        values = updates.model_dump(mode="json", exclude_unset=True)
        with self._scope(token) as (principal, grant):
            current = self._definition(definition_id, principal, grant)
            if ("target" in values and values["target"] != current.get("target")
                    and not self._allowed(values["target"], principal, grant)):
                raise PeerError("schedule_scope_unavailable", 403)
            return self.service.update_recurring_schedule(
                definition_id, tenant_id=principal["tenant_id"], owner_id=principal["owner_id"], updates=values,
            )

    def retire(self, token: str, definition_id: str):
        with self._scope(token) as (principal, grant):
            self._definition(definition_id, principal, grant)
            return self.service.retire_recurring_schedule(
                definition_id, tenant_id=principal["tenant_id"], owner_id=principal["owner_id"],
            )

    def run_now(self, token: str, definition_id: str, idempotency_token: str):
        if not isinstance(idempotency_token, str) or not 8 <= len(idempotency_token) <= 128:
            raise PeerError("schedule_idempotency_key_invalid", 422)
        with self._scope(token) as (principal, grant):
            self._definition(definition_id, principal, grant)
            result = self.service.run_recurring_schedule_now(
                definition_id, tenant_id=principal["tenant_id"], owner_id=principal["owner_id"],
                idempotency_token=idempotency_token,
            )
            if result is None:
                raise PeerError("schedule_not_found", 404)
            # A control receipt confirms admission; the saved executor retains its full payload.
            receipt = {"definition_id": definition_id}
            for key in ("occurrence_id", "status", "state"):
                if key in result:
                    receipt[key] = result[key]
            scheduled_for = result.get("scheduled_for", result.get("run_at"))
            if scheduled_for is not None:
                receipt["scheduled_for"] = scheduled_for
            if type(result.get("occurrence_created")) is bool:
                receipt["occurrence_created"] = result["occurrence_created"]
            return receipt

    def history(self, token: str, definition_id: str, limit: int = 50):
        if not 1 <= limit <= 100:
            raise PeerError("schedule_limit_invalid", 422)
        with self._scope(token) as (principal, grant):
            self._definition(definition_id, principal, grant)
            return self.service.list_recurring_schedule_occurrences(
                definition_id, tenant_id=principal["tenant_id"], owner_id=principal["owner_id"], limit=limit,
            )


def schedule_tool_manifest():
    path = Path(__file__).with_name("prompts") / "schedule-tools.json"
    raw = path.read_bytes()
    return json.loads(raw) | {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": "workers_projects_runtime/prompts/schedule-tools.json",
    }


def native_schedule_server(service):
    from . import native_transport
    from .mcp_server import _mcp_transport_security_settings
    manifest = schedule_tool_manifest()
    server = FastMCP("xperfect-schedules", instructions=manifest["instructions"], stateless_http=True, json_response=True,
        streamable_http_path="/", transport_security=_mcp_transport_security_settings(*native_transport.mcp_listen_origin()))
    access = NativeScheduleAccess(service)

    def token(ctx):
        request = ctx.request_context.request
        scheme, _, value = (request.headers.get("authorization", "") if request else "").partition(" ")
        if scheme.lower() != "bearer":
            raise PeerError("schedule_native_unauthorized", 401)
        return value

    @server.tool(name="schedule_create", description=manifest["tools"]["schedule_create"])
    def create(payload: CreateTargetScheduleRequest, ctx: Context):
        return access.create(token(ctx), payload)

    @server.tool(name="schedule_list", description=manifest["tools"]["schedule_list"])
    def list_schedules(ctx: Context, include_inactive: bool = False, limit: int = 100):
        return access.list(token(ctx), include_inactive, limit)

    @server.tool(name="schedule_get", description=manifest["tools"]["schedule_get"])
    def get(definition_id: str, ctx: Context):
        return access.get(token(ctx), definition_id)

    @server.tool(name="schedule_update", description=manifest["tools"]["schedule_update"])
    def update(definition_id: str, updates: UpdateRecurringScheduleRequest, ctx: Context):
        return access.update(token(ctx), definition_id, updates)

    @server.tool(name="schedule_retire", description=manifest["tools"]["schedule_retire"])
    def retire(definition_id: str, ctx: Context):
        return access.retire(token(ctx), definition_id)

    @server.tool(name="schedule_run_now", description=manifest["tools"]["schedule_run_now"])
    def run_now(definition_id: str, idempotency_token: str, ctx: Context):
        return access.run_now(token(ctx), definition_id, idempotency_token)

    @server.tool(name="schedule_history", description=manifest["tools"]["schedule_history"])
    def history(definition_id: str, ctx: Context, limit: int = 50):
        return access.history(token(ctx), definition_id, limit)

    return server


def claude_schedule_tool_rules(worker, bundle, servers):
    """Grant only the exact runtime-projected server, not an owner-named lookalike."""
    projection = worker.get("_schedule_native_projection")
    if (not isinstance(projection, dict) or not projection.get("token")
            or projection.get("worker_id") != worker.get("worker_id")
            or projection.get("run_id") != worker.get("_active_run_id")
            or (bundle.get("env") or {}).get("GLASSHIVE_SCHEDULE_TOKEN") != projection["token"]
            or servers.get("xperfect-schedules") != projection.get("server")):
        return []
    return ["mcp__xperfect-schedules__" + name for name in (
        "schedule_create", "schedule_list", "schedule_get", "schedule_update",
        "schedule_retire", "schedule_run_now", "schedule_history",
    )]
