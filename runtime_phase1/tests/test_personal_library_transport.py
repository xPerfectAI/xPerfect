"""Transport scope and canonical-operation parity, independent of provider inference."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from workers_projects_runtime.auth import AuthContext
from workers_projects_runtime.personal_library_api import install_personal_library_routes
from workers_projects_runtime.personal_library_mcp import register_personal_library_tools, personal_library_tool_manifest


class Library:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        def op(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in {"list_worker_items", "capture_file"} and args[2] != "worker-a":
                # Read-only ownership lives in the canonical Library/Files owner.
                from fastapi import HTTPException
                raise HTTPException(404)
            return {"items": []} if name in {"list_items", "list_worker_items"} else {"operation": name}
        return op


def app_for(library):
    app = FastAPI()
    def context(request):
        return AuthContext(tenant_id="synthetic", user_id=request.headers.get("x-owner", "owner-a"),
            role=request.headers.get("x-role", "member"), enterprise=True,
            auth_mode=request.headers.get("x-mode", "signed_internal_assertion"),
            scopes=("workspaces:write",) if request.headers.get("x-scope", "write") == "write" else ())
    def worker(worker_id, request, heal=False):
        assert not heal
        if worker_id != "worker-a":
            from fastapi import HTTPException
            raise HTTPException(404)
    install_personal_library_routes(app, SimpleNamespace(personal_library=library), context, worker)
    return app


def test_private_reads_are_management_scoped_and_cannot_spoof_owner():
    lib = Library();client = TestClient(app_for(lib))
    for headers in ({"x-role":"viewer"},{"x-mode":"signed_link"},{"x-scope":"read"},{"x-owner":""}):
        assert client.get("/v1/personal-library", headers=headers).status_code == 403
        assert client.get("/v1/workspaces/worker-a/personal-library", headers=headers).status_code == 403
    assert not lib.calls
    assert client.post("/v1/personal-library", json={"name":"Example", "kind":"note", "text":"Reference", "owner_id":"victim", "idempotency_key":"stable"}).status_code == 201
    assert lib.calls[-1][1][:2] == ("synthetic", "owner-a")


def test_routes_use_exact_versions_and_distinguish_delete_confirmation():
    lib=Library();client=TestClient(app_for(lib));ref={"item_id":"item-a","version_id":"version-a"}
    assert client.post("/v1/personal-library/resolve",json={"refs":[ref]}).json()=={"operation":"resolve_refs"}
    assert client.get("/v1/personal-library/item-a?version_id=version-a").status_code==200
    assert lib.calls[-1][2]["version_id"]=="version-a"
    assert client.patch("/v1/personal-library/item-a",json={"name":"Updated","expected_version_id":"version-a"}).status_code==200
    assert lib.calls[-1][1][-1]=="version-a"
    assert client.delete("/v1/personal-library/item-a").status_code==200
    assert lib.calls[-1][2]["confirm"] is False
    assert client.delete("/v1/personal-library/item-a?confirm=true").status_code==200
    assert lib.calls[-1][2]["confirm"] is True
    assert client.post("/v1/workspaces/worker-a/personal-library",json={"refs":[ref],"request_key":"stable","confirm_native":False}).status_code==200
    assert lib.calls[-1][1][2:]==("worker-a",[ref],"stable")
    assert client.get("/v1/workspaces/other/personal-library").status_code==404
    assert client.delete("/v1/workspaces/worker-a/personal-library/application-a").status_code==200


def test_owner_mcp_uses_same_routes_and_does_not_add_queen_tools():
    class Server:
        def __init__(self):self.tools={}
        def tool(self, **meta):
            def register(f):self.tools[meta['name']]=f;return f
            return register
    class Client:
        def __init__(self):self.calls=[]
        def _request(self, method, path, **kw):self.calls.append((method,path,kw));return {"ok":True}
    server=Server();client=Client();register_personal_library_tools(server,client)
    assert set(server.tools)==set(personal_library_tool_manifest()["tools"])
    assert "coordinator_dispatch" not in server.tools and "list_context" not in server.tools
    refs=[{"item_id":"item-a","version_id":"version-a"}]
    assert server.tools["personal_library_resolve"](refs)=={"ok":True}
    assert client.calls[-1]==("POST","/v1/personal-library/resolve",{"json_body":{"refs":refs},"require_write_scope":True})
    server.tools["personal_library_apply"]("worker-a",refs,"stable")
    assert client.calls[-1][1]=="/v1/workspaces/worker-a/personal-library"
    assert client.calls[-1][2]["json_body"]["confirm_native"] is False
    server.tools["personal_library_delete"]("item-a")
    assert "confirm=false" in client.calls[-1][1]
    assert len(personal_library_tool_manifest()["sha256"])==64


def test_malformed_confirmation_cannot_authorize_projection_or_archive():
    lib=Library(); client=TestClient(app_for(lib))
    for value in ("false", "true", 0, 1, None, []):
        assert client.post("/v1/workspaces/worker-a/personal-library", json={
            "refs": [], "request_key": "stable", "confirm_native": value}).status_code == 422
        assert client.post("/v1/personal-library/item-a/archive", json={
            "archived": value}).status_code == 422
    assert not lib.calls
    assert client.post("/v1/workspaces/worker-a/personal-library/capture", json={
        "file_id":"file-a", "revision":"revision-a", "idempotency_key":"stable"}).status_code == 201
    assert lib.calls[-1][1][-2] is None


def test_actual_closed_workspace_library_transport_keeps_owner_reads_only(tmp_path, monkeypatch):
    from workers_projects_runtime.api import create_app
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    monkeypatch.setenv('WPR_API_TOKEN','synthetic-library-token')
    monkeypatch.setenv('GLASSHIVE_SECURITY_MODE','local')
    monkeypatch.setenv('GLASSHIVE_AUTH_MODE','local')
    monkeypatch.setenv('WPR_BOOTSTRAP_SOURCE_ROOTS',str(tmp_path))
    monkeypatch.delenv('GLASSHIVE_ENTERPRISE_MODE',raising=False)
    monkeypatch.delenv('WPR_ENTERPRISE_MODE',raising=False)
    app = create_app(db_path=str(tmp_path/'runtime.db'), runtime_backend='stub', runtime=StubRuntime())
    service = app.state.service
    project = service.store.create_project('owner-a','Completed job','Saved work','claude-code')
    worker = service.store.create_worker(project['project_id'],'owner-a','Specialist','assistant','claude-code','local','claude','exact')
    workspace = tmp_path/'workspace';workspace.mkdir()
    service.store.update_worker(worker['worker_id'],workspace_dir=str(workspace))
    saved = service.personal_library.create_item('local','owner-a',{'name':'Reference','kind':'note','text':'Exact saved evidence'},'note')
    refs = [{'item_id':saved['item_id'],'version_id':saved['version_id']}]
    added = service.personal_library.apply_to_worker('local','owner-a',worker['worker_id'],refs,'apply')
    application_id = added['items'][0]['application_id']
    (workspace/'finished.txt').write_text('Exact completed output')
    file = next(item for item in service.files.list_files(worker['worker_id'],'local','owner-a')['items'] if item['name']=='finished.txt')
    service.store.update_worker(worker['worker_id'],state='terminated')
    headers = {'Authorization':'Bearer synthetic-library-token','x-viventium-user-id':'owner-a'}
    path = '/v1/workspaces/'+worker['worker_id']+'/personal-library'
    with TestClient(app) as client:
        listed = client.get(path,headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()['items'][0]['application_id'] == application_id
        foreign = {**headers,'x-viventium-user-id':'other-owner'}
        viewer = {**headers,'x-viventium-user-role':'viewer'}
        assert client.get(path,headers=foreign).status_code == 404
        assert client.get(path,headers=viewer).status_code == 403
        captured = client.post(path+'/capture',headers=headers,json={'file_id':file['file_id'],'revision':file['revision'],'idempotency_key':'completed-output'})
        assert captured.status_code == 201, captured.text
        assert client.post(path,headers=headers,json={'refs':refs,'request_key':'closed-apply'}).status_code == 404
        assert client.delete(path+'/'+application_id,headers=headers).status_code == 404
        assert client.post(path+'/'+application_id+'/undo',headers=headers).status_code == 404
        assert client.post(path+'/capture',headers=foreign,json={'file_id':file['file_id'],'revision':file['revision'],'idempotency_key':'foreign'}).status_code == 404
        assert client.post(path+'/capture',headers=viewer,json={'file_id':file['file_id'],'revision':file['revision'],'idempotency_key':'viewer'}).status_code == 403
        assert service.store.get_worker(worker['worker_id'])['state'] == 'terminated'


def test_only_observational_library_tools_have_read_only_metadata():
    """Metadata describes effects; it does not replace private owner authorization."""
    from mcp.types import ToolAnnotations
    class Server:
        def __init__(self):
            self.tools, self.metadata = {}, {}
        def tool(self, **meta):
            def register(function):
                self.tools[meta["name"]] = function
                self.metadata[meta["name"]] = meta
                return function
            return register
    class Client:
        def __init__(self):
            self.calls = []
        def _request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"items": []}
    server, client = Server(), Client()
    register_personal_library_tools(server, client)
    reads = {"personal_library_list", "personal_library_read", "personal_library_resolve", "personal_library_workspace"}
    for name, metadata in server.metadata.items():
        annotations = metadata.get("annotations")
        assert bool(annotations and annotations.readOnlyHint) == (name in reads)
        if name in reads:
            assert isinstance(annotations, ToolAnnotations)
            assert annotations.destructiveHint is False
            assert annotations.openWorldHint is False
    server.tools["personal_library_read"]("item-synthetic")
    assert client.calls[-1][2]["require_write_scope"] is True
    server.tools["personal_library_resolve"]([])
    assert client.calls[-1][2]["require_write_scope"] is True
