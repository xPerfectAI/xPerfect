from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from workers_projects_runtime import native_input


def fixture(root):
    root.mkdir(exist_ok=True)
    context={'version':1,'workerId':'wrk_input','runId':'run_input','contextToken':'context-a'}
    native_input.publish(root/'native-input-context.json',context)
    item={'version':1,'requestId':'request-a','requestFingerprint':'a'*64,'kind':'elicitation','mcpServerName':'fixture','message':'Choose a value.','mode':'form','requestedSchema':{'type':'object','properties':{'value':{'type':'number'}},'required':['value']},'state':'pending','contextToken':'context-a','createdAt':1}
    native_input.publish(native_input.request_path(root,'request-a'),item)
    return item


def response(root, **changes):
    return native_input.respond(root,worker_id='wrk_input',run_id='run_input',request_id='request-a',request_fingerprint='a'*64,action='accept',content={'value':1},**changes)


def test_same_reply_concurrently_publishes_one_complete_record(tmp_path):
    fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(lambda _:response(tmp_path),range(24)))
    assert sum(r['status']=='accepted' for r in results)==1
    assert sum(r['status']=='already_accepted' for r in results)==23
    saved=native_input.read_response(native_input.request_path(tmp_path,'request-a').with_suffix('.response.json'))
    assert saved['response']=={'action':'accept','content':{'value':1}}
    assert not list(tmp_path.glob('.native-input-response-*'))


@pytest.mark.parametrize('change',['different_reply','foreign_run','wrong_fingerprint','cancelled','invalid_form','symlink'])
def test_input_boundaries(tmp_path,change):
    item=fixture(tmp_path)
    args=dict(worker_id='wrk_input',run_id='run_input',request_id='request-a',request_fingerprint='a'*64,action='accept',content={'value':1})
    if change=='different_reply':
        response(tmp_path);args['action']='decline'
    elif change=='foreign_run':args['run_id']='run_other'
    elif change=='wrong_fingerprint':args['request_fingerprint']='b'*64
    elif change=='cancelled':native_input.publish(native_input.request_path(tmp_path,'request-a'),{**item,'state':'cancelled'})
    elif change=='invalid_form':args['content']={'value':'not a number'}
    else:
        path=native_input.request_path(tmp_path,'request-a');path.rename(tmp_path/'target.json');path.symlink_to(tmp_path/'target.json')
    with pytest.raises(native_input.NativeInputError):native_input.respond(tmp_path,**args)


