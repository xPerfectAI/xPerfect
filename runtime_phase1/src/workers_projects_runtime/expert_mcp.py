"""An Expert Queen's own tools over the same native run authority as the coordinator's."""
from __future__ import annotations

from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP

from .coordinator import CoordinatorScopeError
from .experts import expert_tool_manifest


def native_expert_server(experts, peers, endpoint: str):
    from .mcp_server import _mcp_transport_security_settings

    target = urlparse(endpoint)
    server = FastMCP("xperfect-expert", stateless_http=True, json_response=True,
                     streamable_http_path="/", transport_security=_mcp_transport_security_settings(
                         target.hostname or "127.0.0.1", target.port or (443 if target.scheme == "https" else 80)))
    descriptions = expert_tool_manifest()["tools"]

    def caller(ctx):
        request = ctx.request_context.request
        authorization = request.headers.get("authorization", "") if request else ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise CoordinatorScopeError("Native authority required")
        principal = peers.native_principal(token)
        tenant, owner, conversation_id = experts.coordinator.native_identity(principal)
        experts.speaker(tenant, owner, conversation_id)  # only a speaking Queen may use these tools
        return tenant, owner, conversation_id, experts._current_turn(principal)

    @server.tool(name="expert_hand_off", description=descriptions["expert_hand_off"])
    def hand_off(goal: str, idempotency_key: str, ctx: Context, context: str = ""):
        tenant, owner, conversation_id, turn_id = caller(ctx)
        return experts.hand_off(tenant, owner, conversation_id, goal=goal, context=context,
                                idempotency_key=idempotency_key, source_turn_id=turn_id)

    @server.tool(name="expert_jobs", description=descriptions["expert_jobs"])
    def jobs(ctx: Context):
        tenant, owner, conversation_id, _ = caller(ctx)
        return {"jobs": experts.jobs(tenant, owner, conversation_id)}

    @server.tool(name="expert_job_control", description=descriptions["expert_job_control"])
    def job_control(job_id: str, action: str, idempotency_key: str, ctx: Context, message: str = ""):
        tenant, owner, conversation_id, _ = caller(ctx)
        return experts.job_control(tenant, owner, conversation_id, job_id, action=action,
                                   message=message, idempotency_key=idempotency_key)

    @server.tool(name="expert_ask_owner", description=descriptions["expert_ask_owner"])
    def ask_owner(reason: str, idempotency_key: str, ctx: Context, reply: str = "",
                  job_goal: str = "", job_context: str = ""):
        tenant, owner, conversation_id, turn_id = caller(ctx)
        return experts.ask_owner(tenant, owner, conversation_id, reason=reason, reply=reply, job_goal=job_goal,
                                 job_context=job_context, idempotency_key=idempotency_key, source_turn_id=turn_id)

    return server
