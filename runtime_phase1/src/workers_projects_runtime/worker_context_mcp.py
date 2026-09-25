"""Context pagination shares the existing exact-attempt peer authority binder."""

import json
from pathlib import Path
import hashlib
from mcp.server.fastmcp import Context, FastMCP
from .worker_configuration import (
    ConfigurationError,
    _json,
    context_route_unavailable,
    mcp_servers,
    restrict_codex_connections,
)


def context_tool_manifest():
    path = Path(__file__).with_name("prompts") / "worker-context-tools.json"
    raw = path.read_bytes()
    return json.loads(raw) | {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": "workers_projects_runtime/prompts/worker-context-tools.json",
    }


def native_context_server(configuration):
    descriptions = context_tool_manifest()["tools"]
    from . import native_transport
    from .mcp_server import _mcp_transport_security_settings

    server = FastMCP(
        "xperfect-context",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=_mcp_transport_security_settings(
            *native_transport.mcp_listen_origin()
        ),
    )

    def token(ctx):
        request = ctx.request_context.request
        scheme, _, value = (
            request.headers.get("authorization", "") if request else ""
        ).partition(" ")
        if scheme.lower() != "bearer":
            raise ConfigurationError("context_unauthorized", 401)
        return value

    @server.tool(name="list_context", description=descriptions["list_context"])
    def list_context(ctx: Context):
        return configuration.native_list(token(ctx))

    @server.tool(name="read_context", description=descriptions["read_context"])
    def read_context(
        source_id: str, ctx: Context, offset: int = 0, max_chars: int = 12000
    ):
        return configuration.native_read(token(ctx), source_id, offset, max_chars)

    return server


def bind_context_projection(configuration, worker, run):
    projected = configuration.prepare_run(worker, run)
    if not projected["_context_projection"]["manifest"]["context"]["retrievable_chars"]:
        return projected
    from . import native_transport

    # A packaged box reaches the runtime only through its own socket, and a
    # hosted network contains several owners. Without a route, fail before
    # minting the scoped bearer.
    base = native_transport.worker_route()
    if not base:
        raise context_route_unavailable()
    token = configuration.peers.mint_native_session(
        worker["worker_id"], run["run_id"], purpose="context"
    )
    projected["_context_tool_policy"]["runtime_ids"].append("xperfect-context")
    bundle = json.loads(str(projected["bootstrap_bundle_json"] or "{}"))
    bundle["_configured_removed_mcp_servers"] = [
        name for name in (bundle.get("_configured_removed_mcp_servers") or [])
        if name != "xperfect-context"
    ]
    if bundle.get("grok_mcp_servers") is not None:
        bundle["grok_mcp_servers"] = [
            server for server in bundle["grok_mcp_servers"]
            if server.get("name") != "xperfect-context"
        ]
    environment = bundle.setdefault("env", {})
    environment["GLASSHIVE_CONTEXT_TOKEN"] = token
    servers = dict(mcp_servers(bundle))
    bundle["claude_project_mcp"] = {"mcpServers": servers}
    url = base + "/v1/native/context/"
    if native_transport.socket_route(base):
        interpreter = native_transport.bridge_interpreter(base)
        servers["xperfect-context"] = native_transport.stdio_server(
            url, "GLASSHIVE_CONTEXT_TOKEN", interpreter
        )
        codex_block = native_transport.codex_stdio_block(
            "xperfect-context", url, "GLASSHIVE_CONTEXT_TOKEN", interpreter
        )
    else:
        servers["xperfect-context"] = {
            "type": "http",
            "url": url,
            "headers": {"Authorization": "Bearer ${GLASSHIVE_CONTEXT_TOKEN}"},
        }
        codex_block = (
            "[mcp_servers.xperfect-context]\nurl = "
            + json.dumps(url)
            + '\nbearer_token_env_var = "GLASSHIVE_CONTEXT_TOKEN"\n'
        )
    bundle["codex_config_append"] = (
        restrict_codex_connections(
            str(bundle.get("codex_config_append") or ""), {"xperfect-context"}
        )
        + "\n"
        + codex_block
    )
    projected["bootstrap_bundle_json"] = _json(bundle)
    return projected


def register_owner_configuration_tools(server, client):
    from urllib.parse import quote
    from .worker_configuration import ConfigurationUpdate

    descriptions = context_tool_manifest()["tools"]

    @server.tool(
        name="worker_configuration_get",
        description=descriptions["worker_configuration_get"],
    )
    def worker_configuration_get(worker_id: str):
        return client._request(
            "GET", f"/v1/workers/{quote(worker_id, safe='')}/configuration"
        )

    @server.tool(
        name="worker_configuration_update",
        description=descriptions["worker_configuration_update"],
    )
    def worker_configuration_update(worker_id: str, configuration: ConfigurationUpdate):
        return client._request(
            "PUT",
            f"/v1/workers/{quote(worker_id, safe='')}/configuration",
            json_body=configuration.model_dump(),
            require_write_scope=True,
        )
