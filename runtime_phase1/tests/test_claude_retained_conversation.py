"""Retained transport tests with a real local stream peer, no provider requests."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from workers_projects_runtime import claude_conversation as transport
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime, RuntimeErrorBase


PEER = r'''
import sys,json,os
from pathlib import Path
waiting=False
turn=0
def emit(v): print(json.dumps(v),flush=True)
for line in sys.stdin:
 e=json.loads(line)
 if e['type']=='control_request':
  r=e['request'];sub=r['subtype'];response={}
  if sub=='reload_output_styles':response={'available_output_styles':['xperfect-application']}
  if sub=='mcp_set_servers':
   Path('connections.json').write_text(json.dumps(r['servers']))
   response={'errors':{}}
  if sub=='interrupt' and waiting:
   waiting=False
   emit({'type':'result','session_id':'synthetic-session','result':'Interrupted','is_error':True,'terminal_reason':'aborted_streaming'})
  emit({'type':'control_response','response':{'subtype':'success','request_id':e['request_id'],'response':response}})
 elif e['type']=='user':
  turn+=1
  text=e['message']['content'][0]['text']
  if text=='wait':waiting=True;continue
  if text=='refuse':
   emit({'type':'assistant','error':'invalid_request'})
   emit({'type':'result','session_id':'synthetic-session','result':'','is_error':True,'terminal_reason':'failed'});continue
  authority=Path('.claude/output-styles/xperfect-application.md').read_text().split('---\n')[-1]
  emit({'type':'result','session_id':'synthetic-session','result':f'{authority} turn={turn} pid={os.getpid()}','is_error':False})
'''


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.delenv('VIVENTIUM_NATIVE_FIRST_ADMIN_STATE', raising=False)
    runtime=HostClaudeCodeRuntime(base_dir=str(tmp_path/'runtime'))
    runtime.set_run_start_observer(lambda _payload: None)
    monkeypatch.setattr(runtime,'_native_context_hooks_supported',lambda:True)
    peer=tmp_path/'peer.py';peer.write_text(PEER)
    workspace=tmp_path/'workspace';workspace.mkdir()
    worker={'worker_id':'synthetic-retained','profile':'claude-code','execution_mode':'host',
            'model':'claude-opus-5','workspace_root':str(workspace),
            '_active_run_id':'run-1','_run_attempt_id':'attempt-1',
            'trusted_run_lane':'conversation','bootstrap_bundle_json':json.dumps({
                'run_mode':'conversation','developer_instructions':'Current dry weather',
                'provider_capabilities':{'native_tools':False,'conversation_process_reuse':True}})}
    state=runtime._state_dir(worker['worker_id']);state.mkdir(parents=True)
    (state/'conversation-mcp.json').write_text('{"mcpServers":{}}')
    monkeypatch.setattr(runtime,'_assert_host_claude_mcp_config',lambda *_:None)
    command=[sys.executable,str(peer),'--settings','{}','--setting-sources','',
             '--setting-sources','','--no-session-persistence','--mcp-config',str(state/'conversation-mcp.json')]
    def invoke(text='hello',authority=None,timeout=5,extra_args=()):
        if authority:
            b=json.loads(worker['bootstrap_bundle_json']);b['developer_instructions']=authority
            worker['bootstrap_bundle_json']=json.dumps(b)
        root=runtime._run_root(worker['worker_id'],worker['_active_run_id']);root.mkdir(parents=True,exist_ok=True)
        transport.run_turn(runtime,worker,[*command,*extra_args],dict(os.environ),workspace,root,text,timeout,{})
        return json.loads((root/'stdout.log').read_text().splitlines()[-1])['result']
    try:yield runtime,worker,workspace,invoke
    finally:runtime.close_persistent_conversations()


def test_same_process_current_authority_history_and_idle_release(prepared):
    runtime,worker,workspace,invoke=prepared
    first=invoke();before=runtime._read_active_session(worker['worker_id'])
    assert before['phase']=='idle' and before['run_id']=='' and before['attempt_id']==''
    assert before['host_slot_token']=='' and before['process_start_identity']
    worker.update(_active_run_id='run-2',_run_attempt_id='attempt-2')
    second=invoke(authority='Current rainy weather')
    after=runtime._read_active_session(worker['worker_id'])
    assert before['process_pid']==after['process_pid']
    assert 'Current dry weather' in first and 'turn=1' in first
    assert 'Current rainy weather' in second and 'turn=2' in second
    assert json.loads((workspace/'connections.json').read_text())=={}
    assert runtime.host_active_process_status(worker)['retained_conversation_process'] is True
    assert not runtime.supports_native_control(worker,'cancel',run_id='run-1',attempt_id='attempt-1')


def test_current_interrupt_rejects_stale_attempt_and_next_turn_works(prepared):
    runtime,worker,workspace,invoke=prepared
    result=[]
    thread=threading.Thread(target=lambda:result.append(invoke('wait')));thread.start()
    deadline=time.monotonic()+5
    while not runtime.supports_native_control(worker,'cancel',run_id='run-1',attempt_id='attempt-1'):
        assert time.monotonic()<deadline
        time.sleep(.01)
    assert not runtime.supports_native_control(worker,'cancel',run_id='run-1',attempt_id='wrong')
    with pytest.raises(RuntimeErrorBase):
        runtime.native_control(worker,run_id='run-1',attempt_id='wrong',action='cancel')
    pid=runtime._read_active_session(worker['worker_id'])['process_pid']
    runtime.native_control(worker,run_id='run-1',attempt_id='attempt-1',action='cancel')
    thread.join(5);assert not thread.is_alive();assert result==['Interrupted']
    worker.update(_active_run_id='run-2',_run_attempt_id='attempt-2')
    assert 'turn=2' in invoke()
    assert runtime._read_active_session(worker['worker_id'])['process_pid']==pid


def test_idle_expiry_stops_exact_process(prepared,monkeypatch):
    runtime,worker,workspace,invoke=prepared
    monkeypatch.setenv('GLASSHIVE_CLAUDE_CONVERSATION_IDLE_SECONDS','.05')
    invoke()
    deadline=time.monotonic()+6
    while runtime._read_active_session(worker['worker_id']) is not None:
        assert time.monotonic()<deadline
        time.sleep(.02)
    assert not runtime._active_processes


def test_deadline_does_not_leave_native_process(prepared):
    runtime,worker,workspace,invoke=prepared
    with pytest.raises(RuntimeErrorBase,match='response deadline'):
        invoke('wait',timeout=.05)
    assert runtime._read_active_session(worker['worker_id']) is None


def test_phase_change_is_cas_visible(prepared):
    runtime,worker,workspace,invoke=prepared
    invoke();before=runtime._read_active_session(worker['worker_id'])
    assert runtime._write_active_session(worker['worker_id'],{**before,'phase':'preparing'},expected_session=before)
    assert not runtime._write_active_session(worker['worker_id'],before,expected_session=before)


def test_projection_has_one_source_no_builtin_tools_or_api_route():
    command=['claude','--tools','','--safe-mode','--settings','{}','--setting-sources','',
             '--setting-sources','','--no-session-persistence','--mcp-config','old',
             '--append-system-prompt-file','old']
    argv,settings=transport.command_projection(command,Path('.'))
    assert argv.count('--setting-sources')==1
    assert argv[argv.index('--setting-sources')+1]=='project'
    assert argv[argv.index('--tools')+1]==''
    assert '--safe-mode' not in argv and '--append-system-prompt-file' not in argv
    assert settings['disableAllHooks'] and settings['claudeMdExcludes']==['**']


@pytest.mark.parametrize('relative',['settings.json','settings.local.json','CLAUDE.md','output-styles/unadmitted.md'])
def test_unadmitted_project_configuration_rejected(tmp_path,relative):
    target=tmp_path/'.claude'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('{}')
    with pytest.raises(ValueError,match='Unexpected native project configuration'):
        transport.assert_private_style_tree(tmp_path)


def test_current_bearer_not_retained_and_missing_grant_rejected():
    env={'PATH':'/bin','GLASSHIVE_CAPABILITY_BROKER_TOKEN':'new-turn-token'}
    scope,retained=transport.process_scope(['claude'],env)
    assert 'GLASSHIVE_CAPABILITY_BROKER_TOKEN' not in retained
    payload={'mcpServers':{'broker':{'type':'http','url':'http://127.0.0.1',
             'headers':{'Authorization':'Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}'}}}}
    assert transport.native_servers(payload,env)['broker']['headers']['Authorization']=='Bearer new-turn-token'
    with pytest.raises(ValueError):transport.native_servers(payload,{})
    assert scope==transport.process_scope(['claude'],{**env,'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'next-token'})[0]


def test_turn_arriving_during_idle_expiry_relaunches(prepared,monkeypatch):
    runtime,worker,workspace,invoke=prepared
    monkeypatch.setenv('GLASSHIVE_CLAUDE_CONVERSATION_IDLE_SECONDS','.05')
    original=runtime._stop_active_process
    entered=threading.Event();release=threading.Event()
    def stop(worker_id,**kwargs):
        entered.set();assert release.wait(5)
        return original(worker_id,**kwargs)
    monkeypatch.setattr(runtime,'_stop_active_process',stop)
    invoke();assert entered.wait(3)
    first=runtime._read_active_session(worker['worker_id'])['process_pid']
    worker.update(_active_run_id='run-2',_run_attempt_id='attempt-2')
    result=[];thread=threading.Thread(target=lambda:result.append(invoke()));thread.start()
    release.set();thread.join(5)
    assert not thread.is_alive() and result
    assert runtime._read_active_session(worker['worker_id'])['process_pid']!=first


def test_unsupported_cli_refuses_before_invocation(prepared,monkeypatch):
    runtime,worker,workspace,invoke=prepared
    monkeypatch.setattr(runtime,'_native_context_hooks_supported',lambda:False)
    with pytest.raises(RuntimeErrorBase,match='protocol checks'):invoke()
    assert not runtime._active_processes


def test_expired_naive_utc_deadline_refuses_before_invocation(prepared):
    runtime,worker,workspace,invoke=prepared
    worker['_provider_response_deadline_at']='2000-01-01T00:00:00'
    with pytest.raises(RuntimeErrorBase,match='response deadline'):invoke()
    assert not runtime._active_processes


def test_dead_process_between_turns_reconciles_exact_generation(prepared):
    runtime,worker,workspace,invoke=prepared
    invoke();first=runtime._read_active_session(worker['worker_id'])['process_pid']
    process=runtime._active_processes[worker['worker_id']]
    process.terminate();process.wait(timeout=5)
    worker.update(_active_run_id='run-2',_run_attempt_id='attempt-2')
    assert 'turn=1' in invoke()
    assert runtime._read_active_session(worker['worker_id'])['process_pid']!=first


def test_live_scope_change_stops_and_relaunches(prepared):
    runtime,worker,workspace,invoke=prepared
    invoke();first=runtime._read_active_session(worker['worker_id'])['process_pid']
    worker.update(_active_run_id='run-2',_run_attempt_id='attempt-2')
    assert 'turn=1' in invoke(extra_args=('--json-schema','{"type":"object"}'))
    assert runtime._read_active_session(worker['worker_id'])['process_pid']!=first


def test_disabling_reuse_releases_cached_process_before_cold_launch(prepared):
    runtime,worker,workspace,invoke=prepared
    invoke()
    transport.release_cached_conversation(runtime,worker['worker_id'])
    assert runtime._read_active_session(worker['worker_id']) is None
    assert not runtime._active_processes


def test_preflight_failure_after_idle_has_confirmed_cleanup(prepared,monkeypatch):
    runtime,worker,workspace,invoke=prepared
    invoke();worker.update(_active_run_id='run-2',_run_attempt_id='attempt-2')
    def reject(*_):raise RuntimeErrorBase('Fresh grant unavailable')
    monkeypatch.setattr(runtime,'_assert_host_claude_mcp_config',reject)
    with pytest.raises(RuntimeErrorBase,match='Fresh grant unavailable'):invoke()
    assert runtime._read_active_session(worker['worker_id']) is None
    assert not runtime._active_processes


def test_refusal_drains_reports_failure_and_keeps_idle_transport(prepared):
    runtime,worker,workspace,invoke=prepared
    invoke();first=runtime._read_active_session(worker['worker_id'])['process_pid']
    worker.update(_active_run_id='run-2',_run_attempt_id='attempt-2')
    assert invoke('refuse')==''
    root=runtime._run_root(worker['worker_id'],'run-2')
    assert (root/'exit_code').read_text()=='1'
    assert runtime._read_active_session(worker['worker_id'])['phase']=='idle'
    worker.update(_active_run_id='run-3',_run_attempt_id='attempt-3')
    assert 'turn=3' in invoke()
    assert runtime._read_active_session(worker['worker_id'])['process_pid']==first


@pytest.mark.parametrize('foreground_origin', [{'actor_kind':'external_user','origin':'interactive'}, {'actor_kind':'system','origin':'callback'}])
@pytest.mark.parametrize('actor,origin', [('system','system'),('assistant','worker'),('worker','scheduler')])
def test_background_invocations_do_not_take_foreground_capacity(prepared,monkeypatch,actor,origin,foreground_origin):
    runtime,user,workspace,invoke=prepared
    monkeypatch.setenv('WPR_HOST_CONVERSATION_SLOTS_PER_CLI','1')
    monkeypatch.setenv('WPR_HOST_MISSION_SLOTS_PER_CLI','1')
    bundle=json.loads(user['bootstrap_bundle_json'])
    bundle['conversation_origin']={'actor_kind':actor,'origin':origin}
    background={**user,'worker_id':'synthetic-background','_glasshive_conversation_run':True,
                'bootstrap_bundle_json':json.dumps(bundle)}
    bundle={**bundle,'conversation_origin':foreground_origin}
    foreground={**user,'worker_id':'synthetic-foreground','_glasshive_conversation_run':True,
                'bootstrap_bundle_json':json.dumps(bundle)}
    token=runtime._acquire_host_slot(background)
    try:
        assert runtime._host_capacity_lane(background)=='mission'
        assert runtime._host_capacity_lane(foreground)=='conversation'
        assert runtime.worker_capacity_error(foreground) is None
        assert runtime.worker_capacity_error({**background,'worker_id':'another-background'}) is not None
    finally:runtime._release_host_slot(background['worker_id'],expected_slot_token=token)


@pytest.mark.parametrize('declared_origin', [None, {'actor_kind':'external_user','origin':'interactive'}])
def test_foreground_capacity_queues_at_existing_limit_and_recovers(prepared,monkeypatch,declared_origin):
    runtime,user,workspace,invoke=prepared
    monkeypatch.setenv('WPR_HOST_CONVERSATION_SLOTS_PER_CLI','1')
    bundle=json.loads(user['bootstrap_bundle_json'])
    bundle.pop('conversation_origin',None)
    if declared_origin is not None:
        bundle['conversation_origin']=declared_origin
    first={**user,'worker_id':'first-foreground','_glasshive_conversation_run':True,
           'bootstrap_bundle_json':json.dumps(bundle)}
    second={**first,'worker_id':'second-foreground'}
    assert runtime._host_capacity_lane(first)=='conversation'
    token=runtime._acquire_host_slot(first)
    try:
        error=runtime.worker_capacity_error(second)
        assert error is not None
        assert error.capacity_class=='family_lane'
    finally:
        runtime._release_host_slot(first['worker_id'],expected_slot_token=token)
    assert runtime.worker_capacity_error(second) is None
