from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workers_projects_runtime.grok_runtime import GrokBuildRuntime, HostGrokBuildRuntime
from workers_projects_runtime.coordinator import CoordinatorScopeError
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo, ProviderAuthenticationMissingError
from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime, HostNativeCliMixin


def worker(tmp_path):
    return {'worker_id':'worker-grok', 'owner_id':'local', 'profile':'grok-build',
            'model':'grok-selected', 'workspace_dir':str(tmp_path / 'workspace'),
            'bootstrap_bundle_json':'{}', 'execution_mode':'host'}


def info(tmp_path, session=None):
    return RuntimeInfo('grok-build','grok-selected','',None,None,session,str(tmp_path/'state'),str(tmp_path/'workspace'),None)


def test_adapters_reuse_existing_exact_generation_lifecycle():
    assert HostGrokBuildRuntime.run_task is HostNativeCliMixin.run_task
    assert HostGrokBuildRuntime.interrupt_worker is HostNativeCliMixin.interrupt_worker
    assert HostGrokBuildRuntime.terminate_worker is HostNativeCliMixin.terminate_worker
    assert GrokBuildRuntime.run_task is BaseCliWorkerRuntime.run_task
    assert GrokBuildRuntime.interrupt_worker is BaseCliWorkerRuntime.interrupt_worker


def test_command_preserves_exact_model_and_resume_without_instruction_in_argv(tmp_path):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    command = runtime._grok_command(worker(tmp_path), info(tmp_path,'native-id'), host=True)
    assert command[command.index('--model')+1] == 'grok-selected'
    assert command[command.index('--session-id')+1] == 'native-id'
    assert Path(command[1]).is_file()
    assert command[1].startswith(str(runtime._home_dir('worker-grok')))
    assert str(tmp_path / 'workspace') not in command


