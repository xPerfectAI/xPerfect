"""One verified native snapshot serves concurrent reads without control locks."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from workers_projects_runtime import native_input
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, HostClaudeCodeRuntime
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase


def service(tmp_path,runtime):
    s=object.__new__(WorkersProjectsService)
    active={'run_id':'run','active_attempt_id':'attempt','state':'running'}
    s.store=SimpleNamespace(db_path=str(tmp_path/'runtime.db'),get_active_run=lambda _:dict(active))
    s.require_worker=lambda _:{'worker_id':'worker','profile':'synthetic-native'}
    s._runtime_worker_for_run=lambda worker,run:worker
    s._ensure_execution_allowed=lambda _:None
    s.runtime=runtime
    return s,active


@pytest.fixture
def codex(tmp_path,monkeypatch):
    runtime=HostCodexCliRuntime(str(tmp_path/'runtime'))
    worker={'worker_id':'worker','profile':'codex-cli','execution_mode':'host'}
    identity=runtime._process_start_identity(os.getpid())
    active={'run_id':'run','attempt_id':'attempt','process_pid':os.getpid(),'process_start_identity':identity}
    monkeypatch.setattr(runtime,'_read_active_session',lambda _:dict(active))
    root=runtime._run_root('worker','run');root.mkdir(parents=True)
    native_input.publish(root/'native-input-context.json',{'protocol':'codex-app-server','workerId':'worker',
        'runId':'run','attemptId':'attempt','nativeSessionActive':True,'nativeSessionId':'session','nativeTurnId':'turn',
        'nativeControlCapabilities':{'interject':True,'cancel':True,'interject_graph_output':True}})
    counts={'contexts':0,'ps':0};original_context=runtime._native_midstream_context;original_run=subprocess.run
    def context(*a,**kw):counts['contexts']+=1;return original_context(*a,**kw)
    def run(command,*a,**kw):
        if command and command[0]=='ps':counts['ps']+=1
        return original_run(command,*a,**kw)
    monkeypatch.setattr(runtime,'_native_midstream_context',context)
    monkeypatch.setattr(subprocess,'run',run)
    s,active_run=service(tmp_path,runtime)
    s.require_worker=lambda _:worker
    return s,runtime,worker,active_run,counts


def test_one_real_codex_snapshot_does_one_context_and_two_process_checks(codex):
    s,runtime,worker,active,counts=codex
    state=s.native_worker_control('worker')
    assert state['capabilities']=={'interject':True,'cancel':True,'permission':False}
    assert counts=={'contexts':1,'ps':2}
    assert runtime.supports_native_control(worker,'interject_graph_output',run_id='run',attempt_id='attempt') is True


def test_two_real_codex_queries_both_verify_and_return_concurrently(codex,monkeypatch):
    s,runtime,worker,active,counts=codex
    barrier=Barrier(2);original=runtime.native_control_state
    def concurrent(*a,**kw):
        state=original(*a,**kw);barrier.wait(timeout=1);return state
    monkeypatch.setattr(runtime,'native_control_state',concurrent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:s.native_worker_control('worker'),range(2)))
    assert all(state['capabilities']['interject'] for state in results)
    assert counts=={'contexts':2,'ps':4}


@pytest.mark.parametrize('transition', ['new_run','new_attempt','ended'])
def test_concurrent_write_still_owns_lock_and_invalidates_old_read_snapshot(tmp_path,transition):
    entered=Event();release=Event();calls=[]
    def read(worker,**kwargs):
        entered.set();assert release.wait(1)
        return {**kwargs,'capabilities':{'interject':True}}
    runtime=SimpleNamespace(native_control_state=read)
    s,active=service(tmp_path,runtime)
    def write(worker,**kwargs):
        calls.append(kwargs)
        if transition=='new_run':active['run_id']='new-run'
        elif transition=='new_attempt':active['active_attempt_id']='new-attempt'
        else:active['state']='interrupted'
        return {'status':'queued'}
    runtime.native_control=write
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading=pool.submit(s.native_worker_control,'worker');assert entered.wait(1)
        try:
            assert s.native_worker_control('worker',run_id='run',attempt_id='attempt',action='interject')=={'status':'queued'}
        finally:release.set()
        with pytest.raises(ValueError,match='stale'):
            reading.result(timeout=1)
    assert len(calls)==1


@pytest.mark.parametrize('change', [
    {'run_id':'other'},{'attempt_id':'other'},{'capabilities':None},{'capabilities':[]}])
def test_wrong_native_identity_or_malformed_declared_capability_fails_closed(tmp_path,change):
    runtime=SimpleNamespace(native_control_state=lambda worker,**kw:{**kw,'capabilities':{'interject':True},**change})
    s,active=service(tmp_path,runtime)
    with pytest.raises(ValueError):s.native_worker_control('worker')


def test_expected_read_target_and_write_permission_fences_are_preserved(tmp_path):
    calls=[]
    runtime=SimpleNamespace(native_control_state=lambda worker,**kw:calls.append('read') or kw,
        native_control=lambda worker,**kw:calls.append('write') or {})
    s,active=service(tmp_path,runtime)
    with pytest.raises(ValueError,match='stale'):s.native_worker_control('worker',run_id='old',attempt_id='attempt')
    with pytest.raises(ValueError,match='stale'):s.native_worker_control('worker',run_id='run',attempt_id='old')
    def denied(_):raise RuntimeErrorBase('execution_denied')
    s._ensure_execution_allowed=denied
    with pytest.raises(RuntimeErrorBase,match='execution_denied'):
        s.native_worker_control('worker',run_id='run',attempt_id='attempt',action='cancel')
    assert calls==[]


def test_claude_state_and_supports_use_same_single_context_capability_owner(tmp_path,monkeypatch):
    runtime=HostClaudeCodeRuntime(str(tmp_path/'runtime'));seen=[]
    def context(*args,**kwargs):seen.append(kwargs);return {'nativeSessionId':'session'}
    monkeypatch.setattr(runtime,'_native_midstream_context',context)
    state=runtime.native_control_state({'worker_id':'worker'},run_id='run',attempt_id='attempt')
    assert state['capabilities']=={'interject':True} and len(seen)==1
    assert runtime.supports_native_control({'worker_id':'worker'},'cancel',run_id='run',attempt_id='attempt') is False


def test_grok_state_reuses_verified_active_receipt_without_extra_read(tmp_path,monkeypatch):
    runtime=HostGrokBuildRuntime(str(tmp_path/'runtime'));seen=[]
    active={'run_id':'run','attempt_id':'attempt','native_session_id':'session','native_control_capabilities':{'interject':True}}
    def read(_):seen.append(1);return dict(active)
    monkeypatch.setattr(runtime,'_read_active_session',read)
    monkeypatch.setattr(runtime,'_read_session_key',lambda _:'session')
    state=runtime.native_control_state({'worker_id':'worker'},run_id='run',attempt_id='attempt')
    assert len(seen)==1 and state['capabilities']=={'interject':True,'cancel':True,'permission':True}
    assert runtime.supports_native_control({'worker_id':'worker'},'interject',run_id='run',attempt_id='attempt') is True
    assert runtime.supports_native_control({'worker_id':'worker'},'cancel') is True
