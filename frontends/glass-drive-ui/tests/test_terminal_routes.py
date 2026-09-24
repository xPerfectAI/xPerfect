import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from glass_drive_ui.server import create_app
from glass_drive_ui.signed_links import sign_link_token
from glass_drive_ui import terminal_routes
from test_packaged_local_auth import gateway, login, PASSWORD


class Runtime:
    base_url='http://runtime:8766'
    def __init__(self): self.headers=[]
    def with_headers(self, headers): self.headers.append(headers); return self
    def worker_live(self, worker_id): return {'worker':{'worker_id':worker_id,'state':'ready'}}


def test_terminal_serves_scoped_local_assets_and_nonce_policy(gateway):
    gateway.provision_local_owner(password=PASSWORD)
    runtime=Runtime()
    client=TestClient(create_app(runtime_client=runtime))
    assert client.get('/ui/workers/wrk_fixture/terminal').status_code==401
    assert login(client).status_code==200
    response=client.get('/ui/workers/wrk_fixture/terminal')
    assert response.status_code==200
    assert 'unsafe-inline' not in response.headers['Content-Security-Policy']
    assert "'nonce-" in response.headers['Content-Security-Policy']
    assert '{{STYLE_NONCE}}' not in response.text
    assert 'onclick' not in response.text
    assert client.get('/static/terminal.js').status_code==200
    assert client.get('/static/vendor/xterm-5.5.0/xterm.js').status_code==200
    assert runtime.headers[-1]['X-Viventium-User-Id']=='existing-owner'
    assert client.post('/ui/workers/wrk_fixture/terminal').status_code==403
    assert client.get('/ui/workers/wrk_fixture/terminal/arbitrary').status_code==404


def test_terminal_socket_requires_session_origin_csrf_and_preserves_private_hop(gateway,monkeypatch):
    gateway.provision_local_owner(password=PASSWORD)
    runtime=Runtime(); client=TestClient(create_app(runtime_client=runtime))
    login(client)
    csrf=client.cookies['glasshive_csrf']
    url='/ws/workers/wrk_fixture/terminal'
    for origin, protocols in [('', ['xperfect-terminal','csrf.'+csrf]),
          ('https://attacker.invalid',['xperfect-terminal','csrf.'+csrf]),
          ('http://testserver',['xperfect-terminal']),('http://testserver',['xperfect-terminal','csrf.wrong'])]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url,headers={'Origin':origin},subprotocols=protocols): pass
    observations={}; sent=asyncio.Event()
    class Upstream:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def send(self, data): observations['input']=json.loads(data); sent.set()
        def __aiter__(self): return self
        async def __anext__(self):
            await sent.wait(); sent.clear()
            return 'fixture terminal output'
    def connect(target,**kwargs):
        observations.update(target=target,headers=kwargs['additional_headers'])
        return Upstream()
    monkeypatch.setattr(terminal_routes.websockets,'connect',connect)
    with client.websocket_connect(url,headers={'Origin':'http://testserver'},subprotocols=['xperfect-terminal','csrf.'+csrf]) as socket:
        assert socket.accepted_subprotocol=='xperfect-terminal'
        socket.send_text(json.dumps({'type':'input','data':'fixture command\r'}))
        assert socket.receive_text()=='fixture terminal output'
    assert observations['target']=='ws://runtime:8766/ws/workers/wrk_fixture/terminal'
    assert observations['headers']['X-WPR-Token']=='fixture-runtime-secret'
    assert observations['headers']['X-Viventium-User-Id']=='existing-owner'
    assert 'cookie' not in str(observations['headers']).lower()
    assert csrf not in observations['target']
    assert observations['input']['data']=='fixture command\r'
