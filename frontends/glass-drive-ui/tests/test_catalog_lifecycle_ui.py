"""Thin catalog UI transports the same typed lifecycle contract, never a second owner."""
from fastapi.testclient import TestClient
from glass_drive_ui.server import create_app
from test_server import FakeRuntimeClient


class CatalogClient(FakeRuntimeClient):
    def __init__(self):
        super().__init__()
        self.catalog_calls = []

    def _request(self, method, path, *, json_body=None):
        self.catalog_calls.append((method,path,json_body))
        return {'template_id':'wst_example','worker_id':'wrk_1','catalog_revision':2,'catalog_state':'active','items':[]}


def test_ui_proxy_forwards_revision_confirmation_version_and_owner_path():
    runtime = CatalogClient()
    client = TestClient(create_app(runtime_client=runtime))
    cases = [
        ('GET','/api/workspace-templates/wst_example',None),
        ('GET','/api/workspace-templates/wst_example/versions',None),
        ('PATCH','/api/workspace-templates/wst_example',{'name':'Research','description':'Useful work','expected_catalog_revision':1}),
        ('POST','/api/workspace-templates/wst_example/state',{'state':'archived','expected_catalog_revision':1}),
        ('DELETE','/api/workspace-templates/wst_example',{'confirm':True,'expected_catalog_revision':1}),
        ('POST','/api/workspace-templates/wst_example/duplicate',{'name':'Copy','idempotency_key':'stable-copy-key'}),
        ('DELETE','/api/workspaces/wrk_1',{'confirm':True,'expected_catalog_revision':1}),
        ('POST','/api/workspaces/wrk_1/restore',{'expected_catalog_revision':2}),
    ]
    for method,path,payload in cases:
        response=client.request(method,path,json=payload)
        assert response.status_code in (200,201), response.text
        forwarded=runtime.catalog_calls[-1]
        assert forwarded[0] == method
        assert forwarded[1] == path.replace('/api/','/v1/',1)
        if payload:
            assert all(forwarded[2][key] == value for key,value in payload.items())
    before=len(runtime.catalog_calls)
    assert client.request('DELETE','/api/workspaces/wrk_1',json={'confirm':True,'expected_catalog_revision':0}).status_code == 422
    assert len(runtime.catalog_calls) == before
