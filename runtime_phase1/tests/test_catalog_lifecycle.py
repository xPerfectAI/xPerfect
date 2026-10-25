"""Catalog CRUD preserves immutable work and reuses native Close and Library owners."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.control_plane import ControlPlaneConflict, ControlPlaneStore
from workers_projects_runtime.store import Store
from test_workspace_templates import _workspace


def setup(tmp_path):
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime_backend='stub')
    client = TestClient(app)
    worker = _workspace(client)
    template = client.post(f"/v1/workspaces/{worker['worker_id']}/templates", json={'name': 'Research expert'}).json()
    return app, client, worker, template


def lifecycle(client, item, state):
    path = f"/v1/workspace-templates/{item['template_id']}"
    body = {'expected_catalog_revision': item['catalog_revision'], 'confirm': True}
    return client.request('DELETE' if state == 'deleted' else 'POST', path + ('' if state == 'deleted' else '/state'), json={**body, 'state': state if state != 'deleted' else 'active'})


def test_expert_revision_history_copy_remove_restore_preserve_immutable_content(tmp_path):
    app, client, worker, original = setup(tmp_path)
    old = app.state.control_plane.get_workspace_template(template_id=original['template_id'], tenant_id='local', owner_id='demo-owner')
    path = f"/v1/workspace-templates/{original['template_id']}"
    patch = {'name': 'Senior researcher', 'description': 'Evidence-led work', 'expected_catalog_revision': 1}
    revised = client.patch(path, json=patch)
    assert revised.status_code == 200, revised.text
    revised = revised.json()
    assert revised['lineage_id'] == original['lineage_id'] and revised['version'] == 2
    assert client.patch(path, json=patch).json()['template_id'] == revised['template_id']
    assert client.patch(path, json={**patch, 'name': 'Concurrent change'}).status_code == 409
    assert client.get('/v1/workspace-templates').json()['items'][0]['template_id'] == revised['template_id']
    assert len(client.get(path + '/versions').json()['items']) == 2
    assert app.state.control_plane.get_workspace_template(template_id=original['template_id'], tenant_id='local', owner_id='demo-owner')['content'] == old['content']
    duplicate = client.post(path + '/duplicate', json={'name': 'Research copy', 'idempotency_key': 'expert-copy-test'})
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()['lineage_id'] != original['lineage_id']
    assert client.post(path + '/duplicate', json={'name': 'Research copy', 'idempotency_key': 'expert-copy-test'}).json()['template_id'] == duplicate.json()['template_id']
    assert client.post(path + '/duplicate', json={'name': 'Different', 'idempotency_key': 'expert-copy-test'}).status_code == 409
    archived = lifecycle(client, revised, 'archived').json()
    assert archived['catalog_state'] == 'archived'
    assert len(client.get('/v1/workspace-templates?state=archived').json()['items']) == 1
    assert client.post(f"/v1/workspace-templates/{original['template_id']}/instantiate", json={'idempotency_key': 'archived-new-work'}).status_code == 409
    restored = lifecycle(client, archived, 'active').json()
    removed = lifecycle(client, restored, 'deleted')
    assert removed.status_code == 200, removed.text
    assert lifecycle(client, restored, 'deleted').json()['catalog_state'] == 'deleted'
    assert client.get(path).json()['catalog_state'] == 'deleted'
    assert lifecycle(client, removed.json(), 'active').json()['catalog_state'] == 'active'
    # The original immutable version remains selectable, never silently upgraded.
    used = client.post(path + '/instantiate', json={'idempotency_key': 'old-version-exact'})
    assert used.status_code == 201, used.text
    assert used.json()['template']['version'] == 1


def test_accepted_template_use_replays_after_catalog_removal_without_new_worker(tmp_path):
    app, client, worker, template = setup(tmp_path)
    path = f"/v1/workspace-templates/{template['template_id']}/instantiate"
    body = {'idempotency_key': 'accepted-use-before-remove'}
    accepted = client.post(path, json=body).json()
    assert lifecycle(client, template, 'deleted').status_code == 200
    replay = client.post(path, json=body)
    assert replay.status_code == 201, replay.text
    assert replay.json()['workspace']['worker_id'] == accepted['workspace']['worker_id']
    assert replay.json()['idempotent_replay'] is True
    assert client.post(path, json={**body, 'name': 'Different'}).status_code == 409
    assert client.post(path, json={'idempotency_key': 'new-use-after-remove'}).status_code == 409


def test_active_lineage_limit_is_recoverable_and_revisions_do_not_consume_slots(tmp_path):
    store = Store(str(tmp_path / 'quota.db'))
    cp = ControlPlaneStore(store.db_path)
    content = {'schema_version': 1, 'project': {}, 'worker': {'profile': 'codex-cli', 'execution_mode': 'docker'}, 'library_refs': []}
    items = [cp.create_workspace_template(tenant_id='t', owner_id='o', name=f'Expert {n}', description='', content=content) for n in range(100)]
    for _ in range(3):
        revised = cp.create_workspace_template(tenant_id='t', owner_id='o', name='Revision', description='', content=content, lineage_id=items[0]['lineage_id'])
    assert revised['version'] == 4
    with pytest.raises(ControlPlaneConflict, match='Archive or remove'):
        cp.create_workspace_template(tenant_id='t', owner_id='o', name='Overflow', description='', content=content)
    archived = cp.set_workspace_template_state(template_id=revised['template_id'], tenant_id='t', owner_id='o', state='archived', expected_catalog_revision=revised['catalog_revision'])
    created = cp.create_workspace_template(tenant_id='t', owner_id='o', name='Recovered slot', description='', content=content)
    with pytest.raises(ControlPlaneConflict, match='100 active'):
        cp.set_workspace_template_state(template_id=archived['template_id'], tenant_id='t', owner_id='o', state='active', expected_catalog_revision=archived['catalog_revision'])
    cp.set_workspace_template_state(template_id=created['template_id'], tenant_id='t', owner_id='o', state='deleted', expected_catalog_revision=1)
    assert cp.set_workspace_template_state(template_id=archived['template_id'], tenant_id='t', owner_id='o', state='active', expected_catalog_revision=archived['catalog_revision'])['catalog_state'] == 'active'
    assert len(cp.list_workspace_templates(tenant_id='t', owner_id='o')) == 100
    assert len(cp.list_workspace_templates(tenant_id='t', owner_id='o', state='all', lineage_id=items[0]['lineage_id'])) == 4


def test_limit_concurrent_last_slot_is_atomic(tmp_path):
    store = Store(str(tmp_path / 'concurrent.db'))
    cp = ControlPlaneStore(store.db_path)
    content = {'worker': {'profile': 'codex-cli', 'execution_mode': 'docker'}}
    for n in range(99):
        cp.create_workspace_template(tenant_id='t', owner_id='o', name=str(n), description='', content=content)
    def create(n):
        try:
            return cp.create_workspace_template(tenant_id='t', owner_id='o', name=str(n), description='', content=content)
        except ControlPlaneConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, [100, 101]))
    assert sum(item is not None for item in results) == 1


def test_remove_cancel_stale_retry_restore_do_not_resurrect_compute(tmp_path, monkeypatch):
    app, client, worker, template = setup(tmp_path)
    service, store = app.state.service, app.state.store
    stops = []
    original = service.runtime.terminate_worker
    monkeypatch.setattr(service.runtime, 'terminate_worker', lambda item: (stops.append(item['worker_id']), original(item))[1])
    path = f"/v1/workspaces/{worker['worker_id']}"
    assert client.request('DELETE', path, json={'expected_catalog_revision': 1}).status_code == 422
    assert not stops
    renamed = client.patch(path, json={'name': 'Renamed worker', 'expected_catalog_revision': 1}).json()
    assert renamed['catalog_revision'] == 2
    assert client.request('DELETE', path, json={'confirm': True, 'expected_catalog_revision': 1}).status_code == 409
    assert not stops
    response = client.request('DELETE', path, json={'confirm': True, 'expected_catalog_revision': 2})
    assert response.status_code == 200, response.text
    removed = response.json()
    assert removed['catalog_state'] == 'deleted' and store.get_worker(worker['worker_id'])['state'] == 'terminated'
    assert client.request('DELETE', path, json={'confirm': True, 'expected_catalog_revision': 2}).status_code == 200
    assert len(stops) == 1
    assert client.get('/v1/workspaces?kind=named').json()['items'] == []
    assert client.get('/v1/workspaces?kind=named&catalog_state=deleted').json()['items'][0]['worker_id'] == worker['worker_id']
    assert client.patch(path, json={'name': 'Hidden edit'}).status_code == 409
    restored = client.post(path + '/restore', json={'expected_catalog_revision': removed['catalog_revision']})
    assert restored.status_code == 200, restored.text
    assert restored.json()['catalog_state'] == 'active'
    assert store.get_worker(worker['worker_id'])['state'] == 'terminated'
    assert client.post(f"/v1/workers/{worker['worker_id']}/assign", json={'instruction': 'Never restart closed compute'}).status_code == 409


def test_native_close_failure_does_not_hide_worker(tmp_path, monkeypatch):
    app, client, worker, template = setup(tmp_path)
    def fail(item):
        raise RuntimeError('Synthetic native stop unconfirmed')
    monkeypatch.setattr(app.state.service.runtime, 'terminate_worker', fail)
    with pytest.raises(RuntimeError, match='stop unconfirmed'):
        client.request('DELETE', f"/v1/workspaces/{worker['worker_id']}", json={'confirm': True, 'expected_catalog_revision': 1})
    retained = app.state.store.get_worker(worker['worker_id'])
    assert retained['state'] == 'termination_failed' and retained['catalog_state'] == 'active'


def test_removed_ephemeral_worker_cannot_be_gc_claimed(tmp_path):
    app, client, worker, template = setup(tmp_path)
    store = app.state.store
    store.update_worker(worker['worker_id'], state='terminated', workspace_kind='ephemeral', catalog_state='deleted', updated_at='2000-01-01T00:00:00+00:00', touch_updated_at=False)
    assert store.list_ephemeral_workspace_gc_candidates(updated_before='2025-01-01T00:00:00+00:00') == []
    assert store.claim_ephemeral_workspace_gc(worker['worker_id'], updated_before='2025-01-01T00:00:00+00:00', now_epoch=1, claim_token='synthetic', claim_ttl_s=30) is None


def test_expert_and_worker_lifecycle_foreign_owner_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv('WPR_API_TOKEN', 'synthetic-service-token')
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE', 'true')
    monkeypatch.setenv('GLASSHIVE_AUTH_MODE', 'first_party_assertion')
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_TENANT_ID', 'synthetic-tenant')
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET', 'synthetic-different-link-secret')
    app = create_app(db_path=str(tmp_path/'scoped.db'), runtime_backend='stub')
    client = TestClient(app)
    base = {'X-WPR-Token':'synthetic-service-token', 'X-Viventium-Tenant-Id':'synthetic-tenant'}
    owner = {**base, 'X-Viventium-User-Id':'owner-a'}
    other = {**base, 'X-Viventium-User-Id':'owner-b'}
    worker = _workspace(client, headers=owner)
    template = client.post(f"/v1/workspaces/{worker['worker_id']}/templates", headers=owner, json={'name':'Owner expert'}).json()
    for path, method, body in [
        (f"/v1/workspaces/{worker['worker_id']}", 'DELETE', {'expected_catalog_revision':1,'confirm':True}),
        (f"/v1/workspaces/{worker['worker_id']}/restore", 'POST', {'expected_catalog_revision':1}),
        (f"/v1/workspace-templates/{template['template_id']}", 'PATCH', {'expected_catalog_revision':1,'name':'Foreign'}),
        (f"/v1/workspace-templates/{template['template_id']}", 'DELETE', {'expected_catalog_revision':1,'confirm':True}),
        (f"/v1/workspace-templates/{template['template_id']}/duplicate", 'POST', {'idempotency_key':'foreign-copy'}),
    ]:
        assert client.request(method,path,headers=other,json=body).status_code == 404
    assert client.get(f"/v1/workspace-templates/{template['template_id']}/versions",headers=other).status_code == 404


def test_revision_and_duplicate_keep_deleted_library_version_pinned(tmp_path, monkeypatch):
    from dataclasses import replace
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    class PreparedRuntime(StubRuntime):
        def _runtime_info(self, worker, *, pid):
            root = tmp_path / worker['worker_id']
            root.mkdir(exist_ok=True)
            return replace(super()._runtime_info(worker, pid=pid), workspace_dir=str(root))
    monkeypatch.setenv('WPR_BOOTSTRAP_SOURCE_ROOTS', str(tmp_path))
    app = create_app(db_path=str(tmp_path/'runtime.db'),runtime_backend='stub',runtime=PreparedRuntime())
    client = TestClient(app)
    worker = _workspace(client)
    item = client.post('/v1/personal-library', json={'name':'Research method','kind':'note','text':'Use exact evidence.', 'idempotency_key':'catalog-method'}).json()
    refs = [{'item_id':item['item_id'],'version_id':item['version_id']}]
    applied = client.post(f"/v1/workspaces/{worker['worker_id']}/personal-library", json={'refs':refs,'request_key':'apply-method'})
    assert applied.status_code == 200, applied.text
    template = client.post(f"/v1/workspaces/{worker['worker_id']}/templates", json={'name':'Method expert'}).json()
    assert client.delete(f"/v1/personal-library/{item['item_id']}?confirm=true").status_code == 200
    path = f"/v1/workspace-templates/{template['template_id']}"
    revised = client.patch(path, json={'name':'Updated method','description':'Same evidence','expected_catalog_revision':1})
    copied = client.post(path+'/duplicate', json={'name':'Method copy','idempotency_key':'copy-retained-method'})
    assert revised.status_code == 200, revised.text
    assert copied.status_code == 201, copied.text
    for saved in (template,revised.json(),copied.json()):
        assert saved['personal_library_refs'] == refs
        with app.state.store._connect() as conn:
            assert conn.execute("SELECT 1 FROM personal_library_holders WHERE holder_kind='workspace_template' AND holder_id=? AND version_id=?", (saved['template_id'],item['version_id'])).fetchone()
    assert lifecycle(client,revised.json(),'deleted').status_code == 200
    applied = client.post(f"/v1/workspace-templates/{copied.json()['template_id']}/instantiate", json={'idempotency_key':'use-copied-method'})
    assert applied.status_code == 201, applied.text
    assert applied.json()['template']['personal_library_refs'] == refs


def test_updated_setup_is_saved_as_new_version_with_head_cas(tmp_path):
    app, client, worker, template = setup(tmp_path)
    payload = {'name':'Refined researcher','description':'Updated setup','lineage_id':template['lineage_id'],
               'expected_template_id':template['template_id'],'expected_catalog_revision':1}
    endpoint = f"/v1/workspaces/{worker['worker_id']}/templates"
    version = client.post(endpoint,json=payload)
    assert version.status_code == 201, version.text
    assert version.json()['version'] == 2
    assert client.post(endpoint,json={**payload,'name':'Stale change'}).status_code == 409
    assert len(client.get(f"/v1/workspace-templates/{template['template_id']}/versions").json()['items']) == 2


def test_catalog_migrations_keep_existing_versions_workers_and_hashes(tmp_path):
    app, client, worker, template = setup(tmp_path)
    path = app.state.store.db_path
    with app.state.store._connect() as conn:
        before = dict(conn.execute('SELECT template_id,content_json,content_hash FROM workspace_templates WHERE template_id=?',(template['template_id'],)).fetchone())
        for column in ('catalog_state','catalog_revision'):
            conn.execute(f'ALTER TABLE workers DROP COLUMN {column}')
        for column in ('catalog_state','catalog_revision','catalog_updated_at'):
            conn.execute(f'ALTER TABLE workspace_templates DROP COLUMN {column}')
        conn.execute("UPDATE glasshive_schema_versions SET version=12 WHERE component='runtime_store'")
        conn.execute("UPDATE glasshive_schema_versions SET version=5 WHERE component='control_plane'")
    store = Store(path)
    cp = ControlPlaneStore(path)
    assert store.get_worker(worker['worker_id'])['catalog_state'] == 'active'
    assert store.get_worker(worker['worker_id'])['catalog_revision'] == 1
    with store._connect() as conn:
        assert dict(conn.execute('SELECT template_id,content_json,content_hash FROM workspace_templates WHERE template_id=?',(template['template_id'],)).fetchone()) == before
    assert cp.list_workspace_templates(tenant_id='local',owner_id='demo-owner')[0]['template_id'] == template['template_id']


def test_remove_member_keeps_other_members_and_native_uid_records(tmp_path):
    from workers_projects_runtime.service import WorkersProjectsService
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    store = Store(str(tmp_path/'members.db'))
    project = store.create_project('owner','Shared work','Retain shared work','codex-cli',tenant_id='tenant')
    workspace = store.create_execution_workspace(project_id=project['project_id'],tenant_id='tenant',owner_id='owner',execution_mode='docker')
    def member(name):
        return store.create_worker(project_id=project['project_id'],owner_id='owner',tenant_id='tenant',workspace_id=workspace['workspace_id'],name=name,role='member',profile='codex-cli',backend='',runtime='',model='',execution_mode='docker')
    first, second = member('Author'), member('Reviewer')
    with store._connect() as conn:
        identities = [dict(row) for row in conn.execute('SELECT * FROM execution_workspace_identities ORDER BY member_uid')]
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        removed = service.remove_workspace(first['worker_id'],expected_catalog_revision=1)
        assert removed['catalog_state'] == 'deleted'
        assert store.get_worker(second['worker_id'])['state'] == second['state']
        assert store.get_execution_workspace(workspace['workspace_id'],'tenant','owner')
        with store._connect() as conn:
            assert [dict(row) for row in conn.execute('SELECT * FROM execution_workspace_identities ORDER BY member_uid')] == identities
    finally:
        service.shutdown()


def test_real_mcp_catalog_actions_reach_same_api_owner(tmp_path):
    import asyncio
    from fastmcp import Client
    from workers_projects_runtime.mcp_server import WorkersProjectsApiClient, create_mcp_server
    app, http, worker, template = setup(tmp_path)
    class LocalApi(WorkersProjectsApiClient):
        def _request(self, method, path, *, json_body=None, **kwargs):
            response = http.request(method,path,json=json_body)
            response.raise_for_status()
            return response.json()
    server = create_mcp_server(api_client=LocalApi(base_url='http://testserver'))
    def data(result):
        body = result.structured_content or {}
        return body.get('result',body)
    async def exercise():
        async with Client(server) as mcp:
            tools = {tool.name:tool for tool in await mcp.list_tools()}
            assert tools['workspace_template_get'].annotations.readOnlyHint is True
            saved = data(await mcp.call_tool('workspace_template_get',{'template_id':template['template_id']}))
            revised = data(await mcp.call_tool('workspace_template_manage',{'template_id':saved['template_id'],'action':'edit',
                'name':'MCP researcher','description':'Same API owner','expected_catalog_revision':saved['catalog_revision']}))
            assert revised['version'] == 2
            for action in ('archive','restore','remove','restore'):
                revised = data(await mcp.call_tool('workspace_template_manage',{'template_id':revised['template_id'],'action':action,
                    'expected_catalog_revision':revised['catalog_revision'],'confirm':action=='remove'}))
            assert revised['catalog_state'] == 'active'
            removed = data(await mcp.call_tool('workspace_remove',{'worker_id':worker['worker_id'],
                'expected_catalog_revision':1,'confirm':True}))
            assert removed['catalog_state'] == 'deleted'
            restored = data(await mcp.call_tool('workspace_restore',{'worker_id':worker['worker_id'],
                'expected_catalog_revision':removed['catalog_revision']}))
            assert restored['catalog_state'] == 'active'
            assert app.state.store.get_worker(worker['worker_id'])['state'] == 'terminated'
    asyncio.run(exercise())


@pytest.mark.parametrize('component', ['runtime_store','control_plane'])
def test_catalog_migration_failure_rolls_back_and_backup_remains_old_readable(tmp_path, monkeypatch, component):
    import sqlite3
    import workers_projects_runtime.store as store_module
    import workers_projects_runtime.control_plane as cp_module
    from workers_projects_runtime.schema_version import require_compatible_schema
    app, client, worker, template = setup(tmp_path)
    database = app.state.store.db_path
    old_version = 12 if component == 'runtime_store' else 5
    table = 'workers' if component == 'runtime_store' else 'workspace_templates'
    columns = ('catalog_state','catalog_revision') if component == 'runtime_store' else ('catalog_state','catalog_revision','catalog_updated_at')
    with app.state.store._connect() as conn:
        for column in columns:
            conn.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        conn.execute('UPDATE glasshive_schema_versions SET version=? WHERE component=?',(old_version,component))
    backup = tmp_path/'before-upgrade.db'
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    with sqlite3.connect(database) as conn:
        before = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
    module = store_module if component == 'runtime_store' else cp_module
    original = module.record_schema_version
    def fail_at_commit(conn, **kwargs):
        if kwargs.get('component') == component:
            raise RuntimeError('Synthetic upgrade interruption')
        return original(conn, **kwargs)
    monkeypatch.setattr(module,'record_schema_version',fail_at_commit)
    with pytest.raises(RuntimeError,match='upgrade interruption'):
        (Store if component == 'runtime_store' else ControlPlaneStore)(database)
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0] == before
        assert conn.execute('SELECT version FROM glasshive_schema_versions WHERE component=?',(component,)).fetchone()[0] == old_version
    monkeypatch.setattr(module,'record_schema_version',original)
    (Store if component == 'runtime_store' else ControlPlaneStore)(database)
    with sqlite3.connect(database) as conn:
        with pytest.raises(RuntimeError):
            require_compatible_schema(conn,component=component,target_version=old_version)
    with sqlite3.connect(backup) as source, sqlite3.connect(database) as destination:
        source.backup(destination)
    with sqlite3.connect(database) as conn:
        require_compatible_schema(conn,component=component,target_version=old_version)
        assert conn.execute('SELECT content_hash FROM workspace_templates WHERE template_id=?',(template['template_id'],)).fetchone()[0] == template['content_hash']


@pytest.mark.parametrize('surface', ['api','mcp'])
def test_name_only_expert_edit_preserves_description_and_independent_goal(tmp_path, surface):
    import asyncio
    from fastmcp import Client
    from workers_projects_runtime.mcp_server import WorkersProjectsApiClient, create_mcp_server
    app, client, worker, _ = setup(tmp_path)
    saved = app.state.control_plane.create_workspace_template(tenant_id='local', owner_id='demo-owner',
        name='Named expert',description='Professional background',content={'schema_version':1,
        'project':{'title':'Named expert','goal':'Keep this exact working goal'},
        'worker':{'profile':'codex-cli','execution_mode':'docker','name':'Named expert'},'library_refs':[]})
    def edit(item, name, **extra):
        body={'name':name,'expected_catalog_revision':item['catalog_revision'],**extra}
        if surface=='api':
            response=client.patch(f"/v1/workspace-templates/{item['template_id']}",json=body)
            assert response.status_code==200,response.text
            return response.json()
        class LocalApi(WorkersProjectsApiClient):
            def _request(self,method,path,*,json_body=None,**kwargs):
                response=client.request(method,path,json=json_body);response.raise_for_status();return response.json()
        async def call():
            async with Client(create_mcp_server(api_client=LocalApi(base_url='http://testserver'))) as mcp:
                result=await mcp.call_tool('workspace_template_manage',{'template_id':item['template_id'],'action':'edit',**body})
                payload=result.structured_content or {};return payload.get('result',payload)
        return asyncio.run(call())
    renamed=edit(saved,'Renamed expert')
    assert renamed['description']=='Professional background'
    exact=app.state.control_plane.get_workspace_template(template_id=renamed['template_id'],tenant_id='local',owner_id='demo-owner')
    assert exact['content']['project']['goal']=='Keep this exact working goal'
    blanked=edit(renamed,'Renamed expert',description='')
    exact=app.state.control_plane.get_workspace_template(template_id=blanked['template_id'],tenant_id='local',owner_id='demo-owner')
    assert blanked['description']=='' and exact['content']['project']['goal']==''


def test_late_native_cleanup_failure_returns_removed_worker_to_visible_recovery(tmp_path):
    app, client, worker, _ = setup(tmp_path)
    removed=client.request('DELETE',f"/v1/workspaces/{worker['worker_id']}",json={'confirm':True,'expected_catalog_revision':1}).json()
    failed=app.state.store.record_worker_termination_cleanup_failure(worker['worker_id'],'Synthetic late cleanup failure')
    assert failed['state']=='termination_failed' and failed['catalog_state']=='active'
    assert failed['catalog_revision']==removed['catalog_revision']+1
    visible=client.get('/v1/workspaces?kind=named').json()['items']
    assert visible[0]['worker_id']==worker['worker_id'] and visible[0]['state']=='termination_failed'
    assert client.get('/v1/workspaces?kind=named&catalog_state=deleted').json()['items']==[]


@pytest.mark.parametrize('state',['archived','deleted'])
def test_template_lifecycle_race_is_checked_inside_new_reservation(tmp_path,monkeypatch,state):
    app, client, worker, template = setup(tmp_path)
    cp=app.state.control_plane
    reserve=cp.reserve_workspace_template_instantiation
    def changed_before_transaction(**kwargs):
        cp.set_workspace_template_state(template_id=template['template_id'],tenant_id='local',owner_id='demo-owner',state=state,expected_catalog_revision=1)
        return reserve(**kwargs)
    monkeypatch.setattr(cp,'reserve_workspace_template_instantiation',changed_before_transaction)
    with app.state.store._connect() as conn:
        counts=tuple(conn.execute('SELECT (SELECT COUNT(*) FROM workers),(SELECT COUNT(*) FROM projects),(SELECT COUNT(*) FROM workspace_template_instantiations)').fetchone())
    response=client.post(f"/v1/workspace-templates/{template['template_id']}/instantiate",json={'idempotency_key':'interleave-before-new-admission'})
    assert response.status_code==409,response.text
    with app.state.store._connect() as conn:
        assert tuple(conn.execute('SELECT (SELECT COUNT(*) FROM workers),(SELECT COUNT(*) FROM projects),(SELECT COUNT(*) FROM workspace_template_instantiations)').fetchone())==counts
