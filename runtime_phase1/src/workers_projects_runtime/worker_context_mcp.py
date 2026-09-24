"""Context pagination shares the existing exact-attempt peer authority binder."""

import json
import os
from urllib.parse import urlparse
from pathlib import Path
import hashlib
from mcp.server.fastmcp import Context, FastMCP
from .worker_configuration import ConfigurationError, _json, mcp_servers, restrict_codex_connections


def context_tool_manifest():
    path = Path(__file__).with_name("prompts") / "worker-context-tools.json"
    raw = path.read_bytes()
    return json.loads(raw) | {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": "workers_projects_runtime/prompts/worker-context-tools.json",
    }


def native_context_server(configuration):
    descriptions = context_tool_manifest()["tools"]
    from .mcp_server import _mcp_transport_security_settings

    endpoint = urlparse(
        os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    )
    server = FastMCP(
        "xperfect-context",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=_mcp_transport_security_settings(
            endpoint.hostname or "127.0.0.1",
            endpoint.port or (443 if endpoint.scheme == "https" else 80),
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
    hosted = os.environ.get("XPERFECT_EXECUTION_PROFILE") == "hosted-xfs"
    # The supported hosted package has no trusted native context bridge yet.
    # Fail before minting the scoped bearer instead of advertising a dead URL
    # or sending the bearer across a shared plaintext worker network.
    if hosted:
        raise ConfigurationError("context_endpoint_unavailable")
    base = os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "").rstrip("/")
    endpoint = urlparse(base)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.netloc
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ConfigurationError("context_endpoint_unavailable")
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
    bundle.setdefault("env", {})["GLASSHIVE_CONTEXT_TOKEN"] = token
    servers = dict(mcp_servers(bundle))
    bundle["claude_project_mcp"] = {"mcpServers": servers}
    servers["xperfect-context"] = {
        "type": "http",
        "url": base + "/v1/native/context/",
        "headers": {"Authorization": "Bearer ${GLASSHIVE_CONTEXT_TOKEN}"},
    }
    bundle["codex_config_append"] = (
        restrict_codex_connections(
            str(bundle.get("codex_config_append") or ""), {"xperfect-context"}
        )
        + "\n[mcp_servers.xperfect-context]\nurl = "
        + json.dumps(base + "/v1/native/context/")
        + '\nbearer_token_env_var = "GLASSHIVE_CONTEXT_TOKEN"\n'
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