def test_only_run_bound_coordinator_projection_can_preapprove_its_own_tools(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    monkeypatch.setattr(runtime, '_native_environment', lambda _worker, host: {
        'GLASSHIVE_PEER_TOKEN': 'synthetic-token'
    })
    record = worker(tmp_path)
    record['_active_run_id'] = 'run-1'
    record['_coordinator_native_projection'] = {
        'worker_id': record['worker_id'], 'run_id': 'run-1',
        'token': 'synthetic-token', 'url': 'https://example.test/coordinator',
    }
    command = runtime._grok_command(record, info(tmp_path), host=True)
    assert 'xperfect-coordinator__coordinator_accept_goals' in command
    assert 'synthetic-token' not in command
    stale = {**record, '_active_run_id': 'run-2'}
    with pytest.raises(CoordinatorScopeError, match='Coordinator native projection mismatch'):
        runtime._grok_command(stale, info(tmp_path), host=True)
    plain = worker(tmp_path)
    assert '--allow-mcp-tool' not in runtime._grok_command(plain, info(tmp_path), host=True)


def test_no_model_default_or_unknown_profile_fallback(tmp_path, monkeypatch):
    runtime = GrokBuildRuntime(str(tmp_path))
    monkeypatch.delenv('WPR_MODEL_GROK_BUILD', raising=False)
    with pytest.raises(RuntimeErrorBase, match='exact Grok model'):
        runtime.resolve_model('grok-build')
    with pytest.raises(RuntimeErrorBase, match='Unsupported profile'):
        runtime.resolve_model('unknown')


def test_native_auth_missing_is_distinct_and_private_home_cannot_be_overridden(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    record = worker(tmp_path)
    monkeypatch.setattr(runtime, '_host_env', lambda _: {'GROK_HOME':'/other-worker', 'OTEL_EXPORTER_OTLP_ENDPOINT':'https://example.invalid'})
    with pytest.raises(ProviderAuthenticationMissingError):
        runtime._native_environment(record, host=True)
    native_home = runtime._grok_home(record)
    (native_home/'auth.json').write_text('{}')
    env = runtime._native_environment(record, host=True)
    assert env['GROK_HOME'] == str(native_home)
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT' not in env
    assert native_home.stat().st_mode & 0o777 == 0o700


def test_parser_rejects_mismatched_terminal_session(tmp_path):
    runtime = GrokBuildRuntime(str(tmp_path))
    events = [{'type':'grok.session.started','session_id':'native-id','model':'grok-selected'},
              {'type':'grok.result','session_id':'wrong','stop_reason':'end_turn','output':'not accepted'}]
    with pytest.raises(RuntimeErrorBase, match='native session/model'):
        runtime._parse_output(worker(tmp_path), '\n'.join(map(json.dumps,events)), '', info(tmp_path))


def test_runner_subprocess_preserves_native_protocol_and_resumes(tmp_path):
    # This peer is a protocol fixture, not evidence of provider or account parity.
    fake = tmp_path/'grok-fixture'
    fake.write_text('''#!'''+sys.executable+'''
import json,sys
assert sys.argv[1:]==['agent','--no-leader','--model','grok-selected','stdio']
for line in sys.stdin:
 r=json.loads(line); method=r['method']
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'agentVersion':'fixture','defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate': result={}
 elif method=='session/load':
  assert r['params']['sessionId']=='native-id'
  result={'sessionId':'native-id','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  assert r['params']['prompt']==[{'type':'text','text':'exact task'}]
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-id','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'answer'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 else: raise Exception(method)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    runtime = HostGrokBuildRuntime(str(tmp_path/'state'))
    runtime.binary = str(fake)
    command = runtime._grok_command(worker(tmp_path), info(tmp_path,'native-id'), host=True)
    result = subprocess.run(command, input='exact task', capture_output=True, text=True,
                            env={**os.environ,'GROK_HOME':str(tmp_path/'private-grok')}, timeout=5)
    assert result.returncode == 0, result.stderr
    assert runtime._parse_output(worker(tmp_path),result.stdout,result.stderr,info(tmp_path,'native-id')) == ('native-id','answer')


def test_host_adapter_runs_inside_real_supervisor_and_persists_exact_session(tmp_path, monkeypatch):
    fake = tmp_path/'native-fixture'
    fake.write_text('''#!'''+sys.executable+'''
import json,sys
for line in sys.stdin:
 r=json.loads(line);method=r['method']
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate': result={}
 elif method in ('session/new','session/load'): result={'sessionId':'native-supervised','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-supervised','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Completed the requested synthetic task.'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 else: raise Exception(method)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    runtime=HostGrokBuildRuntime(str(tmp_path/'data'))
    runtime.binary=str(fake)
    record={**worker(tmp_path),'_run_attempt_id':'attempt-1','workspace_root':str(tmp_path/'workspace-root')}
    home=runtime._grok_home(record)
    home.mkdir(parents=True)
    (home/'auth.json').write_text('{}')
    output=runtime.run_task(record,'Complete the synthetic task.',timeout_sec=10,run_id='run-real-supervisor')
    assert output == 'Completed the requested synthetic task.'
    assert runtime._read_session_key(record['worker_id']) == 'native-supervised'
    assert not runtime._active_pid(record['worker_id'])
    run_root=runtime._run_root(record['worker_id'],'run-real-supervisor')
    assert (run_root/'exit_code').read_text().strip()=='0'
    assert 'grok.session.started' in (run_root/'stdout.log').read_text()


def test_container_command_does_not_reuse_host_binary_path(tmp_path, monkeypatch):
    runtime=GrokBuildRuntime(str(tmp_path/'data'))
    runtime.binary='/host-only/grok'
    monkeypatch.delenv('WPR_GROK_CONTAINER_BIN',raising=False)
    command=runtime._grok_command(worker(tmp_path),info(tmp_path),host=False)
    assert command[command.index('--binary')+1]=='grok'
    assert '/host-only/grok' not in command


def test_reviewed_artifact_mismatch_fails_before_native_launch(tmp_path, monkeypatch):
    runtime=HostGrokBuildRuntime(str(tmp_path/'data'))
    runtime.binary=sys.executable
    monkeypatch.setenv('WPR_GROK_REVIEWED_BINARY_SHA256','0'*64)
    command=runtime._grok_command(worker(tmp_path),info(tmp_path),host=True)
    result=subprocess.run(command,input='task',text=True,capture_output=True,
                          env={**os.environ,'GROK_HOME':str(tmp_path/'private-grok')},timeout=5)
    assert result.returncode==2
    assert 'does not match' in result.stderr
    assert not result.stdout


def test_docker_launch_preserves_private_grok_selectors_and_discovery_policy():
    from workers_projects_runtime.docker_sandbox import _safe_docker_exec_env
    expected = {
        "GROK_HOME": "/workspace/member/home/.grok",
        "GROK_AUTH_PATH": "/workspace/member/home/.grok/auth.json",
        "GROK_DISABLE_AUTOUPDATER": "1",
        "GROK_TELEMETRY_ENABLED": "false",
        "GROK_TELEMETRY_TRACE_UPLOAD": "false",
        "GROK_EXTERNAL_OTEL": "0",
        "GROK_CURSOR_MCPS_ENABLED": "false",
        "GROK_CLAUDE_MCPS_ENABLED": "false",
    }
    assert _safe_docker_exec_env({**expected, "UNRELATED_SECRET": "synthetic", "LD_PRELOAD": "untrusted"}) == expected


def test_runner_preserves_managed_home_acl(tmp_path, monkeypatch):
    from workers_projects_runtime import grok_acp_runner as runner
    home = tmp_path / "grok-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GROK_HOME", str(home))
    original = Path.chmod
    def deny_home_chmod(path, *args, **kwargs):
        if path == home:
            raise PermissionError("service-owned ACL")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "chmod", deny_home_chmod)
    monkeypatch.setattr(runner, "run", lambda args: 0)
    assert runner.main(["--binary", "grok", "--model", "synthetic", "--managed-home"]) == 0
    with pytest.raises(PermissionError):
        runner.main(["--binary", "grok", "--model", "synthetic"])


PERMISSION_FIXTURE = '''#!PYTHON
import json,sys
for line in sys.stdin:
 r=json.loads(line); method=r.get('method')
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'agentVersion':'fixture','defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate': result={}
 elif method=='session/new': result={'sessionId':'native-id','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','id':'perm-1','method':'session/request_permission','params':{'sessionId':'native-id','toolCall':{'title':'Run a command'},'options':[{'optionId':'allow','kind':'allow_once','name':'Allow'},{'optionId':'reject','kind':'reject_once','name':'No'}]}}),flush=True)
  reply={}
  while reply.get('id')!='perm-1': reply=json.loads(sys.stdin.readline())
  selected=reply['result']['outcome'].get('optionId')=='allow'
  if selected: print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-id','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'done'}}}}),flush=True)
  result={'stopReason':'end_turn' if selected else 'cancelled'}
 else: continue
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
'''


@pytest.mark.parametrize('answer,code,failure_class,message', [
    (None, 2, 'native_input_expired',
     'Grok stopped because its request for your response expired after 0.4 seconds without an answer.'),
    ({}, 2, 'native_input_cancelled', 'Grok stopped because you cancelled its request.'),
    ({'action':'cancel'}, 2, 'native_turn_cancelled', 'Grok stopped because its turn was cancelled.'),
    ({'option_id':'reject'}, 2, 'native_input_declined', 'Grok stopped after you declined its request.'),
    ({'option_id':'allow'}, 0, None, None),
])
def test_runner_reports_why_a_native_request_ended_the_turn(tmp_path, monkeypatch, capfd, answer, code, failure_class, message):
    import functools, io, threading, time
    from workers_projects_runtime import grok_acp_runner as runner
    from workers_projects_runtime.grok_control import submit_control
    fake = tmp_path/'grok-fixture'
    fake.write_text(PERMISSION_FIXTURE.replace('PYTHON', sys.executable))
    fake.chmod(0o700)
    control = tmp_path/'control'
    monkeypatch.setenv('GROK_HOME', str(tmp_path/'home'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('task'))
    monkeypatch.setattr(runner, 'ControlMailbox', functools.partial(runner.ControlMailbox, permission_timeout=.4))
    def owner():
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            pending = list(control.glob('*.pending')) if control.is_dir() else []
            if pending:
                request = json.loads(pending[0].read_text())
                body = {'run_id':'run-1','attempt_id':'attempt-1','session_id':'native-id'}
                body.update({'action':'permission','request_id':request['request_id']})
                body.update(answer)
                submit_control(control, body)
                return
            time.sleep(.02)
    thread = threading.Thread(target=owner) if answer is not None else None
    if thread: thread.start()
    returned = runner.main(['--binary', str(fake), '--model', 'grok-selected', '--control-dir', str(control),
                            '--run-id', 'run-1', '--attempt-id', 'attempt-1'])
    if thread: thread.join(5)
    captured = capfd.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line.startswith('{')]
    assert returned == code, captured.err
    if message is None:
        assert events[-1]['type'] == 'grok.result' and events[-1]['output'] == 'done'
        return
    error = events[-1]
    assert (error['type'], error['failure_class'], error['message']) == ('grok.error', failure_class, message)
    assert captured.err.strip().splitlines()[-1] == message


@pytest.mark.parametrize('failure_class,message', [
    ('native_input_expired', 'Grok stopped because its request for your response expired without an answer.'),
    ('native_input_declined', 'Grok stopped after you declined its request.'),
    ('native_input_cancelled', 'Grok stopped because you cancelled its request.'),
    ('native_turn_cancelled', 'Grok stopped because its turn was cancelled.'),
])
def test_typed_native_stop_is_the_run_failure_truth(failure_class, message):
    from workers_projects_runtime.failure_classification import classify_cli_failure
    stdout = '\n'.join(json.dumps(event) for event in (
        {'type':'grok.session.started','session_id':'native-id','model':'grok-selected'},
        {'type':'grok.error','session_id':'native-id','code':None,'failure_class':failure_class,'message':'runner text'},
    ))
    result = classify_cli_failure(stdout=stdout, stderr='runner text', runtime_name='grok-build', exit_code=2)
    assert (result.failure_class, result.user_message, result.retryable, result.structured) == (failure_class, message, False, True)
    assert 'new instruction' in result.recommended_recovery
    # Only the Grok runner's typed event counts: another runtime or an unknown class stays unclassified.
    assert classify_cli_failure(stdout=stdout, stderr='x', runtime_name='codex-cli', exit_code=2).failure_class != failure_class
    other = stdout.replace(failure_class, 'native_something_else')
    assert classify_cli_failure(stdout=other, stderr='x', runtime_name='grok-build', exit_code=2).failure_class == 'unknown'
