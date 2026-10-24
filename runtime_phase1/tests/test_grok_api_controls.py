from contextlib import nullcontext
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.models import NativeControlRequest


def test_native_control_fences_durable_run_and_attempt_before_calling_runtime():
    calls=[]
    service=object.__new__(WorkersProjectsService)
    service._worker_compute_release_lock=lambda _,**kwargs:nullcontext()
    service.require_worker=lambda _: {'worker_id':'worker','profile':'grok-build'}
    service._ensure_execution_allowed=lambda _:None
    service.store=SimpleNamespace(get_active_run=lambda _: {'run_id':'run','active_attempt_id':'attempt','state':'running'})
    service.runtime=SimpleNamespace(native_control=lambda worker,**kw: calls.append(kw) or {'status':'queued'}, native_control_state=lambda worker,**kw:kw)
    with pytest.raises(ValueError,match='stale'):
        service.native_worker_control('worker',run_id='run',attempt_id='old',action='cancel')
    assert calls==[]
    assert service.native_worker_control('worker',run_id='run',attempt_id='attempt',action='interject',payload={'text':'direction'})=={'status':'queued'}
    assert calls[0]['payload']=={'text':'direction'}
    assert service.native_worker_control('worker')=={'run_id':'run','attempt_id':'attempt',
        'capabilities': {'interject':False,'cancel':False,'permission':False}}


@pytest.mark.parametrize('actions', [{'interject':True}, {'interject':True,'cancel':True},
                                    {'permission':True}, {'interject':'true'}])
def test_native_state_actions_come_from_exact_runtime_capability_probe(actions):
    service=object.__new__(WorkersProjectsService)
    service._worker_compute_release_lock=lambda _,**kwargs:nullcontext()
    service.require_worker=lambda _: {'worker_id':'worker','profile':'synthetic-native'}
    service.store=SimpleNamespace(get_active_run=lambda _: {'run_id':'run','active_attempt_id':'attempt','state':'running'})
    calls=[]
    service.runtime=SimpleNamespace(
        native_control_state=lambda worker,**kw:kw,
        supports_native_control=lambda worker,action,**kw:calls.append((worker,action,kw)) or actions.get(action,False))
    state=service.native_worker_control('worker')
    assert state['capabilities']=={action:actions.get(action) is True for action in ('interject','cancel','permission')}
    assert [item[1] for item in calls]==['interject','cancel','permission']
    assert all(item[2]=={'run_id':'run','attempt_id':'attempt'} for item in calls)


def test_public_controls_require_existing_worker_authorization(tmp_path):
    app=create_app(str(tmp_path/'runtime.db'),runtime_backend='stub',reconcile_on_startup=False)
    client=TestClient(app)
    response=client.post('/v1/workers/missing/native-control',json={'run_id':'run','attempt_id':'attempt','action':'cancel'})
    assert response.status_code in (401,403,404)
    schema=app.openapi()
    assert '/v1/workers/{worker_id}/native-control' in schema['paths']
    with pytest.raises(ValueError):
        NativeControlRequest(run_id='run',attempt_id='attempt',action='allow_everything')
