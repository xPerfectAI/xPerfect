"""Coordinator-only tools over the shared O06 native run/attempt authority binder."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP

from .coordinator import Control, CoordinatorScopeError, Dispatch, Goal


def coordinator_tool_manifest():
    return json.loads((Path(__file__).with_name("prompts") / "coordinator-tools.json").read_text())


def native_coordinator_server(coordinator, peers, endpoint: str):
    from .mcp_server import _mcp_transport_security_settings

    target = urlparse(endpoint)
    server = FastMCP("xperfect-coordinator", stateless_http=True, json_response=True,
                     streamable_http_path="/", transport_security=_mcp_transport_security_settings(
                         target.hostname or "127.0.0.1", target.port or (443 if target.scheme == "https" else 80)))
    descriptions = coordinator_tool_manifest()["tools"]

    def identity(ctx):
        request = ctx.request_context.request
        authorization = request.headers.get("authorization", "") if request else ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise CoordinatorScopeError("Native authority required")
        return coordinator.native_identity(peers.native_principal(token))

    @server.tool(name="coordinator_accept_goals", description=descriptions["coordinator_accept_goals"])
    def accept(turn_id: str, goals: list[Goal], ctx: Context):
        return coordinator.accept_goals(*identity(ctx), turn_id, goals)

    @server.tool(name="coordinator_dispatch", description=descriptions["coordinator_dispatch"])
    def dispatch(goal_id: str, route_id: str, instruction: str, ctx: Context):
        return coordinator.dispatch(*identity(ctx), Dispatch(goal_id=goal_id, route_id=route_id, instruction=instruction))

    @server.tool(name="coordinator_status", description=descriptions["coordinator_status"])
    def status(ctx: Context):
        return coordinator.snapshot(*identity(ctx))

    @server.tool(name="coordinator_result", description=descriptions["coordinator_result"])
    def result(goal_id: str, ctx: Context, offset: int = 0, max_chars: int = 12000):
        return coordinator.read_result(*identity(ctx), goal_id, offset, max_chars)

    @server.tool(name="coordinator_handle", description=descriptions["coordinator_handle"])
    def handle(goal_ids: list[str], ctx: Context):
        request = ctx.request_context.request
        authorization = request.headers.get("authorization", "") if request else ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise CoordinatorScopeError("Native authority required")
        principal = peers.native_principal(token)
        scope = coordinator.native_identity(principal)
        return coordinator.handle_goals(*scope, goal_ids, principal["worker_id"], principal["run_id"])

    @server.tool(name="coordinator_control", description=descriptions["coordinator_control"])
    def control(goal_id: str, control: Control, ctx: Context):
        return coordinator.control(*identity(ctx), goal_id, control)

    return server


def project_coordinator_bootstrap(worker: dict, bundle: dict) -> dict:
    """Only a trusted run boundary may attach this projection; no stored token."""
    projection = worker.get("_coordinator_native_projection")
    if not isinstance(projection, dict):
        return bundle
    if projection.get("worker_id") != worker.get("worker_id") or projection.get("run_id") != worker.get("_active_run_id"):
        raise CoordinatorScopeError("Coordinator native projection mismatch")
    # Reuse the exact O06 token, which the service already minted for this run/attempt.
    result = json.loads(json.dumps(bundle))
    result.setdefault("env", {})["GLASSHIVE_PEER_TOKEN"] = projection["token"]
    mcp = result.get("claude_project_mcp") or {}
    servers = mcp.get("mcpServers", mcp)
    servers["xperfect-coordinator"] = {"type": "http", "url": projection["url"],
                                      "headers": {"Authorization": "Bearer ${GLASSHIVE_PEER_TOKEN}"}}
    result["claude_project_mcp"] = {"mcpServers": servers}
    from .bootstrap import _strip_codex_mcp_server_blocks
    append = _strip_codex_mcp_server_blocks(result.get("codex_config_append", ""), {"xperfect-coordinator"}).rstrip()
    result["codex_config_append"] = append + '\n\n[mcp_servers.xperfect-coordinator]\nurl = ' + json.dumps(projection["url"]) + '\nbearer_token_env_var = "GLASSHIVE_PEER_TOKEN"\n'
    return result
