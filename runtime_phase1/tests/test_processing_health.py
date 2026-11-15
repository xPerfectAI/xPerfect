import json
import pytest
from fastapi.testclient import TestClient
from workers_projects_runtime import api
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.processing_health import FAILURE_CLASSES, summarize
from workers_projects_runtime.store import Store


def outcome(state='failed', scope=('local', 'alice', 'codex-cli', 'codex-cli', 'model'), start=1, end=2, failure='provider_unauthorized'):
    return dict(scope=scope, state=state, started_at=f'2026-01-01T00:00:0{start}+00:00', ended_at=f'2026-01-01T00:00:0{end}+00:00', failure_class=failure)


@pytest.mark.parametrize('failure', sorted(FAILURE_CLASSES))
def test_failure_clears_only_on_matching_later_started_success(failure):
    failed = outcome(failure=failure)
    assert summarize([failed])['status'] == 'down'
    for other in (outcome(state='completed', start=1, end=3, failure=''), outcome(state='completed', start=3, end=4, failure='', scope=('local', 'bob', 'codex-cli', 'codex-cli', 'model')), outcome(state='paused', start=3, end=4, failure=''), outcome(state='cancelled', start=3, end=4, failure='')):
        assert summarize([failed, other])['status'] == 'down'
    assert summarize([failed, outcome(state='completed', start=3, end=4, failure='')])['status'] == 'up'


def test_unknown_and_duplicate_projection():
    assert summarize([])['status'] == 'unknown'
    assert summarize([outcome(failure='user_cancelled')])['status'] == 'unknown'
    assert summarize([outcome(), outcome()])['unresolved_failures'] == 1
    assert summarize([outcome(state='completed', start=4, end=3, failure='')])['status'] == 'unknown'


def seed(store, owner='alice', tenant='local'):
    p = store.create_project(owner, 'Fixture', 'Synthetic health', 'codex-cli', tenant_id=tenant)
    w = store.create_worker(p['project_id'], owner, 'Fixture', 'fixture', 'codex-cli', 'docker', 'codex-cli', 'model', tenant_id=tenant)
    r = store.create_run(w['worker_id'], p['project_id'], 'Synthetic input', state='failed')
    with store._connect() as c:
        c.execute('UPDATE runs SET started_at=?,runtime_invoked_at=?,ended_at=?,failure_class=? WHERE run_id=?', ('2026-01-01T00:00:01Z', '2026-01-01T00:00:01Z', '2026-01-01T00:00:02Z', 'provider_unauthorized', r['run_id']))
    return p,w,r


def test_retained_attempt_survives_restart_and_mutable_run(tmp_path):
    path=str(tmp_path/'db.sqlite');s=Store(path);p,w,r=seed(s)
    with s._connect() as c:
        c.execute("INSERT INTO run_attempts(attempt_id,run_id,attempt_number,state,claimed_at,runtime_invoked_at,ended_at,terminal_reason) VALUES (?,?,1,'failed',?,?,?,?)", ('attempt',r['run_id'],'2026-01-01T00:00:01Z','2026-01-01T00:00:01Z','2026-01-01T00:00:02Z','provider_unauthorized'))
        c.execute("UPDATE runs SET state='paused',failure_class='',ended_at=NULL,execution_binding_json=? WHERE run_id=?", (json.dumps({'route':{'profile':'codex-cli','native_model':'model'}}),r['run_id']))
        c.execute("UPDATE workers SET model='other' WHERE worker_id=?",(w['worker_id'],))
    s=Store(path)
    assert s.processing_health(owner_id='alice',profile='codex-cli',runtime='codex-cli',model='model')['status']=='down'
    assert s.processing_health(owner_id='bob')['status']=='unknown'
    assert s.processing_health(tenant_id='foreign')['status']=='unknown'
    assert s.processing_health(model='other')['status']=='unknown'
    recovered=s.create_run(w['worker_id'],p['project_id'],'Synthetic recovery',state='completed')
    with s._connect() as c:
        c.execute("UPDATE runs SET runtime_invoked_at='2026-01-01T00:00:03Z',ended_at='2026-01-01T00:00:04Z',execution_binding_json=? WHERE run_id=?", (json.dumps({'route':{'profile':'codex-cli','native_model':'model'}}),recovered['run_id']))
    assert s.processing_health(owner_id='alice',model='model')['status']=='up'


