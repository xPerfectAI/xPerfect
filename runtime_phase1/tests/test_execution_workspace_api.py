from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import RuntimeInfo


class ReadySharedRuntime:
    preflight_uses_cli_subprocess = False

    def resolve_model(self, profile, execution_mode='docker'):
        return f'synthetic/{profile}'

    def preflight_worker_profile(self, profile, execution_mode='docker'):
        return None

    def shared_workspace_readiness(self, workspace):
        return {'available': True, 'code': 'ready', 'accounting_version': 'workspace-v1'}

    def prepare_worker_workspace(self, worker):
        return RuntimeInfo(
            runtime='synthetic', model=worker.get('model') or 'synthetic/model',
            gateway_url='', gateway_port=None, gateway_token=None,
            session_key=f"worker:{worker['worker_id']}",
            state_dir=f"/tmp/{worker['worker_id']}/state",
            workspace_dir=f"/tmp/{worker['worker_id']}/workspace", pid=None,
        )


def test_shared_metadata_creation_and_truthful_admission_gate(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'synthetic-owner')
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime_backend='stub', reconcile_on_startup=False)
    with TestClient(app) as client:
        project = client.post('/v1/projects', json={'owner_id':'synthetic-owner', 'title':'Shared test',
                                                   'goal':'Work together', 'default_worker_profile':'codex-cli'})
        assert project.status_code == 201, project.text
        created = client.post('/v1/projects/'+project.json()['project_id']+'/execution-workspaces', json={})
        assert created.status_code == 201, created.text
        workspace = created.json()
        assert workspace['mode'] == 'shared'
        assert workspace['members'] == []
        assert workspace['runtime_readiness']['available'] is False
        roster = client.get('/v1/workspaces/'+workspace['workspace_id']+'/members')
        assert roster.status_code == 200
        assert roster.json()['runtime_readiness']['available'] is False
        result = client.post('/v1/workspaces/'+workspace['workspace_id']+'/members', json={})
        assert result.status_code == 409, result.text
        assert 'not available' in result.json()['detail']['message'].lower()
        assert client.get('/v1/workspaces/'+workspace['workspace_id']+'/members').json()['members'] == []


def test_shared_member_request_cannot_override_owner_or_placement(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'synthetic-owner')
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime_backend='stub', reconcile_on_startup=False)
    with TestClient(app) as client:
        result = client.post('/v1/workspaces/wsp_missing/members',
                             json={'owner_id':'another-owner', 'execution_mode':'host'})
        assert result.status_code == 422
        assert client.post('/v1/workspaces/wsp_missing/members', json={}).status_code == 404


def test_shared_member_admission_is_deferred_and_persists_typed_selection(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'synthetic-owner')
    monkeypatch.setenv('WPR_MODEL_GROK_BUILD', 'grok-4.6')
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime=ReadySharedRuntime(), reconcile_on_startup=False)
    with TestClient(app) as client:
        project = client.post('/v1/projects', json={
            'owner_id': 'synthetic-owner', 'title': 'Shared ready',
            'goal': 'Work together', 'default_worker_profile': 'grok-build',
        }).json()
        workspace = client.post(
            f"/v1/projects/{project['project_id']}/execution-workspaces", json={}
        ).json()
        account = app.state.control_plane.create_provider_account(
            tenant_id='local', owner_id='synthetic-owner', provider='grok',
            label='Synthetic Grok', auth_method='subscription',
            platform_support='supported', secret_locator='native-home://synthetic-grok',
            status='ready', make_default=True,
        )
        response = client.post(
            f"/v1/workspaces/{workspace['workspace_id']}/members",
            json={
                'profile': 'grok-build', 'effort': 'high',
                'provider_account_policy': 'personal_required',
                'provider_account_id': account['account_id'],
            },
        )
        assert response.status_code == 201, response.text
        member = response.json()
        assert member['state'] == 'paused'
        roster = client.get(f"/v1/workspaces/{workspace['workspace_id']}/members").json()
        assert roster['members'][0]['provider_account'] == {
            'policy': 'personal_required', 'account_id': account['account_id']
        }
        assert roster['members'][0]['effort'] == 'high'
        assert app.state.store.reserve_workspace_member_identity(
            member['worker_id'], tenant_id='local', owner_id='synthetic-owner'
        )['member_uid'] >= 20001


