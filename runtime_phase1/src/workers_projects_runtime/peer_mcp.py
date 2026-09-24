"""Peer MCP tools share the same authorization and durable service as HTTP."""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import quote, urlparse

from mcp.server.fastmcp import Context, FastMCP

from .peer_collaboration import PeerError, PeerMessageRequest


def native_peer_server(peers):
    from .mcp_server import _mcp_transport_security_settings

    endpoint = urlparse(
        os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    )
    transport = _mcp_transport_security_settings(
        endpoint.hostname or "127.0.0.1",
        endpoint.port or (443 if endpoint.scheme == "https" else 80),
    )
    server = FastMCP(
        "xperfect-peers",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=transport,
    )

    def principal(ctx):
        request = ctx.request_context.request
        authorization = request.headers.get("authorization", "") if request else ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise PeerError("peer_native_unauthorized", 401)
        return peers.native_principal(token)

    def identity(ctx):
        actor = principal(ctx)
        return actor["worker_id"], {
            "tenant_id": actor["tenant_id"],
            "owner_id": actor["owner_id"],
        }

    @server.tool(
        name="peers_list",
        description="List only metadata allowed by both workspaces. Discovery grants no read, message, file or control access.",
    )
    def peers_list(ctx: Context):
        worker, owner = identity(ctx)
        return peers.discover(worker, **owner)

    @server.tool(
        name="peer_access_list",
        description="List owner-issued exact peer grants. A listed expired or revoked grant is not authority; every use is revalidated.",
    )
    def peer_access_list(ctx: Context):
        worker, owner = identity(ctx)
        return peers.grants(worker, **owner)

    @server.tool(
        name="peer_message",
        description="Persist untrusted peer data and queue a normal worker turn. Requires a message grant and explicit wake grant for an idle member. Queued is not delivered or read. Use one stable idempotency key per message.",
    )
    def peer_message(
        target_worker_id: str,
        grant_id: str,
        message: str,
        idempotency_key: str,
        ctx: Context,
        target_run_id: str | None = None,
        reply_to: str | None = None,
    ):
        actor = principal(ctx)
        return peers.send(
            actor["worker_id"],
            tenant_id=actor["tenant_id"],
            owner_id=actor["owner_id"],
            source_run_id=actor["run_id"],
            source_attempt_id=actor["attempt_id"],
            request=PeerMessageRequest(
                target_worker_id=target_worker_id,
                grant_id=grant_id,
                message=message,
                idempotency_key=idempotency_key,
                target_run_id=target_run_id,
                reply_to=reply_to,
            ),
        )

    @server.tool(
        name="peer_messages",
        description="Replay only your sent and received peer envelopes after a durable sequence cursor. Revoked or expired content is unavailable.",
    )
    def peer_messages(ctx: Context, after: int = 0, limit: int = 50):
        worker, owner = identity(ctx)
        return peers.messages(worker, **owner, after=after, limit=limit)

    @server.tool(
        name="peer_context_read",
        description="Read only one explicitly granted peer run result. Does not disclose bootstrap, credentials, ambient files, or other conversations.",
    )
    def peer_context_read(
        target_worker_id: str, grant_id: str, run_id: str, ctx: Context
    ):
        worker, owner = identity(ctx)
        return peers.read_context(worker, target_worker_id, grant_id, run_id, **owner)

    @server.tool(
        name="peer_wait",
        description="Wait up to 20 seconds for a newer durable message; timeout is explicit and never implies delivery. Authority is rechecked while waiting.",
    )
    async def peer_wait(ctx: Context, after: int = 0, timeout_seconds: float = 10):
        if not 0 <= timeout_seconds <= 20:
            raise PeerError("peer_wait_invalid", 422)
        end = time.monotonic() + timeout_seconds
        while True:
            worker, owner = identity(ctx)
            result = peers.messages(worker, **owner, after=after)
            if result["items"] or time.monotonic() >= end:
                return result | {"timed_out": not bool(result["items"])}
            await asyncio.sleep(min(0.2, max(0, end - time.monotonic())))

    return server


