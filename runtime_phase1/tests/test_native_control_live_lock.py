"""Read snapshots do not serialize on a lifecycle guard; writes keep its normal wait."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService


def service(tmp_path):
    s=object.__new__(WorkersProjectsService)
    s.store=SimpleNamespace(db_path=str(tmp_path/'runtime.db'),get_active_run=lambda _:{'run_id':'run','active_attempt_id':'attempt','state':'running'})
    s.require_worker=lambda _:{'worker_id':'worker','profile':'synthetic-native'}
    s._runtime_worker_for_run=lambda worker,run:worker
    s._ensure_execution_allowed=lambda worker:None
    calls=[]
    s.runtime=SimpleNamespace(native_control_state=lambda worker,**kw:calls.append(('state',kw)) or kw,
        supports_native_control=lambda *a,**kw:True,
        native_control=lambda worker,**kw:calls.append(('write',kw)) or {'status':'queued'})
    return s,calls


def test_native_snapshot_remains_available_while_lifecycle_guard_is_held(tmp_path):
    s,calls=service(tmp_path)
    guard=s._acquire_worker_lifecycle_guard('worker',timeout_s=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(s.native_worker_control,'worker')
        try:
            assert future.result(timeout=1)['capabilities']['interject'] is True
            assert calls[0][0]=='state'
        finally:guard.release()
    assert s.native_worker_control('worker')['capabilities']['interject'] is True
    assert calls[0][0]=='state'


def test_mutations_keep_default_guard_and_exact_attempt_fence(tmp_path):
    s,calls=service(tmp_path)
    original=s._acquire_worker_lifecycle_guard;requested=[]
    def observe(worker_id,**kwargs):
        requested.append(kwargs);return original(worker_id,**kwargs)
    s._acquire_worker_lifecycle_guard=observe
    s.native_worker_control('worker')
    assert requested==[]
    assert s.native_worker_control('worker',run_id='run',attempt_id='attempt',action='interject',payload={'text':'Exact direction'})=={'status':'queued'}
    assert requested[-1]=={} and s._worker_lifecycle_lock_timeout_s()==30
    with pytest.raises(ValueError,match='stale'):
        s.native_worker_control('worker',run_id='run',attempt_id='old',action='cancel')
    assert len([c for c in calls if c[0]=='write'])==1


def test_live_poll_returns_verified_snapshot_during_held_lifecycle_action(tmp_path,monkeypatch):
    for key,value in {'GLASSHIVE_SIGNED_LINK_SECRET':'synthetic-secret','WPR_API_TOKEN':'synthetic-token',
        'GLASSHIVE_ENTERPRISE_MODE':'true','GLASSHIVE_ENTERPRISE_TENANT_ID':'tenant-alpha',
        'GLASSHIVE_AUTH_MODE':'first_party_assertion'}.items():monkeypatch.setenv(key,value)
    app=create_app(str(tmp_path/'api.db'),runtime_backend='stub',runtime=StubRuntime(),reconcile_on_startup=False)
    store=app.state.store;service=app.state.service
    project=store.create_project('owner-a','Synthetic project','Read a status','codex-cli',tenant_id='tenant-alpha')
    worker=store.create_worker(project_id=project['project_id'],owner_id='owner-a',name='Native worker',role='worker',
        profile='codex-cli',backend='profiled',runtime='codex-cli',model='synthetic-model',tenant_id='tenant-alpha')
    run={'run_id':'run','worker_id':worker['worker_id'],'project_id':project['project_id'],'state':'running','active_attempt_id':'attempt'}
    monkeypatch.setattr(store,'get_active_run',lambda _:dict(run))
    monkeypatch.setattr(service,'_runtime_worker_for_run',lambda worker,run:worker)
    calls=[]
    monkeypatch.setattr(service.runtime,'native_control_state',lambda worker,**kw:calls.append('state') or kw,raising=False)
    monkeypatch.setattr(service.runtime,'supports_native_control',lambda *a,**kw:True,raising=False)
    headers={'X-WPR-Token':'synthetic-token','X-Viventium-Tenant-Id':'tenant-alpha',
        'X-Viventium-User-Id':'owner-a','X-Viventium-User-Role':'operator'}
    with TestClient(app) as client:
        guard=service._acquire_worker_lifecycle_guard(worker['worker_id'],timeout_s=0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.get,f"/v1/workers/{worker['worker_id']}/live",headers=headers)
            try:
                response=future.result(timeout=1)
                assert response.status_code==200
                assert response.json()['worker']['worker_id']==worker['worker_id']
                assert response.json()['native_control']['available'] is True
                assert response.json()['native_control']['capabilities']['interject'] is True
                assert calls==['state']
            finally:guard.release()
        restored=client.get(f"/v1/workers/{worker['worker_id']}/live",headers=headers)
        assert restored.status_code==200 and restored.json()['native_control']['available'] is True
        assert restored.json()['native_control']['capabilities']['interject'] is True
        assert calls==['state','state']
