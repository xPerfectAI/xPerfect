"""The pinned official MCP dispatcher must not serialize blocking HTTP tools."""
import asyncio
from contextvars import ContextVar
import threading
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import Context
from workers_projects_runtime import mcp_server


class AtomicApi:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.both_entered = threading.Event()
        self.token_subjects = {}
    def get_preferences(self):
        return {}
    def create_delegation(self, **kwargs):
        # A fake blocking HTTP owner. No model, runtime or external side effect.
        with self.lock:
            self.calls.append(kwargs)
            token = mcp_server.get_access_token()
            self.token_subjects[kwargs['owner_id']] = token.subject if token else None
            if len(self.calls) == 2:
                self.both_entered.set()
        if not self.both_entered.wait(.5):
            raise RuntimeError('Independent HTTP calls did not overlap')
        return {'workRef': 'work_' + kwargs['owner_id'], 'state': 'queued',
                'idempotentReplay': False, 'resourceClass': 'light'}


@pytest.mark.parametrize('oauth', [False, True])
def test_actual_registered_delegations_overlap_and_keep_each_owner_context(monkeypatch, oauth):
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken
    headers = ContextVar('synthetic_mcp_headers', default={})
    monkeypatch.setattr(mcp_server, 'get_http_headers', lambda: headers.get())
    api = AtomicApi()
    server = mcp_server.create_mcp_server(api_client=api)
    async def launch(owner):
        token = headers.set({'X-Viventium-Tenant-Id': 'tenant', 'X-Viventium-User-Id': owner,
                             'X-Viventium-Message-Id': 'message_' + owner})
        auth = auth_context_var.set(AuthenticatedUser(AccessToken(
            token='synthetic_' + owner, client_id='synthetic_client', scopes=[],
            subject=owner, claims={'tenant_id': 'tenant'})) if oauth else None)
        try:
            return await server.call_tool('worker_delegate_once', {
                'title': 'Synthetic ' + owner, 'instruction': 'Return a synthetic reference',
                'profile': 'codex-cli', 'execution_mode': 'docker', 'resource_class': 'light'})
        finally:
            auth_context_var.reset(auth)
            headers.reset(token)
    async def scenario():
        return await asyncio.gather(launch('owner_a'), launch('owner_b'))
    results = asyncio.run(scenario())
    assert len(results) == 2 and api.both_entered.is_set()
    assert {item['owner_id'] for item in api.calls} == {'owner_a', 'owner_b'}
    assert all(item['tenant_id'] == 'tenant' for item in api.calls)
    assert len({item['idempotency_key'] for item in api.calls}) == 2
    assert all(item['payload']['workerName'] == 'Synthetic ' + item['owner_id'] for item in api.calls)
    assert api.token_subjects == {owner: owner if oauth else None for owner in ('owner_a', 'owner_b')}


def test_registration_preserves_original_sync_calls_schema_context_and_async_functions():
    import inspect
    from mcp.server.fastmcp.tools.base import Tool
    server = mcp_server.GlassHiveFastMCP('synthetic')
    @server.tool()
    def typed_sync(count: int, ctx: Context) -> dict[str, int]:
        assert isinstance(ctx, Context)
        return {'count': count}
    baseline = Tool.from_function(typed_sync)
    registered = server._tool_manager.get_tool('typed_sync')
    assert not inspect.iscoroutinefunction(typed_sync)
    assert typed_sync(3, Context(fastmcp=server)) == {'count': 3}
    assert registered.parameters == baseline.parameters
    assert registered.output_schema == baseline.output_schema
    assert registered.context_kwarg == baseline.context_kwarg
    assert asyncio.run(registered.run({'count': '4'}, context=Context(fastmcp=server))) == {'count': 4}
    @server.tool()
    async def native_async(count: int) -> int:
        return count
    assert server._tool_manager.get_tool('native_async').fn is native_async
    assert asyncio.run(server._tool_manager.get_tool('native_async').run({'count': 5})) == 5


def test_sync_failure_keeps_official_error_contract():
    from mcp.server.fastmcp.exceptions import ToolError
    server = mcp_server.GlassHiveFastMCP('synthetic')
    @server.tool()
    def rejected() -> str:
        raise PermissionError('synthetic owner denied')
    with pytest.raises(ToolError, match='synthetic owner denied'):
        asyncio.run(server.call_tool('rejected', {}))