def register_owner_peer_tools(server, client):
    def path(value):
        return quote(value, safe="")

    @server.tool(
        name="peers_list",
        description="List same-account peers allowed by independent discovery settings; metadata visibility does not grant access.",
    )
    def peers_list(worker_id: str):
        return client._request("GET", f"/v1/workers/{path(worker_id)}/peers")

    @server.tool(
        name="peer_policy_get",
        description="Read the current versioned discovery and access policy for a workspace.",
    )
    def peer_policy_get(workspace_id: str):
        return client._request(
            "GET", f"/v1/workspaces/{path(workspace_id)}/peer-policy"
        )

    @server.tool(
        name="peer_policy_set",
        description="Set separate discovery and access controls using the current revision. Requires an authenticated human confirmation scope; native members cannot expand permissions.",
    )
    def peer_policy_set(
        workspace_id: str,
        expected_revision: int,
        discovery: str = "off",
        access_enabled: bool = False,
        selected_workspaces: list[str] | None = None,
    ):
        return client._request(
            "PUT",
            f"/v1/workspaces/{path(workspace_id)}/peer-policy",
            json_body={
                "expected_revision": expected_revision,
                "discovery": discovery,
                "access_enabled": access_enabled,
                "selected_workspaces": selected_workspaces or [],
            },
        )

    @server.tool(
        name="peer_access_list",
        description="Read owner-issued scoped grants for a member; expired, stale and revoked grants cannot be used.",
    )
    def peer_access_list(worker_id: str):
        return client._request("GET", f"/v1/workers/{path(worker_id)}/peer-grants")

    @server.tool(
        name="peer_access_grant",
        description="Create an exact source-to-target scoped peer grant (explicit null expiry means until revoked) at both current policy revisions. Requires human confirmation. Only message, wake, and exact-resource context_read are implemented; other typed scopes fail unavailable.",
    )
    def peer_access_grant(
        source_worker_id: str,
        target_worker_id: str,
        scopes: list[str],
        source_revision: int,
        target_revision: int,
        expires_at: str | None,
        idempotency_key: str,
        resource_ids: list[str] | None = None,
    ):
        return client._request(
            "POST",
            "/v1/peer-grants",
            json_body={
                "source_worker_id": source_worker_id,
                "target_worker_id": target_worker_id,
                "scopes": scopes,
                "source_revision": source_revision,
                "target_revision": target_revision,
                "expires_at": expires_at,
                "idempotency_key": idempotency_key,
                "resource_ids": resource_ids or [],
            },
        )

    @server.tool(
        name="peer_access_options",
        description="Owner-only current workspace/member snapshots for an explicit permission action. Does not enable discovery or access.",
    )
    def peer_access_options(worker_id: str):
        return client._request(
            "GET", f"/v1/workers/{path(worker_id)}/peer-access-options"
        )

    @server.tool(
        name="peer_access_grant_batch",
        description="Owner-confirmed permission for exact current members, with explicit send/receive/both direction and optional wake. Does not grant future members or context/file/control access.",
    )
    def peer_access_grant_batch(
        source_worker_id: str,
        workspaces: list[dict],
        target_worker_ids: list[str],
        direction: str,
        enable_access: bool,
        expires_at: str | None,
        idempotency_key: str,
        wake: bool = False,
    ):
        return client._request(
            "POST",
            "/v1/peer-grants/batch",
            json_body={
                "source_worker_id": source_worker_id,
                "workspaces": workspaces,
                "target_worker_ids": target_worker_ids,
                "direction": direction,
                "enable_access": enable_access,
                "expires_at": expires_at,
                "idempotency_key": idempotency_key,
                "wake": wake,
            },
        )

    @server.tool(
        name="peer_access_revoke",
        description="Revoke a peer grant at the next enforced boundary. Already delivered model context cannot be erased. Requires human confirmation.",
    )
    def peer_access_revoke(grant_id: str):
        return client._request("DELETE", f"/v1/peer-grants/{path(grant_id)}")

    @server.tool(
        name="peer_message",
        description="Queue one durable peer message using an owner-issued grant and idempotency key. Recipient turns use normal admission; queued is not read.",
    )
    def peer_message(
        worker_id: str,
        target_worker_id: str,
        grant_id: str,
        message: str,
        idempotency_key: str,
        target_run_id: str | None = None,
        reply_to: str | None = None,
    ):
        return client._request(
            "POST",
            f"/v1/workers/{path(worker_id)}/peer-messages",
            json_body={
                "target_worker_id": target_worker_id,
                "grant_id": grant_id,
                "message": message,
                "idempotency_key": idempotency_key,
                "target_run_id": target_run_id,
                "reply_to": reply_to,
            },
        )

    @server.tool(
        name="peer_messages",
        description="Read ordered durable sent/received messages from a sequence cursor. Revoked content stays hidden.",
    )
    def peer_messages(worker_id: str, after: int = 0, limit: int = 50):
        return client._request(
            "GET",
            f"/v1/workers/{path(worker_id)}/peer-messages?after={after}&limit={limit}",
        )

    @server.tool(
        name="peer_context_read",
        description="Read one explicitly authorized result, without other worker context or credentials.",
    )
    def peer_context_read(
        worker_id: str, target_worker_id: str, grant_id: str, run_id: str
    ):
        return client._request(
            "POST",
            f"/v1/workers/{path(worker_id)}/peer-context",
            json_body={
                "target_worker_id": target_worker_id,
                "grant_id": grant_id,
                "run_id": run_id,
            },
        )

    @server.tool(
        name="peer_wait",
        description="Wait up to 20 seconds for a newer durable peer message, with an explicit timeout result.",
    )
    def peer_wait(worker_id: str, after: int = 0, timeout_seconds: float = 10):
        return client._request(
            "GET",
            f"/v1/workers/{path(worker_id)}/peer-wait?after={after}&timeout_seconds={timeout_seconds}",
        )