def test_relay_keeps_input_open_and_preserves_final_result(tmp_path):
    context={'version':1,'workerId':'wrk_input','runId':'run_input','contextToken':'context-a'}
    native_input.publish(tmp_path/'native-input-context.json',context)
    child=tmp_path/'child.py'
    child.write_text('''import json,sys
initialize=json.loads(sys.stdin.readline());user=json.loads(sys.stdin.readline())
assert user['message']['content']=='Original task bytes.\\n'
print(json.dumps({'type':'control_request','request_id':'native-a','request':{'subtype':'elicitation','mcp_server_name':'fixture','message':'Allow synthetic input?','mode':'form','requested_schema':{'type':'object','properties':{}}}}),flush=True)
reply=json.loads(sys.stdin.readline())
assert reply['response']['response']['action']=='decline'
print(json.dumps({'type':'result','subtype':'success','result':'The native decline was preserved.','session_id':'retained-native-session'}),flush=True)
assert sys.stdin.read()==''
''')
    p=subprocess.Popen([sys.executable,native_input.__file__,str(tmp_path/'native-input-context.json'),sys.executable,str(child)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    p.stdin.write('Original task bytes.\n');p.stdin.close()
    deadline=time.monotonic()+5;pending=None
    while time.monotonic()<deadline:
        pending=native_input.pending(tmp_path,worker_id='wrk_input',run_id='run_input')
        if pending:break
        time.sleep(.01)
    assert pending and pending['state']=='pending'
    assert p.poll() is None
    native_input.respond(tmp_path,worker_id='wrk_input',run_id='run_input',request_id=pending['requestId'],request_fingerprint=pending['requestFingerprint'],action='decline')
    p.wait(timeout=5);output=p.stdout.read();error=p.stderr.read()
    assert p.returncode==0,error
    assert json.loads(output.splitlines()[-1])['result']=='The native decline was preserved.'
    assert native_input.pending(tmp_path,worker_id='wrk_input',run_id='run_input') is None


@pytest.mark.parametrize('schema,value', [
    ({'type':'string','format':'date'}, '2026-99-99'),
    ({'type':'string','format':'date-time'}, 'tomorrow'),
    ({'type':'string','format':'email'}, 'invalid'),
    ({'type':'string','format':'uri'}, 'not a uri'),
    ({'type':'array','items':{'enum':['a','b']},'minItems':1}, []),
    ({'type':'array','items':{'enum':['a','b']},'maxItems':1}, ['a','b']),
    ({'type':'array','items':{'enum':['a','b']}}, ['c']),
])
def test_invalid_native_form_stays_editable_without_consuming_request(tmp_path,schema,value):
    item=fixture(tmp_path)
    item['requestedSchema']['properties']['value']=schema
    native_input.publish(native_input.request_path(tmp_path,'request-a'),item)
    with pytest.raises(native_input.NativeInputError,match='native_input_invalid'):
        native_input.respond(tmp_path,worker_id='wrk_input',run_id='run_input',request_id='request-a',request_fingerprint='a'*64,action='accept',content={'value':value})
    assert not native_input.request_path(tmp_path,'request-a').with_suffix('.response.json').exists()
    assert native_input.pending(tmp_path,worker_id='wrk_input',run_id='run_input')['state']=='pending'


def test_replay_recovers_completed_publication_after_writer_loss(tmp_path):
    fixture(tmp_path)
    response(tmp_path)
    path=native_input.request_path(tmp_path,'request-a').with_suffix('.response.json')
    os.link(path,tmp_path/'.native-input-response-abandoned')
    assert response(tmp_path,allow_new=False)['status']=='already_accepted'
    assert path.stat().st_nlink==1
    assert not (tmp_path/'.native-input-response-abandoned').exists()


@pytest.mark.parametrize('failure', ['invalid_json', 'non_object', 'open_mode'])
def test_invalid_single_link_response_fails_without_retry(tmp_path, failure):
    path = tmp_path / 'native-input-request.response.json'
    if failure == 'invalid_json':
        path.write_text('{')
        path.chmod(0o600)
    elif failure == 'non_object':
        path.write_text('[]')
        path.chmod(0o600)
    else:
        path.write_text('{}')
        path.chmod(0o644)
    with pytest.raises((native_input.NativeInputError, json.JSONDecodeError)):
        native_input.read_response(path)


def test_dead_native_process_cannot_receive_a_new_response(tmp_path):
    fixture(tmp_path)
    with pytest.raises(native_input.NativeInputError,match='native_input_stale'):
        response(tmp_path,allow_new=False)
    assert not native_input.request_path(tmp_path,'request-a').with_suffix('.response.json').exists()


@pytest.mark.parametrize('ending',['timeout','stop'])
def test_existing_supervisor_ends_pending_native_process_group(tmp_path,ending):
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime, RuntimeErrorBase
    runtime=HostClaudeCodeRuntime(base_dir=str(tmp_path/'state'))
    worker={'worker_id':'wrk_input','profile':'claude-code','execution_mode':'host'}
    run_id='run_input';root=runtime._run_root(worker['worker_id'],run_id);root.mkdir(parents=True)
    child=root/'fixture.py';pid_path=root/'child.pid'
    child.write_text("import json,os,pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\njson.loads(sys.stdin.readline());json.loads(sys.stdin.readline())\nprint(json.dumps({'type':'control_request','request_id':'native-a','request':{'subtype':'elicitation','mcp_server_name':'fixture','message':'Wait for owner.','mode':'form','requested_schema':{'type':'object','properties':{}}}}),flush=True)\nsys.stdin.readline()\ntime.sleep(60)\n")
    instruction=root/'instruction.stdin';instruction.write_text('Synthetic native task.');instruction.chmod(0o600)
    exit_path=root/'exit_code'
    command=runtime._durable_host_process_command([sys.executable,str(child),str(pid_path)],run_root=root,exit_path=exit_path,stdin_path=instruction,timeout_sec=1 if ending=='timeout' else 30,native_input_context={'version':1,'workerId':worker['worker_id'],'runId':run_id,'contextToken':'context-a'})
    proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        runtime._wait_for_durable_host_supervisor(proc,run_root=root)
        runtime._write_active_session(worker['worker_id'],{'run_id':run_id,'session_name':'task-input','process_pid':proc.pid,'exit_path':str(exit_path),'stdout_path':str(root/'stdout.log'),'stderr_path':str(root/'stderr.log'),'run_mode':'mission'})
        runtime._release_durable_host_process(runtime._read_active_session(worker['worker_id']))
        deadline=time.monotonic()+3;waiting=None
        while time.monotonic()<deadline:
            waiting=runtime.pending_native_input(worker,run_id=run_id)
            if waiting:break
            time.sleep(.01)
        assert waiting and proc.poll() is None
        assert runtime.pending_native_input(worker,run_id='run_other') is None
        if ending=='stop':runtime._stop_active_process(worker['worker_id'],worker=worker,run_id=run_id)
        proc.wait(timeout=5)
        assert exit_path.read_text().strip()==('124' if ending=='timeout' else '143')
        assert runtime.pending_native_input(worker,run_id=run_id) is None
        assert native_input.pending(root,worker_id=worker['worker_id'],run_id=run_id) is None
        with pytest.raises(RuntimeErrorBase,match='native_input_stale'):
            runtime.respond_native_input(worker,run_id=run_id,request_id=waiting['requestId'],request_fingerprint=waiting['requestFingerprint'],action='accept',content={})
        with pytest.raises(ProcessLookupError):os.kill(int(pid_path.read_text()),0)
    finally:
        if proc.poll() is None:
            import signal
            os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=5)


@pytest.mark.parametrize('schema,value', [
    ({'type':'string','format':'date'}, '2026-09-06'),
    ({'type':'string','format':'date-time'}, '2026-09-06T12:30:00Z'),
    ({'type':'string','format':'email'}, 'qa@example.test'),
    ({'type':'string','format':'uri'}, 'https://example.test/resource'),
    ({'type':'array','items':{'enum':['a','b']},'minItems':1,'maxItems':2}, ['a']),
])
def test_supported_native_form_values_are_accepted(tmp_path,schema,value):
    item=fixture(tmp_path);item['requestedSchema']['properties']['value']=schema
    native_input.publish(native_input.request_path(tmp_path,'request-a'),item)
    assert native_input.respond(tmp_path,worker_id='wrk_input',run_id='run_input',request_id='request-a',request_fingerprint='a'*64,action='accept',content={'value':value})['status']=='accepted'


def test_native_stream_result_and_usage_ignore_control_frames(tmp_path):
    from types import SimpleNamespace
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime
    runtime=HostClaudeCodeRuntime(base_dir=str(tmp_path/'runtime'))
    lines=[{'type':'control_response','response':{'subtype':'success'}},{'type':'result','result':'The useful retained answer.','session_id':'same-session','usage':{'input_tokens':10,'output_tokens':8}},{'type':'system','subtype':'late_notification'}]
    output='\n'.join(json.dumps(line) for line in lines)
    assert runtime._parse_output({},output,'',SimpleNamespace(session_key='old'))==('same-session','The useful retained answer.')
    assert runtime._usage_from_output(output)['output_tokens']==8
    lines[1]['result']=''
    assert runtime._parse_output({},'\n'.join(json.dumps(line) for line in lines),'',SimpleNamespace(session_key='old'))==('same-session','')

    assert runtime._parse_output({},json.dumps(lines[0]),'',SimpleNamespace(session_key='old'))==('old','')