def test_busy_shared_workspace_rechecks_after_capacity_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'synthetic-owner')
    runtime = ReadySharedRuntime()
    capacity = {'ready': False, 'preflights': 0}
    runtime.shared_workspace_readiness = lambda _workspace: {
        'available': capacity['ready'],
        'code': 'ready' if capacity['ready'] else 'shared_capacity_busy',
    }
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime=runtime, reconcile_on_startup=False)
    with TestClient(app) as client:
        project = client.post('/v1/projects', json={
            'owner_id': 'synthetic-owner', 'title': 'Capacity recovery',
            'goal': 'Admit a member after idle compute is released',
            'default_worker_profile': 'codex-cli',
        }).json()
        workspace = client.post(
            f"/v1/projects/{project['project_id']}/execution-workspaces", json={}
        ).json()

        def recover_during_preflight(*_args, **_kwargs):
            capacity['preflights'] += 1
            capacity['ready'] = True
            return {}

        monkeypatch.setattr(app.state.service, '_reserved_runtime_preflight', recover_during_preflight)
        response = client.post(f"/v1/workspaces/{workspace['workspace_id']}/members", json={})
        assert response.status_code == 201, response.text
        assert capacity['preflights'] == 1
        assert response.json()['state'] == 'paused'


def test_busy_shared_workspace_still_rejects_when_preflight_cannot_recover(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'synthetic-owner')
    runtime = ReadySharedRuntime()
    runtime.shared_workspace_readiness = lambda _workspace: {
        'available': False, 'code': 'shared_capacity_busy',
    }
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime=runtime, reconcile_on_startup=False)
    with TestClient(app) as client:
        project = client.post('/v1/projects', json={
            'owner_id': 'synthetic-owner', 'title': 'Busy capacity',
            'goal': 'Retain fail-closed admission',
            'default_worker_profile': 'codex-cli',
        }).json()
        workspace = client.post(
            f"/v1/projects/{project['project_id']}/execution-workspaces", json={}
        ).json()
        preflights = []
        monkeypatch.setattr(
            app.state.service, '_reserved_runtime_preflight',
            lambda *_args, **_kwargs: preflights.append(True) or {},
        )
        response = client.post(f"/v1/workspaces/{workspace['workspace_id']}/members", json={})
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'shared_capacity_busy'
        assert preflights == [True]
        roster = client.get(f"/v1/workspaces/{workspace['workspace_id']}/members").json()
        assert roster['members'] == []


def test_shared_member_rejects_openclaw_and_wrong_owner_account_without_row(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'synthetic-owner')
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime=ReadySharedRuntime(), reconcile_on_startup=False)
    with TestClient(app) as client:
        project = client.post('/v1/projects', json={
            'owner_id': 'synthetic-owner', 'title': 'Shared rejection',
            'goal': 'Reject unsupported', 'default_worker_profile': 'codex-cli',
        }).json()
        workspace = client.post(
            f"/v1/projects/{project['project_id']}/execution-workspaces", json={}
        ).json()
        unsupported = client.post(
            f"/v1/workspaces/{workspace['workspace_id']}/members",
            json={'profile': 'openclaw-general'},
        )
        assert unsupported.status_code == 409
        assert unsupported.json()['detail']['code'] == 'shared_profile_unavailable'
        wrong_owner = client.post(
            f"/v1/workspaces/{workspace['workspace_id']}/members",
            json={'provider_account_policy': 'personal_required', 'provider_account_id': 'acct_other'},
        )
        assert wrong_owner.status_code == 409
        assert wrong_owner.json()['detail']['code'] == 'shared_provider_account_unavailable'
        assert client.get(f"/v1/workspaces/{workspace['workspace_id']}/members").json()['members'] == []
