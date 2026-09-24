"""Typed inventory of GlassHive MCP tools whose user-visible result arrives later by callback."""

from __future__ import annotations

# Tools that queue, continue, or steer a Worker run. Their tool result is only an acknowledgement:
# the delivered result reaches the host later as a GlassHive Worker callback, so a host client may
# keep listening for that callback after the harness turn completes. Every other connected tool is
# request/response and must not arm long follow-up polling. `worker_takeover` is deliberately absent:
# it returns operator URLs synchronously and delivers nothing later.
DEFERRED_CALLBACK_TOOLS: frozenset[str] = frozenset(
    {
        "worker_delegate_once",
        "workspace_launch",
        "workspace_continue",
        "worker_run",
        "worker_message",
        "worker_resume",
        "worker_schedule",
        "worker_recurring_schedule_run_now",
    }
)

# The anchor is a GlassHive capability claim, so it requires GlassHive provenance. These are the
# MCP server names Viventium compiles for its own planes; a foreign server that happens to expose a
# tool called `worker_run` must never arm a host client's deferred-callback listening.
GLASSHIVE_MCP_SERVERS: frozenset[str] = frozenset(
    {
        "glasshive",
        "glasshive-workers-projects",
        "glasshive-user-capabilities",
    }
)


def _split_origin_suffix(value: str) -> tuple[str, str]:
    """Split LibreChat's `<qualified tool>_mcp_<origin server>` naming convention.

    The host advertises a brokered tool as `mcp__<client server>__<tool>_mcp_<origin server>`, so
    the origin server is a suffix, not a prefix. Returns (qualified tool, origin server).
    """

    if "_mcp_" in value:
        qualified, _, origin = value.rpartition("_mcp_")
        return qualified, origin
    return value, ""


def _segments(value: str) -> list[str]:
    candidate = value
    for delimiter in ("/", ":"):
        candidate = candidate.replace(delimiter, "__")
    if candidate.startswith("mcp__"):
        candidate = candidate[len("mcp__") :]
    return [segment for segment in candidate.split("__") if segment]


def _normalized(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def bare_connected_tool_name(value: object) -> str:
    """Return the final tool-name segment of a harness tool id."""

    qualified, _origin = _split_origin_suffix(_normalized(value))
    segments = _segments(qualified)
    return segments[-1] if segments else ""


def connected_tool_servers(value: object) -> set[str]:
    """Return every MCP server name a harness tool id carries (client server and origin server)."""

    qualified, origin = _split_origin_suffix(_normalized(value))
    segments = _segments(qualified)
    servers = {origin} if origin else set()
    if len(segments) > 1:
        servers.add(segments[0])
    return {server for server in servers if server}


def connected_tool_expects_deferred_callback(value: object, *, server: object = None) -> bool:
    """True only for a GlassHive run-dispatching tool proven to come from a GlassHive server.

    Provenance may arrive as an explicit ``server`` (the codex invocation field), as the client
    server segment of a namespaced tool id, or as the `_mcp_<origin server>` suffix the host
    appends to a brokered tool. A tool id carrying no server at all fails closed, so neither a
    foreign nor an unqualified tool can arm host-side listening.
    """

    servers = connected_tool_servers(value)
    explicit = _normalized(server)
    if explicit:
        servers.add(explicit)
    if not servers & GLASSHIVE_MCP_SERVERS:
        return False
    return bare_connected_tool_name(value) in DEFERRED_CALLBACK_TOOLS