def test_authenticated_read_only_content_free_scoped_api(tmp_path,monkeypatch):
    monkeypatch.setenv('WPR_API_TOKEN','synthetic-token');monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE','0');monkeypatch.setenv('WPR_ENTERPRISE_MODE','0');monkeypatch.setenv('GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED','false')
    monkeypatch.setattr(api,'load_viventium_runtime_env',lambda:None)
    with TestClient(api.create_app(str(tmp_path/'db.sqlite'),runtime=StubRuntime())) as c:
        s=c.app.state.store;_,_,r=seed(s);before=s.get_run(r['run_id'])
        query={'owner_id':'alice','profile':'codex-cli','runtime':'codex-cli','model':'model'};url='/v1/health/processing';headers={'Authorization':'Bearer synthetic-token'}
        assert c.get(url,params=query).status_code==401
        response=c.get(url,params=query,headers=headers)
        assert response.status_code==200 and response.json()['status']=='down'
        assert 'alice' not in response.text and r['run_id'] not in response.text and 'Synthetic input' not in response.text
        assert c.get(url,params={**query,'owner_id':'bob'},headers=headers).json()['status']=='unknown'
        assert s.get_run(r['run_id'])==before
        assert c.get('/health').status_code==200


def test_enterprise_health_is_bound_to_authenticated_tenant_and_owner(tmp_path, monkeypatch):
    for key,value in {'WPR_API_TOKEN':'synthetic-token','GLASSHIVE_ENTERPRISE_MODE':'1','GLASSHIVE_AUTH_MODE':'first_party_assertion','GLASSHIVE_ENTERPRISE_TENANT_ID':'fixture-tenant','GLASSHIVE_SIGNED_LINK_SECRET':'synthetic-link-secret','GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED':'false'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setattr(api,'load_viventium_runtime_env',lambda:None)
    with TestClient(api.create_app(str(tmp_path/'db.sqlite'),runtime=StubRuntime())) as c:
        seed(c.app.state.store,tenant='fixture-tenant')
        query={'owner_id':'alice','profile':'codex-cli','runtime':'codex-cli','model':'model'}
        headers={'Authorization':'Bearer synthetic-token'}
        assert c.get('/v1/health/processing',params=query,headers=headers).status_code==401
        headers.update({'x-viventium-user-id':'alice','x-viventium-user-role':'operator'})
        assert c.get('/v1/health/processing',params=query,headers=headers).json()['status']=='down'
        headers['x-viventium-user-id']='bob'
        assert c.get('/v1/health/processing',params=query,headers=headers).json()['status']=='unknown'
        headers['x-viventium-tenant-id']='foreign'
        assert c.get('/v1/health/processing',params=query,headers=headers).status_code==401


def test_real_service_failure_and_recovery_feed_projection_without_provider_calls(tmp_path):
    import time
    from workers_projects_runtime.openclaw_runtime import ProviderAuthenticationMissingError, runtime_start_boundary, notify_runtime_started
    from workers_projects_runtime.service import WorkersProjectsService

    class Fixture(StubRuntime):
        fail = True

        def resolve_model(self, profile):
            return "model"

        def _runtime_info(self, worker, *, pid):
            from dataclasses import replace
            return replace(super()._runtime_info(worker, pid=pid), runtime="codex-cli", model="model")

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
            if self.fail:
                raise ProviderAuthenticationMissingError('Synthetic authorization failure')
            return 'Synthetic completion; no external provider contacted.'

    store = Store(str(tmp_path / 'state.sqlite3'))
    runtime = Fixture()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False, start_background_consumers=False)
    project = store.create_project('alice', 'Fixture', 'No external work', 'codex-cli')
    worker = store.create_worker(project['project_id'], 'alice', 'Fixture', 'fixture', 'codex-cli', 'docker', 'codex-cli', 'model')
    try:
        for failed in (True, False):
            runtime.fail = failed
            run = service.assign_run(worker['worker_id'], 'Synthetic test')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = store.get_run(run['run_id'])
                if current['state'] in {'completed', 'failed'}:
                    break
                time.sleep(.01)
            assert current['state'] == ('failed' if failed else 'completed')
            assert store.processing_health(owner_id='alice', profile='codex-cli', runtime='codex-cli', model='model')['status'] == ('down' if failed else 'up')
            if failed:
                assert current['failure_class'] == 'provider_auth_missing'
    finally:
        service.shutdown()
