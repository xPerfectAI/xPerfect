"""View links and viewer HTML must not disclose interactive desktop authority."""
import pytest
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.signed_links import create_signed_link_ref, sign_link_token


@pytest.fixture
def desktop_case(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE', 'true')
    monkeypatch.setenv('GLASSHIVE_AUTH_MODE', 'first_party_assertion')
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_TENANT_ID', 'tenant-example')
    monkeypatch.setenv('WPR_API_TOKEN', 'test-service-token')
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET', 'test-signed-view-key')
    app = create_app(str(tmp_path / 'runtime.db'), runtime_backend='stub', runtime=StubRuntime())
    app.state.service._ensure_worker_processor = lambda _worker: None
    client = TestClient(app)
    owner = {'X-WPR-Token': 'test-service-token', 'X-Viventium-Tenant-Id': 'tenant-example',
             'X-Viventium-User-Id': 'user-example', 'X-Viventium-User-Role': 'member'}
    project = client.post('/v1/projects', headers=owner,
        json={'title':'Desktop authority', 'goal':'Keep read access separate from desktop control', 'owner_id':'ignored'}).json()
    response = client.post('/v1/projects/' + project['project_id'] + '/workers', headers=owner,
        json={'owner_id':'ignored', 'name':'Desktop worker', 'role':'research', 'profile':'codex-cli', 'start_synchronously':False})
    assert response.status_code == 201, response.text
    worker = response.json()
    token = sign_link_token(kind='worker_view', worker_id=worker['worker_id'],
                           tenant_id=worker['tenant_id'], owner_id=worker['owner_id'])
    ref = create_signed_link_ref(token=token, target_url='/watch/' + worker['worker_id'])
    calls = []
    def describe(_worker):
        calls.append('credentials')
        return {'mode':'workstation-desktop', 'view_url':'http://desktop.example/vnc.html?password=test-desktop-credential'}
    monkeypatch.setattr(app.state.service.runtime, 'describe_worker', describe, raising=False)
    def action(_worker, requested, **_kwargs):
        calls.append('action')
        return {'action':requested, 'status':'launched', 'mode':'workstation-desktop', 'url':'/desktop/example'}
    monkeypatch.setattr(app.state.service, 'desktop_action', action)
    return client, owner, project, worker, ref, calls


def test_legacy_view_references_cannot_open_or_start_desktop(desktop_case):
    client, owner, project, worker, ref, calls = desktop_case
    for suffix in ('/desktop', '/desktop-frame'):
        response = client.get('/w/' + ref + suffix, headers=owner, follow_redirects=False)
        assert response.status_code == 403, response.text
        assert 'test-desktop-credential' not in response.text
        assert 'location' not in response.headers
    response = client.post('/w/' + ref + '/desktop-action', headers=owner, json={'action':'terminal'})
    assert response.status_code == 403, response.text
    assert not calls, 'Refusal must precede credential lookup or native side effects'
    # The authenticated owner still uses the normal typed action route.
    response = client.post('/v1/workers/' + worker['worker_id'] + '/desktop-action', headers=owner,
                           json={'action':'terminal'})
    assert response.status_code == 202, response.text
    assert calls == ['action']


def test_viewer_html_cannot_recover_desktop_credentials(desktop_case):
    client, owner, project, worker, ref, calls = desktop_case
    viewer = {**owner, 'X-Viventium-User-Role':'viewer'}
    response = client.get('/ui/workers/' + worker['worker_id'] + '/view', headers=viewer)
    assert response.status_code == 403, response.text
    response = client.get('/ui/projects/' + project['project_id'], headers=viewer)
    assert response.status_code == 200, response.text
    assert 'test-desktop-credential' not in response.text
    assert not calls, 'Read-only HTML must not obtain interactive desktop credentials'
    response = client.get('/ui/workers/' + worker['worker_id'] + '/view', headers=owner)
    assert response.status_code == 200, response.text
    assert 'test-desktop-credential' in response.text
    assert calls == ['credentials']
