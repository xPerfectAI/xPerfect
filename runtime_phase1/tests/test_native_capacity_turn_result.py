from types import SimpleNamespace
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_coordinator_application import app_core
from test_coordinator_turn_results import complete, notices
from workers_projects_runtime.coordinator import Dispatch, Goal
from workers_projects_runtime.coordinator_application import completed_turn_result, queue_turn_results
from workers_projects_runtime.service import WorkersProjectsService, HostResourceUsage
import workers_projects_runtime.service as module

GIB = 1024**3


def capacity(monkeypatch, tmp_path, *, database=16*GIB, runtime=16*GIB, known=True, percent=True):
    service = object.__new__(WorkersProjectsService)
    service.store = SimpleNamespace(db_path=tmp_path/'db', active_host_mission_worker_ids=lambda:set(),
        conclusively_terminal_host_mission_history=lambda:set(),list_host_mission_workers=lambda:[],
        list_active_host_run_leases=lambda:[])
    service.runtime = SimpleNamespace(isolated_parallel_readiness=lambda **k:{'ready':True})
    service._storage_pressure_v1=lambda:{'version':1,'healthy':not percent,'state':'critical' if percent else 'healthy',
        'usedPercent':99.2 if percent else 80.0,'availableBytes':database if known else None,
        'thresholdPercent':99.0,**({'errorCode':'storage_probe_unavailable'} if not known else {})}
    monkeypatch.setattr(module,'native_parallel_policy_enabled',lambda:True)
    monkeypatch.setattr(module,'isolated_parallel_policy_enabled',lambda:True)
    monkeypatch.setattr(module,'worker_prompt_layer_integrity_snapshot',lambda **k:{})
    monkeypatch.setattr(module,'valid_worker_prompt_layer_capability',lambda v:True)
    monkeypatch.setattr(module,'host_resource_usage',lambda _:HostResourceUsage(child_processes=0,threads=0,
        available_memory_bytes=16*GIB,available_disk_bytes=runtime))
    monkeypatch.setenv('WPR_HOST_MIN_AVAILABLE_DISK_MB','4096')
    return service


def test_native_high_used_percent_uses_existing_byte_capacity(monkeypatch,tmp_path):
    service=capacity(monkeypatch,tmp_path)
    value=service.orchestration_capabilities()
    assert value['storagePressure']['state']=='critical'
    assert value['nativeParallelReady'] is True and value['nativeStorage']=={
        'version':1,'healthy':True,'availableBytes':16*GIB,'requiredBytes':4*GIB}
    error,snapshot=service._host_resource_capacity_error({'execution_mode':'host'},_include_snapshot=True)
    assert error is None and snapshot['available']['diskBytes']==16*GIB


def test_native_keeps_both_database_and_selected_runtime_byte_floors(monkeypatch,tmp_path):
    service=capacity(monkeypatch,tmp_path,database=3*GIB,runtime=16*GIB)
    assert service.orchestration_capabilities()['nativeParallelReady'] is False
    error=service._host_resource_capacity_error({'execution_mode':'host'})
    assert error.capacity_class=='resource_pressure' and error.shortage['diskBytes']==GIB
    service=capacity(monkeypatch,tmp_path,database=16*GIB,runtime=3*GIB)
    error=service._host_resource_capacity_error({'execution_mode':'host'})
    assert error.capacity_class=='resource_pressure' and error.shortage['diskBytes']==GIB


def test_native_unknown_database_probe_fails_closed_even_zero_configured_floor(monkeypatch,tmp_path):
    service=capacity(monkeypatch,tmp_path,known=False)
    monkeypatch.setenv('WPR_HOST_MIN_AVAILABLE_DISK_MB','0')
    value=service.orchestration_capabilities()
    assert value['nativeParallelReady'] is False and value['nativeStorage']['availableBytes'] is None
    assert value['nativeStorage']['requiredBytes']==0 and value['nativeStorage']['errorCode']=='storage_probe_unavailable'
    error=service._host_resource_capacity_error({'execution_mode':'host'})
    assert error.capacity_class=='resource_probe_unavailable'


def test_docker_keeps_existing_percentage_policy_and_telemetry(monkeypatch,tmp_path):
    service=capacity(monkeypatch,tmp_path,percent=True)
    before=service._storage_pressure_v1()
    value=service.orchestration_capabilities()
    assert value['storagePressure']==before and value['isolatedParallelReady'] is False
    assert value['isolatedParallelReason']=='storage_pressure_critical'
    service=capacity(monkeypatch,tmp_path,percent=False)
    assert service.orchestration_capabilities()['isolatedParallelReady'] is True


def dispatch(core,cid):
    request,run,response=complete(core,cid,text='The launch was rejected; execution did not start.')
    core.accept_goals('local','owner',cid,'source',[Goal(id='goal',text='Make a tiny local page.')])
    core.dispatch('local','owner',cid,Dispatch(goal_id='goal',route_id='primary'))
    return request,run,response


def test_rejected_unreserved_dispatch_publishes_original_turn_once_without_fake_worker(app_core):
    core,cid,_=app_core;request,run,response=dispatch(core,cid)
    with core.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET status='rejected',http_status=409,last_error='http_rejected' WHERE event_type='coordinator.dispatch_due'")
        conn.execute("UPDATE coordinator_goals SET blocker='application_delivery_rejected' WHERE conversation_id=?",(cid,))
    before=core._goal(cid,'goal')
    result=completed_turn_result(core,'local','owner',cid,'source')
    assert result['presentation_eligible'] is True and result['superseded'] is False
    queue_turn_results(core,core._conversation('local','owner',cid));queue_turn_results(core,core._conversation('local','owner',cid))
    rows=notices(core);assert len(rows)==1
    event=json.loads(rows[0]['payload_json'])
    assert event['type']=='coordinator.turn_result_ready' and event['data']['request_id']==request['request_id']
    assert event['data']['run_id']==run['run_id'] and 'goal_id' not in event['data']
    assert core._goal(cid,'goal')==before and not before['run_id'] and not before['worker_id']


def test_valid_pending_dispatch_keeps_existing_worker_result_owner(app_core):
    core,cid,_=app_core;dispatch(core,cid)
    result=completed_turn_result(core,'local','owner',cid,'source')
    assert result['superseded'] is True and result['presentation_eligible'] is False
    queue_turn_results(core,core._conversation('local','owner',cid));assert notices(core)==[]


def test_actual_owned_worker_run_keeps_existing_goal_result_owner(app_core):
    core,cid,_=app_core;request,manager,response=dispatch(core,cid)
    with core.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET status='rejected' WHERE event_type='coordinator.dispatch_due'")
    worker=core.store.create_worker(manager['project_id'],'owner','Synthetic','worker','codex-cli','synthetic','stub','synthetic')
    run=core.store.create_run(worker['worker_id'],manager['project_id'],'Make a tiny local page.')
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=?',(worker['worker_id'],run['run_id'],cid))
    result=completed_turn_result(core,'local','owner',cid,'source')
    assert result['superseded'] is True and result['presentation_eligible'] is False
    queue_turn_results(core,core._conversation('local','owner',cid));assert notices(core)==[]
