"""Exact native-turn input preserves current Codex policy and reader consumers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from workers_projects_runtime import native_input as owner
from workers_projects_runtime.grok_control import submit_control
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, HostClaudeCodeRuntime
from workers_projects_runtime.agent_builder_control import conversation_output_schema
from workers_projects_runtime.conversation_provider import _native_tool_evidence


def launch(tmp_path, *, resume=False):
    schema = tmp_path / 'output.json'
    schema.write_text('{"type":"object"}')
    command = ['codex', 'exec', *(['resume'] if resume else []), '--json', '--skip-git-repo-check',
               '-c', 'model="synthetic-model"', '-c', 'model_reasoning_effort="high"',
               '-c', 'developer_instructions="Stable authority"', '--enable', 'code_mode_host',
               '-c', 'approval_policy="never"', '-c', 'sandbox_mode="workspace-write"',
               '-c', 'sandbox_workspace_write.network_access=false', '--output-schema', str(schema),
               '--image', str(tmp_path / 'image.png'), *(['session-synthetic'] if resume else ['-C', str(tmp_path), '--add-dir', str(tmp_path / 'files')]), '-']
    return owner.codex_app_launch(command), command


@pytest.mark.parametrize('resume', [False, True])
def test_launch_keeps_exact_model_effort_authority_features_policy_images_and_schema(tmp_path, resume):
    got, command = launch(tmp_path, resume=resume)
    assert got['command'][:3] == ['codex', 'app-server', '--stdio']
    assert 'developer_instructions="Stable authority"' in got['command']
    assert got['thread']['model'] == 'synthetic-model'
    assert got['thread']['approvalPolicy'] == 'never'
    assert got['thread']['sandbox'] == 'workspace-write'
    assert got['turn'] == {'effort': 'high', 'outputSchema': {'type': 'object'}}
    assert got['images'] == [{'type': 'localImage', 'path': str(tmp_path / 'image.png')}]
    assert got['workspace_policy'] == {'network_access': False, **({} if resume else {'writable_roots': [str(tmp_path / 'files')]})}
    assert got['resumed'] is resume
    if resume:
        assert got['thread']['threadId'] == 'session-synthetic'
        assert got['thread']['excludeTurns'] is True
        assert 'experimentalRawEvents' not in got['thread']
    else:
        assert got['thread']['runtimeWorkspaceRoots'] == [str(tmp_path), str(tmp_path / 'files')]


@pytest.mark.parametrize('flag', ['--ignore-rules', '--unknown-policy', '--full-auto'])
def test_unmapped_native_control_preserves_legacy_exec(tmp_path, flag):
    _, command = launch(tmp_path)
    assert owner.codex_app_launch([*command[:-1], flag, '-']) is None


def test_approval_routes_without_client_handler_keep_legacy_exec():
    assert owner.codex_app_launch(['codex', 'exec', '-c', 'approval_policy="on-request"', '-']) is None


@pytest.mark.parametrize('matches', [True, False])
def test_graph_input_capability_requires_actual_native_schema_match(tmp_path, matches):
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    schema = conversation_output_schema(graph, None)
    schema_path = tmp_path / 'schema.json'
    schema_path.write_text(json.dumps(schema if matches else {'type': 'string'}))
    runtime = object.__new__(HostCodexCliRuntime)
    runtime._native_midstream_schema_supported = lambda: True
    runtime._bootstrap_bundle_for_worker = lambda worker: {'agent_builder_control': graph}
    worker = {'worker_id': 'worker-synthetic', '_active_run_id': 'run-synthetic', '_run_attempt_id': 'attempt-synthetic'}
    command = ['codex', 'exec', '-c', 'approval_policy="never"', '--output-schema', str(schema_path), '-']
    runtime._prepare_native_midstream_command(worker, command, run_root=tmp_path)
    launch_value = owner.read_object(tmp_path / 'native-input-context.json')['launch']
    assert launch_value.get('interject_graph_control') == (graph if matches else None)


@pytest.mark.parametrize('ack', [{'turnId': 'foreign-turn'}, {}, None])
def test_steer_ack_requires_exact_active_turn(ack):
    rpc = SimpleNamespace(call=lambda *args: ack)
    turn = owner.CodexTurn(rpc, 'session-synthetic', 'turn-synthetic')
    with pytest.raises((owner.NativeInputError, AttributeError)):
        turn.interject('Completed evidence', 'd' * 64)


def test_native_steer_does_not_start_resume_interrupt_or_change_authority():
    calls = []
    rpc = SimpleNamespace(call=lambda method, params: calls.append((method, params)) or {'turnId': 'turn-synthetic'})
    turn = owner.CodexTurn(rpc, 'session-synthetic', 'turn-synthetic')
    assert turn.interject('Exact evidence', 'd' * 64) == {'status': 'queued', 'message_id': 'd' * 64}
    assert calls == [('turn/steer', {'threadId': 'session-synthetic', 'expectedTurnId': 'turn-synthetic',
                                  'input': [{'type': 'text', 'text': 'Exact evidence'}]})]
    turn.end()
    with pytest.raises(owner.NativeInputError, match='ended'):
        turn.interject('Late evidence', 'e' * 64)
    assert len(calls) == 1


def test_projected_native_tool_and_team_events_reach_current_consumers():
    events = []
    for item in [
        {'type': 'commandExecution', 'id': 'shell-a', 'command': 'read synthetic file', 'aggregatedOutput': 'Exact tool result', 'exitCode': 0, 'status': 'completed'},
        {'type': 'mcpToolCall', 'id': 'mcp-a', 'server': 'synthetic-server', 'tool': 'read', 'arguments': {}, 'result': {'content': [{'type': 'text', 'text': 'Exact MCP result'}]}, 'status': 'completed'},
        {'type': 'fileChange', 'id': 'write-a', 'changes': [{'path': 'synthetic.txt', 'kind': 'add'}], 'status': 'completed'},
        {'type': 'collabAgentToolCall', 'id': 'bee-a', 'tool': 'spawn_agent', 'receiverThreadIds': ['child-synthetic'], 'senderThreadId': 'session-synthetic', 'agentsStates': {}, 'status': 'completed'},
    ]:
        events.append(owner.codex_event({'method': 'item/completed', 'params': {'threadId': 'session-synthetic', 'turnId': 'turn-synthetic', 'item': item}}, 'session-synthetic', 'turn-synthetic'))
    evidence = _native_tool_evidence('codex-cli', '\n'.join(json.dumps(event) for event in events))
    assert len(evidence['results']) == 3
    assert evidence['results'][0]['output']['text'] == 'Exact tool result'
    assert 'Exact MCP result' in evidence['results'][1]['output']['text']
    from workers_projects_runtime.native_team import project_native_events
    team = project_native_events('codex-cli', events[-1])
    assert team and team[0]['payload']['childRef'] == 'child-synthetic'


@pytest.mark.parametrize('item', [{'type': 'userMessage', 'id': 'user-a', 'content': []}, {'type': 'hookPrompt', 'id': 'hook-a', 'fragments': []}])
def test_host_input_echo_is_not_model_authorship(item):
    assert owner.codex_event({'method': 'item/started', 'params': {'threadId': 'session-synthetic', 'turnId': 'turn-synthetic', 'item': item}}, 'session-synthetic', 'turn-synthetic') is None


def test_foreign_session_or_turn_cannot_project_into_current_output():
    event = {'method': 'item/completed', 'params': {'threadId': 'foreign', 'turnId': 'turn-synthetic', 'item': {'type': 'agentMessage', 'text': 'Foreign'}}}
    assert owner.codex_event(event, 'session-synthetic', 'turn-synthetic') is None
    event['params'].update(threadId='session-synthetic', turnId='foreign')
    assert owner.codex_event(event, 'session-synthetic', 'turn-synthetic') is None


def fake_native(tmp_path):
    cli = tmp_path / 'native.py'
    cli.write_text('''import json, os, sys
from pathlib import Path
receipt = Path(os.environ['NATIVE_RECEIPT'])
answered = []
def emit(value):
 print(json.dumps(value), flush=True)
def notify(method, **params):
 emit({'method': method, 'params': {'threadId': 'session-synthetic', **params}})
for line in sys.stdin:
 v = json.loads(line)
 with receipt.open('a') as f: f.write(json.dumps(v)+'\\n')
 method = v.get('method')
 if method is None and os.environ.get('NATIVE_SERVER_REQUESTS'):
  answered.append(v)
  if len(answered) == 2:
   assert answered[0] == {'id': 713, 'result': {'action': 'cancel', 'content': None, '_meta': None}}
   assert answered[1] == {'id': 'question-native', 'error': {'code': -32000, 'message': 'request_user_input is not supported in exec mode for thread `child-synthetic`', 'data': None}}
   notify('item/completed', turnId='turn-synthetic', item={'type':'agentMessage', 'id':'agent-a', 'text':'Typed native requests resolved'})
   notify('turn/completed', turn={'id':'turn-synthetic', 'status':'completed'})
  continue
 if method == 'initialized': continue
 if method == 'initialize': result = {}
 elif method in ('thread/start', 'thread/resume'):
  params=v['params']; result={'thread': {'id': 'session-synthetic'}, 'model': params['model'], 'approvalPolicy': 'never', 'sandbox': {'type': 'workspaceWrite', 'networkAccess': False, 'writableRoots': params.get('runtimeWorkspaceRoots', [])[1:]}, 'reasoningEffort': 'high', 'cwd': params.get('cwd', str(Path.cwd())), 'runtimeWorkspaceRoots': params.get('runtimeWorkspaceRoots')}
 elif method == 'turn/start': result={'turn': {'id': 'turn-synthetic'}}
 elif method == 'turn/steer': result={'turnId': 'turn-synthetic'}
 elif method == 'turn/interrupt': result={}
 else: raise RuntimeError(method)
 if os.environ.get('NATIVE_BAD_MODEL') and method == 'thread/start': result['model']='changed-model'
 emit({'id': v['id'], 'result': result})
 if method == 'turn/start':
  if os.environ.get('NATIVE_SERVER_REQUESTS'):
   emit({'id':713, 'method':'mcpServer/elicitation/request', 'params': {'threadId':'child-synthetic', 'serverName':'synthetic', 'request': {'mode':'form', 'message':'Choose', 'requestedSchema': {'type':'object', 'properties':{}}}}})
   emit({'id':'question-native', 'method':'item/tool/requestUserInput', 'params': {'threadId':'child-synthetic', 'turnId':'child-turn', 'itemId':'question-item', 'isBlocking':True, 'questions':[{'id':'choice', 'header':'Choice', 'question':'Select', 'options':[{'label':'A', 'description':'A option'}]}]}})
  notify('item/started', turnId='turn-synthetic', item={'type': 'userMessage', 'id': 'user-a', 'content': []})
  notify('item/started', turnId='turn-synthetic', item={'type': 'commandExecution', 'id': 'tool-a', 'command': 'read file', 'status': 'inProgress'})
 if method == 'turn/steer':
  notify('item/completed', turnId='turn-synthetic', item={'type': 'commandExecution', 'id': 'tool-a', 'command': 'read file', 'aggregatedOutput': 'Tool data', 'exitCode': 0, 'status': 'completed'})
  notify('item/completed', turnId='turn-synthetic', item={'type': 'agentMessage', 'id': 'agent-a', 'text': v['params']['input'][0]['text']})
  notify('thread/tokenUsage/updated', turnId='turn-synthetic', tokenUsage={'modelContextWindow': 475000, 'last': {'inputTokens': 12, 'cachedInputTokens': 4, 'outputTokens': 3}})
  notify('turn/completed', turn={'id': 'turn-synthetic', 'status': 'completed'})
''')
    return cli


def ready_context(path, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = owner.read_object(path)
        if value.get('nativeSessionActive') is True:
            return value
        if process.poll() is not None:
            break
        time.sleep(.01)
    pytest.fail('Native test did not expose its active turn')


def test_real_relay_preserves_exec_typed_server_requests_and_exact_native_ids(tmp_path, monkeypatch):
    cli = fake_native(tmp_path)
    launch_value, _ = launch(tmp_path)
    context_path = tmp_path / 'native-input-context.json'
    owner.publish(context_path, {'version': 1, 'protocol': 'codex-app-server', 'workerId': 'worker-synthetic',
        'runId': 'run-synthetic', 'attemptId': 'attempt-synthetic', 'launch': launch_value})
    receipt = tmp_path / 'requests.jsonl'
    environment = {**os.environ, 'PYTHONPATH': str(Path(owner.__file__).parent.parent),
                   'NATIVE_RECEIPT': str(receipt), 'NATIVE_SERVER_REQUESTS': '1'}
    process = subprocess.run([sys.executable, '-m', 'workers_projects_runtime.native_input', str(context_path),
        sys.executable, str(cli)], input='Admitted user input', text=True, capture_output=True, env=environment, timeout=5)
    assert process.returncode == 0, process.stderr + process.stdout
    calls = [json.loads(line) for line in receipt.read_text().splitlines()]
    responses = [value for value in calls if 'method' not in value]
    assert [value['id'] for value in responses] == [713, 'question-native']
    assert responses[0]['result']['action'] == 'cancel'
    assert responses[1]['error']['code'] == -32000
    assert [value.get('method') for value in calls[:4]] == ['initialize', 'initialized', 'thread/start', 'turn/start']
    assert 'Typed native requests resolved' in process.stdout
    assert owner.read_object(context_path)['nativeSessionActive'] is False


def test_unreviewed_native_version_keeps_exec_before_start(tmp_path, monkeypatch):
    runtime = object.__new__(HostCodexCliRuntime)
    runtime.binary = sys.executable
    calls = []
    def version_only(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='codex-cli unreviewed-version')
    monkeypatch.setattr(subprocess, 'run', version_only)
    assert runtime._native_midstream_schema_supported() is False
    assert calls == [[sys.executable, '--version']]


def test_real_relay_process_mailbox_replays_once_and_terminal_fences(tmp_path, monkeypatch):
    cli = fake_native(tmp_path)
    launch_value, _ = launch(tmp_path)
    command = [sys.executable, str(cli)]
    context_path = tmp_path / 'native-input-context.json'
    owner.publish(context_path, {'version': 1, 'protocol': 'codex-app-server', 'workerId': 'worker-synthetic',
        'runId': 'run-synthetic', 'attemptId': 'attempt-synthetic', 'nativeSessionActive': False, 'launch': launch_value})
    receipt = tmp_path / 'requests.jsonl'
    monkeypatch.setenv('NATIVE_RECEIPT', str(receipt))
    environment = {**os.environ, 'PYTHONPATH': str(Path(owner.__file__).parent.parent)}
    process = subprocess.Popen([sys.executable, '-m', 'workers_projects_runtime.native_input', str(context_path), *command],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    process.stdin.write('Admitted user input'); process.stdin.close(); process.stdin = None
    try:
        active = ready_context(context_path, process)
        assert active['nativeSessionId'] == 'session-synthetic'
        root = tmp_path / 'native-control'
        # Publication precedes mailbox start by a few instructions.
        deadline = time.monotonic() + 2
        while not root.is_dir() and time.monotonic() < deadline: time.sleep(.01)
        body = {'action': 'interject', 'session_id': active['nativeSessionId'], 'run_id': 'run-synthetic',
            'attempt_id': 'attempt-synthetic', 'text': 'Completed worker evidence', 'request_fingerprint': 'a' * 64}
        identity = 'd' * 64
        result = submit_control(root, body, message_id=identity, timeout=2)
        assert result == {'status': 'queued', 'message_id': identity}
        assert submit_control(root, body, message_id=identity, timeout=0) == result
        with pytest.raises(ValueError, match='conflict'):
            submit_control(root, {**body, 'text': 'Changed evidence'}, message_id=identity, timeout=0)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        events = [json.loads(line) for line in stdout.splitlines()]
        lifecycle = [event for event in events if event['type'] in {'thread.started', 'turn.started', 'turn.completed'}]
        assert lifecycle[0] == {'type': 'thread.started', 'thread_id': 'session-synthetic'}
        assert lifecycle[1]['turn_id'] == 'turn-synthetic'
        assert lifecycle[-1]['usage'] == {'input_tokens': 12, 'cached_input_tokens': 4, 'output_tokens': 3}
        assert lifecycle[-1]['context_window_tokens'] == 475000
        assert lifecycle[-1]['observed_at_ms'] > 0
        assert 'Completed worker evidence' in stdout
        assert owner.read_object(context_path)['nativeSessionActive'] is False
        calls = [json.loads(line) for line in receipt.read_text().splitlines()]
        assert [call['method'] for call in calls] == ['initialize', 'initialized', 'thread/start', 'turn/start', 'turn/steer']
        start = calls[3]['params']
        assert start['input'][0] == {'type': 'text', 'text': 'Admitted user input'}
        assert start['input'][1] == launch_value['images'][0]
        assert start['outputSchema'] == {'type': 'object'}
    finally:
        if process.poll() is None: process.terminate(); process.wait(timeout=5)


@pytest.mark.parametrize('action', ['interject', 'interject_graph_output'])
@pytest.mark.parametrize('mutation', ['run', 'attempt', 'session', 'turn', 'inactive', 'dead-process'])
def test_live_capability_is_exact_typed_and_generation_bound(tmp_path, mutation, action):
    runtime = object.__new__(HostCodexCliRuntime)
    runtime._run_root = lambda *args: tmp_path
    runtime._read_active_session = lambda *args: {'run_id': 'run-synthetic', 'attempt_id': 'attempt-synthetic'}
    runtime.host_process_identity = lambda *args: {'verified': True}
    context = {'protocol': 'codex-app-server', 'workerId': 'worker-synthetic', 'runId': 'run-synthetic',
        'attemptId': 'attempt-synthetic', 'nativeSessionActive': True, 'nativeSessionId': 'session-synthetic',
        'nativeTurnId': 'turn-synthetic', 'nativeControlCapabilities': {'interject': True, 'interject_graph_output': True}}
    owner.publish(tmp_path / 'native-input-context.json', context)
    assert runtime.supports_native_control({'worker_id': 'worker-synthetic'}, action, run_id='run-synthetic', attempt_id='attempt-synthetic')
    if mutation == 'dead-process': runtime.host_process_identity = lambda *args: None
    else:
        key = {'run': 'runId', 'attempt': 'attemptId', 'session': 'nativeSessionId', 'turn': 'nativeTurnId', 'inactive': 'nativeSessionActive'}[mutation]
        context[key] = 'foreign' if mutation in ('run', 'attempt') else False if mutation == 'inactive' else ''
        owner.publish(tmp_path / 'native-input-context.json', context)
    assert not runtime.supports_native_control({'worker_id': 'worker-synthetic'}, action, run_id='run-synthetic', attempt_id='attempt-synthetic')


def test_claude_retains_truthful_unsupported_same_turn_input():
    assert not callable(getattr(object.__new__(HostClaudeCodeRuntime), 'native_control', None))


def test_native_model_mismatch_is_refused_before_user_turn(tmp_path, monkeypatch):
    cli = fake_native(tmp_path)
    value, _ = launch(tmp_path)
    path = tmp_path / 'native-input-context.json'
    owner.publish(path, {'version': 1, 'protocol': 'codex-app-server', 'workerId': 'worker-synthetic',
        'runId': 'run-synthetic', 'attemptId': 'attempt-synthetic', 'nativeSessionActive': False, 'launch': value})
    receipt = tmp_path / 'requests.jsonl'
    monkeypatch.setenv('NATIVE_RECEIPT', str(receipt)); monkeypatch.setenv('NATIVE_BAD_MODEL', '1')
    result = subprocess.run([sys.executable, '-m', 'workers_projects_runtime.native_input', str(path), sys.executable, str(cli)],
        input='Admitted user input', text=True, capture_output=True, timeout=5,
        env={**os.environ, 'PYTHONPATH': str(Path(owner.__file__).parent.parent)})
    assert result.returncode == 1
    assert 'native_binding_mismatch' in result.stdout
    assert [json.loads(line)['method'] for line in receipt.read_text().splitlines()] == ['initialize', 'initialized', 'thread/start']
    assert owner.read_object(path)['nativeSessionActive'] is False