def test_cancellation_retains_started_http_owner_result_without_replay():
    import anyio
    owner = ContextVar('synthetic_http_owner', default='unbound')
    entered, release = threading.Event(), threading.Event()
    observed = []
    server = mcp_server.GlassHiveFastMCP('synthetic')
    @server.tool()
    def accepted_http() -> str:
        observed.append(('sent', owner.get()))
        entered.set()
        assert release.wait(1)
        observed.append(('receipt', owner.get()))
        return 'same-owned-receipt'
    async def scenario():
        token = owner.set('owner_a')
        scope = anyio.CancelScope()
        try:
            async with anyio.create_task_group() as tasks:
                async def invoke():
                    with scope:
                        await server.call_tool('accepted_http', {})
                tasks.start_soon(invoke)
                assert await anyio.to_thread.run_sync(entered.wait, 1)
                scope.cancel()
                release.set()
        finally:
            owner.reset(token)
    asyncio.run(scenario())
    assert observed == [('sent', 'owner_a'), ('receipt', 'owner_a')]
    # A cancelled request is not permission to replay an accepted native action.
    assert observed.count(('sent', 'owner_a')) == 1


def test_cancelled_scope_does_not_start_unadmitted_http_tool():
    import anyio
    calls = []
    server = mcp_server.GlassHiveFastMCP('synthetic')
    @server.tool()
    def not_sent() -> str:
        calls.append('sent')
        return 'unexpected'
    async def scenario():
        with anyio.CancelScope() as scope:
            scope.cancel()
            await server.call_tool('not_sent', {})
    asyncio.run(scenario())
    assert calls == []


def test_cancelled_delegation_retry_uses_same_key_and_one_durable_native_effect(monkeypatch, tmp_path):
    import anyio
    import hashlib
    import json
    from workers_projects_runtime.store import Store

    store = Store(str(tmp_path / 'synthetic.sqlite3'))
    headers = ContextVar('synthetic_retry_headers', default={})
    monkeypatch.setattr(mcp_server, 'get_http_headers', lambda: headers.get())
    committed, release = threading.Event(), threading.Event()
    calls = []

    class NativeLedgerApi:
        def get_preferences(self):
            return {}

        def create_delegation(self, *, tenant_id, owner_id, idempotency_key, payload):
            calls.append(idempotency_key)
            # Existing native Store transaction; no runtime, provider, or network.
            record = store.reserve_delegation(
                tenant_id=tenant_id, owner_id=owner_id, idempotency_key=idempotency_key,
                request_digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                origin_ref='web:synthetic_message', title=payload['title'], goal=payload['goal'],
                instruction=payload['instruction'], origin_surface=payload['originSurface'],
                worker_name=payload['workerName'], worker_role=payload['workerRole'],
                profile=payload['profile'], backend='codex', runtime='codex-cli', model='synthetic',
                execution_mode=payload['executionMode'])
            committed.set()
            assert release.wait(2)
            return {'workRef': record['work_ref'], 'state': 'queued',
                    'idempotentReplay': record['idempotent_replay'], 'resourceClass': 'light'}

    server = mcp_server.create_mcp_server(api_client=NativeLedgerApi())
    arguments = {'title': 'Synthetic retry', 'instruction': 'Return a synthetic reference',
                 'profile': 'codex-cli', 'execution_mode': 'docker', 'resource_class': 'light'}

    async def scenario():
        token = headers.set({'X-Viventium-Tenant-Id': 'tenant', 'X-Viventium-User-Id': 'owner',
                             'X-Viventium-Message-Id': 'synthetic_message'})
        scope = anyio.CancelScope()
        try:
            async with anyio.create_task_group() as tasks:
                async def discarded_response():
                    with scope:
                        await server.call_tool('worker_delegate_once', arguments)
                tasks.start_soon(discarded_response)
                assert await anyio.to_thread.run_sync(committed.wait, 2)
                scope.cancel()
                release.set()
            # Simulate a client reconnect after losing the first receipt.
            await server.call_tool('worker_delegate_once', arguments)
        finally:
            headers.reset(token)

    asyncio.run(scenario())
    assert len(calls) == 2 and calls[0] == calls[1]
    with store._connect() as conn:
        assert {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('delegations', 'projects', 'workers', 'runs')} == {
                    'delegations': 1, 'projects': 1, 'workers': 1, 'runs': 1}
