import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from workers_projects_runtime import mcp_server as m


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'local-linux')
    monkeypatch.setenv('WPR_DEFAULT_OWNER_ID', 'existing-owner')
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'existing-owner')
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE', 'local')
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE', 'false')
    monkeypatch.setattr(m, 'get_access_token', lambda: None)
    monkeypatch.setattr(m, 'get_http_headers', lambda: {})
    monkeypatch.setattr(m, 'DEFAULT_MCP_API_TOKEN', 'fixture-mcp-client-key')


def test_fixed_owner_projection_and_explicit_tool_conflicts(local, monkeypatch):
    assert m._request_owner_id(None) == 'existing-owner'
    assert m._account_request_scope(None) == ('local', 'existing-owner')
    assert m.WorkersProjectsApiClient('http://runtime')._owner_id(None) == 'existing-owner'
    for resolve in (m._request_owner_id, m._account_request_scope, m.WorkersProjectsApiClient('http://runtime')._owner_id):
        with pytest.raises(PermissionError, match='local owner'):
            resolve('other-owner')
    for primary in (m.HEADER_USER_ID,m.HEADER_STORAGE_USER_ID,m.HEADER_TENANT_ID,m.HEADER_USER_ROLE):
        for name in (primary,*m.HEADER_ALIASES.get(primary,())):
            monkeypatch.setattr(m,'get_http_headers',lambda name=name: {name:'conflicting'})
            with pytest.raises(PermissionError, match='local owner'):
                m._request_headers()


def test_only_mcp_client_key_and_fixed_owner_authenticate(local):
    async def ok(request):
        return JSONResponse({'ok':True})
    app = Starlette(routes=[Route('/mcp',ok,methods=['POST'])])
    app.add_middleware(m.McpHttpAuthMiddleware)
    client=TestClient(app)
    for key in ('','fixture-runtime-secret','fixture-native-bearer','fixture-browser-password'):
        assert client.post('/mcp',headers={'Authorization':'Bearer '+key}).status_code == 401
    headers={'Authorization':'Bearer fixture-mcp-client-key'}
    assert client.post('/mcp',headers=headers).status_code == 200
    for primary in (m.HEADER_USER_ID,m.HEADER_STORAGE_USER_ID,m.HEADER_TENANT_ID,m.HEADER_USER_ROLE):
        for name in (primary,*m.HEADER_ALIASES.get(primary,())):
            assert client.post('/mcp',headers={**headers,name:'another-owner'}).status_code == 401


def test_outbound_uses_fixed_owner_and_private_runtime_credential(local,monkeypatch):
    requests=[]
    class FakeClient:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def request(self,method,url,**kwargs):
            import httpx
            requests.append(kwargs)
            return httpx.Response(200,json={'ok':True},request=httpx.Request(method,url))
    monkeypatch.setattr(m.httpx,'Client',FakeClient)
    m.WorkersProjectsApiClient('http://runtime',api_token='fixture-private-runtime-secret').health()
    headers=requests[0]['headers']
    assert headers['Authorization']=='Bearer fixture-private-runtime-secret'
    assert headers[m.HEADER_USER_ID]=='existing-owner'
    assert headers[m.HEADER_TENANT_ID]=='local'
    assert headers[m.HEADER_USER_ROLE]=='tenant_admin'
    assert 'fixture-mcp-client-key' not in str(requests)
