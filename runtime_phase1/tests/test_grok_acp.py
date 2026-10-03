from __future__ import annotations

import json
import subprocess
import sys
from threading import Thread

import pytest

from workers_projects_runtime.grok_acp import AcpClient, AcpError, GrokSession


def peer(script):
    return subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def test_bidirectional_permission_does_not_deadlock_prompt():
    process = peer('''
import sys,json
r=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':'permission-1','method':'session/request_permission','params':{'sessionId':'s','options':[{'optionId':'yes','kind':'allow_once'}]}}),flush=True)
a=json.loads(sys.stdin.readline())
assert a['result']=={'outcome':{'outcome':'cancelled'}}
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'stopReason':'end_turn'}}),flush=True)
''')
    with AcpClient(process) as client:
        assert client.request('session/prompt', {}, timeout=2)['stopReason'] == 'end_turn'


def test_malformed_protocol_fails_waiters_without_hanging():
    process = peer("import sys;sys.stdin.readline();print('not-json',flush=True)")
    with AcpClient(process) as client:
        with pytest.raises(AcpError, match='Malformed'):
            client.request('initialize', {}, timeout=2)


def test_request_timing_excludes_private_protocol_payloads():
    process = peer('''
import sys,json
r=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'private':'synthetic-private-output'}}),flush=True)
''')
    events = []
    with AcpClient(process, request_timing=events.append) as client:
        assert client.request('initialize', {'private': 'synthetic-private-input'}, timeout=2) == {'private': 'synthetic-private-output'}
    assert len(events) == 1
    assert set(events[0]) == {'method', 'outcome', 'duration_ms'}
    assert events[0]['method'] == 'initialize' and events[0]['outcome'] == 'ok'
    assert events[0]['duration_ms'] >= 0


def test_request_timing_preserves_timeout_and_error_outcomes():
    process = peer("import sys,time;sys.stdin.readline();time.sleep(1)")
    events = []
    with AcpClient(process, request_timing=events.append) as client:
        with pytest.raises(AcpError, match='timed out'):
            client.request('initialize', {}, timeout=.03)
    assert events[0]['outcome'] == 'timeout'
    process = peer("import sys;sys.stdin.readline();print('not-json',flush=True)")
    with AcpClient(process, request_timing=events.append) as client:
        with pytest.raises(AcpError, match='Malformed'):
            client.request('initialize', {}, timeout=2)
    assert events[-1]['outcome'] == 'error'


def test_request_timing_sink_failure_does_not_replace_result():
    process = peer('''
import sys,json
r=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'ok':True}}),flush=True)
''')
    def broken_sink(_):
        raise RuntimeError('observation unavailable')
    with AcpClient(process, request_timing=broken_sink) as client:
        assert client.request('initialize', {}, timeout=2) == {'ok': True}


def test_timeout_does_not_poison_later_response():
    process = peer('''
import sys,json,time
first=json.loads(sys.stdin.readline());time.sleep(.15)
print(json.dumps({'jsonrpc':'2.0','id':first['id'],'result':{}}),flush=True)
second=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':second['id'],'result':{'ok':True}}),flush=True)
''')
    with AcpClient(process) as client:
        with pytest.raises(AcpError, match='timed out'):
            client.request('first', {}, timeout=.03)
        assert client.request('second', {}, timeout=2) == {'ok': True}


class ProtocolPeer:
    def __init__(self, model='grok-selected'):
        self.calls = []
        self.model = model
        self.notification = None
    def request(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == 'initialize':
            return {'protocolVersion':1, 'agentCapabilities':{'loadSession':True},
                    'authMethods':[{'id':'cached_token'}],
                    '_meta':{'grokShell':True,'agentVersion':'1.0','defaultAuthMethodId':'cached_token'}}
        if method in ('session/new', 'session/load'):
            return {'sessionId':'native-exact', 'models':{'currentModelId':self.model},
                    'configOptions':[{'id':'reasoning_effort','currentValue':'medium',
                      'options':[{'value':'medium'},{'value':'high'}]}]}
        if method == 'session/set_config_option':
            return {'configOptions':[{'id':params['configId'],'currentValue':params['value']}]}
        if method == 'session/prompt':
            self.notification('session/update', {'sessionId':'other','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'WRONG'}}})
            self.notification('session/update', {'sessionId':'native-exact','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'native output'}}})
            return {'stopReason':'end_turn'}
        return {'result':{'status':'queued'}}
    def notify(self, method, params):
        self.calls.append((method, params))


class RestrictedProtocolPeer(ProtocolPeer):
    def __init__(self, initial_tools=(), final_tools=()):
        super().__init__()
        self.initial_tools = initial_tools
        self.final_tools = final_tools
        self.effort = 'medium'

    def manifest(self, tools, identity='native-exact'):
        if tools is False:
            return
        update = {'sessionUpdate': 'available_commands_update'}
        if tools is not None:
            update['_meta'] = {'tools': list(tools) if isinstance(tools, tuple) else tools}
        self.notification('session/update', {'sessionId': identity, 'update': update})

    def request(self, method, params, timeout=30):
        if method in ('session/new', 'session/load'):
            # The installed native implementation advertises before the new response binds its ID.
            self.manifest(self.initial_tools if method == 'session/new' else self.final_tools)
        if method == 'session/set_config_option' and params['configId'] == 'reasoning_effort':
            self.effort = params['value']
        result = super().request(method, params, timeout)
        if method in ('session/new', 'session/load'):
            result['configOptions'][0]['currentValue'] = self.effort
        return result


def test_restricted_open_buffers_pre_response_manifest_then_verifies_final_exact_session():
    client = RestrictedProtocolPeer()
    events = []
    session = GrokSession(client, event=events.append)
    session.open(cwd='/empty-native', model='grok-selected', effort='high',
                 system_prompt_override='Exact synthetic authority.', native_tools_disabled=True)
    setups = [(method, params) for method, params in client.calls if method in ('session/new', 'session/load')]
    assert [method for method, _ in setups] == ['session/new', 'session/load']
    for method, params in setups:
        assert params['mcpServers'] == []
        assert params['cwd'] == '/empty-native'
        assert params['_meta']['agentProfile'] == {
            'name': 'native-tools-disabled', 'description': 'Native tools disabled',
            'tools': ['ToolSearch'], 'disallowedTools': ['search_tool', 'use_tool'],
            'agentsMd': False, 'discoverSkills': False}
        assert params['_meta']['systemPromptOverride'] == 'Exact synthetic authority.'
        assert params['_meta']['yoloMode'] is False and params['_meta']['autoMode'] is False
        assert ('sessionId' in params) is (method == 'session/load')
    assert setups[-1][1]['sessionId'] == session.session_id == 'native-exact'
    assert setups[-1][1]['_meta']['noReplay'] is True
    assert session.configured_model == 'grok-selected'
    assert session._options(session.session_state, 'reasoning_effort')['currentValue'] == 'high'
    assert next(event for event in events if event['type'] == 'grok.native_tools.verified')['function_tool_count'] == 0
    assert session.prompt('Use only this supplied synthetic context.') == 'native output'


@pytest.mark.parametrize('initial,final', [
    (('read_file',), ()), (None, ()), (False, ()), ((), ('web_search',)),
    ((), None), ((), False), ((), 'invalid'),
])
def test_restricted_missing_malformed_or_nonempty_inventory_never_prompts(monkeypatch, initial, final):
    client = RestrictedProtocolPeer(initial, final)
    session = GrokSession(client)
    monkeypatch.setattr(session._native_tools_manifest, 'wait', lambda timeout: session._native_tools_manifest.is_set())
    with pytest.raises(AcpError, match='inventory'):
        session.open(cwd='/empty-native', model='grok-selected', native_tools_disabled=True)
    assert not any(method == 'session/prompt' for method, _ in client.calls)
    with pytest.raises(AcpError):
        session.prompt('Synthetic.')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_restricted_preopen_inventory_is_id_scoped_and_bounded():
    class IdPeer(RestrictedProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/new':
                self.manifest(('read_file',), 'other-session')
            return super().request(method, params, timeout)
    session = GrokSession(IdPeer())
    session.open(cwd='/empty-native', model='grok-selected', native_tools_disabled=True)
    assert session._native_tools_verified
    class OverflowPeer(RestrictedProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/new':
                for index in range(33):
                    self.manifest((), f'other-{index}')
            return super().request(method, params, timeout)
    client = OverflowPeer()
    with pytest.raises(AcpError, match='inventory'):
        GrokSession(client).open(cwd='/empty-native', model='grok-selected', native_tools_disabled=True)
    assert not any(method == 'session/prompt' for method, _ in client.calls)


@pytest.mark.parametrize('setup', [{'session_id': 'prior-session'}, {'mcp_servers': [{'name': 'synthetic'}]},
                                  {'native_tools_disabled': 'false'}])
def test_restricted_resume_mcp_or_untyped_flag_fails_before_native_setup(setup):
    client = RestrictedProtocolPeer()
    with pytest.raises(AcpError):
        GrokSession(client).open(cwd='/empty-native', model='grok-selected',
                                **({'native_tools_disabled': True} | setup))
    assert not client.calls


def test_restricted_late_tool_advertisement_blocks_prompt():
    client = RestrictedProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/empty-native', model='grok-selected', native_tools_disabled=True)
    client.manifest(('read_file',))
    with pytest.raises(AcpError, match='not verified'):
        session.prompt('Synthetic.')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_restricted_native_tool_call_cancels_and_cannot_be_accepted():
    class ToolPeer(RestrictedProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/prompt':
                self.notification('session/update', {'sessionId': 'native-exact',
                    'update': {'sessionUpdate': 'tool_call', 'toolCallId': 'synthetic-read'}})
            return super().request(method, params, timeout)
    client = ToolPeer()
    session = GrokSession(client)
    session.open(cwd='/empty-native', model='grok-selected', native_tools_disabled=True)
    with pytest.raises(AcpError, match='native tools'):
        session.prompt('Synthetic context.')
    assert ('session/cancel', {'sessionId': 'native-exact'}) in client.calls


def test_restricted_inventory_violation_at_prompt_boundary_never_prompts():
    client = RestrictedProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/empty-native', model='grok-selected', native_tools_disabled=True)

    class InventoryRace:
        def acquire(self, blocking=False):
            client.manifest(('read_file',))
            return True

        def release(self):
            pass

    session._prompt_lock = InventoryRace()
    with pytest.raises(AcpError, match='native tools'):
        session.prompt('Synthetic context.')
    assert ('session/cancel', {'sessionId': 'native-exact'}) in client.calls
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_restricted_final_inventory_refresh_preserves_exact_model():
    class ChangedPeer(RestrictedProtocolPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == 'session/load':
                result['models']['currentModelId'] = 'other-model'
            return result
    client = ChangedPeer()
    with pytest.raises(AcpError, match='session/model'):
        GrokSession(client).open(cwd='/empty-native', model='grok-selected', native_tools_disabled=True)
    assert not any(method == 'session/prompt' for method, _ in client.calls)


@pytest.mark.parametrize('unsafe', ['missing', 'nonempty', 'linked', 'open', 'resume'])
def test_restricted_runner_refuses_unsafe_workspace_before_native_launch(tmp_path, monkeypatch, unsafe):
    from io import StringIO
    from workers_projects_runtime import grok_acp_runner as runner
    workspace = tmp_path / 'empty'
    workspace.mkdir(mode=0o700)
    selected = workspace
    if unsafe == 'missing':
        selected = tmp_path / 'missing'
    elif unsafe == 'nonempty':
        (workspace / 'private.txt').write_text('Synthetic canary')
    elif unsafe == 'linked':
        selected = tmp_path / 'linked'
        selected.symlink_to(workspace, target_is_directory=True)
    elif unsafe == 'open':
        workspace.chmod(0o755)
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'native-home'))
    monkeypatch.setattr(sys, 'stdin', StringIO('Synthetic context.'))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Unsafe native launch'))
    args = ['--binary', 'synthetic-native', '--model', 'grok-selected', '--native-tools-disabled',
            '--restricted-workspace', str(selected)]
    if unsafe == 'resume':
        args += ['--session-id', 'existing-session']
    with pytest.raises(ValueError, match='fresh empty'):
        runner.main(args)


def test_restricted_runner_uses_empty_child_cwd_and_never_reads_mcp_config(tmp_path, monkeypatch, capfd):
    from io import StringIO
    from workers_projects_runtime import grok_acp_runner as runner
    workspace = tmp_path / 'empty'
    workspace.mkdir(mode=0o700)
    native = tmp_path / 'native-peer'
    native.write_text('#!' + sys.executable + '\n' + '''
import json,os,sys
for line in sys.stdin:
 r=json.loads(line);method=r['method'];p=r.get('params',{})
 if method=='initialize':result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate':result={}
 elif method in ('session/new','session/load'):
  assert p['cwd']==os.getcwd() and not os.listdir('.') and p['mcpServers']==[]
  assert p['_meta']['agentProfile']['tools']==['ToolSearch']
  assert p['_meta']['agentProfile']['disallowedTools']==['search_tool','use_tool']
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-exact','update':{'sessionUpdate':'available_commands_update','_meta':{'tools':[]}}}}),flush=True)
  result={'sessionId':'native-exact','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-exact','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Synthetic answer'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 else:raise AssertionError(method)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    native.chmod(0o700)
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'native-home'))
    monkeypatch.setattr(sys, 'stdin', StringIO('Supplied synthetic context.'))
    assert runner.main(['--binary', str(native), '--model', 'grok-selected', '--native-tools-disabled',
                        '--restricted-workspace', str(workspace), '--mcp-file', str(tmp_path / 'must-not-be-read')]) == 0
    events = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    verified = next(index for index, event in enumerate(events) if event['type'] == 'grok.native_tools.verified')
    prompted = next(index for index, event in enumerate(events) if event.get('phase') == 'prompt_started')
    assert verified < prompted
    assert next(event for event in events if event['type'] == 'grok.result')['output'] == 'Synthetic answer'
    assert not list(workspace.iterdir())


@pytest.mark.parametrize('close_capability', [None, False, {}])
def test_session_close_requires_advertisement_and_keeps_exact_history(close_capability):
    class ClosingPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/close':
                assert timeout == 2
            result = super().request(method, params, timeout)
            if method == 'initialize' and close_capability is not None:
                result['agentCapabilities']['sessionCapabilities'] = {'close': close_capability}
            return result
    client = ClosingPeer()
    session = GrokSession(client)
    assert session.close() is False and not client.calls
    session.open(cwd='/workspace', model='grok-selected', session_id='native-exact', effort='high')
    assert session.prompt('Actual synthetic task.') == 'native output'
    state = session.session_state
    assert session.close() is isinstance(close_capability, dict)
    closes = [params for method, params in client.calls if method == 'session/close']
    assert closes == ([{'sessionId': 'native-exact'}] if isinstance(close_capability, dict) else [])
    assert session.session_id == 'native-exact' and session.session_state is state
    resumed = GrokSession(client)
    resumed.open(cwd='/workspace', model='grok-selected', session_id=session.session_id, effort='high')
    loads = [params for method, params in client.calls if method == 'session/load']
    assert loads[-1]['sessionId'] == 'native-exact' and loads[-1]['_meta']['noReplay'] is True
    assert resumed.configured_model == session.configured_model == 'grok-selected'


@pytest.mark.parametrize('close_outcome', ['ok', 'timeout', 'rejected'])
def test_runner_result_precedes_close_and_cleanup_preserves_success(tmp_path, monkeypatch, capfd, close_outcome):
    from workers_projects_runtime import grok_acp_runner as runner
    home = tmp_path / 'native-home'
    monkeypatch.setenv('GROK_HOME', str(home))
    native = tmp_path / 'native-peer'
    native.write_text('#!' + sys.executable + '\n' + '''
import json,os,sys
from pathlib import Path
assert sys.argv[1:]==['agent','--no-leader','--model','grok-selected','stdio']
history={'sessionId':'native-exact','closed':False}
for line in sys.stdin:
 r=json.loads(line);method=r['method']
 if method=='initialize':
  result={'protocolVersion':1,'agentCapabilities':{'loadSession':True,'sessionCapabilities':{'close':{}}},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate':result={}
 elif method=='session/new':result={'sessionId':'native-exact','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  history['prompt']=r['params']['prompt']
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-exact','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'native output'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 elif method=='session/close':
  assert r['params']=={'sessionId':'native-exact'}
  history['closed']=True
  if CLOSE_OUTCOME=='timeout':continue
  if CLOSE_OUTCOME=='rejected':
   print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':-32601,'message':'unsupported'}}),flush=True);continue
  result={}
 else:raise AssertionError(method)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
# This write happens only after stdin EOF, not after a termination signal.
(Path(os.environ['GROK_HOME'])/'history.json').write_text(json.dumps(history))
'''.replace('CLOSE_OUTCOME', repr(close_outcome)))
    native.chmod(0o700)
    from io import StringIO
    monkeypatch.setattr(sys, 'stdin', StringIO('Exact synthetic task.'))
    assert runner.main(['--binary', str(native), '--model', 'grok-selected']) == 0
    events = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    result_index = next(i for i, event in enumerate(events) if event['type'] == 'grok.result')
    close_index = next(i for i, event in enumerate(events) if event['type'] == 'grok.acp.rpc' and event['method'] == 'session/close')
    assert result_index < close_index and events[result_index]['output'] == 'native output'
    assert events[close_index]['outcome'] == {'ok': 'ok', 'timeout': 'timeout', 'rejected': 'error'}[close_outcome]
    assert not any(event['type'] == 'grok.error' for event in events)
    assert json.loads((home / 'history.json').read_text()) == {
        'sessionId': 'native-exact', 'closed': True,
        'prompt': [{'type': 'text', 'text': 'Exact synthetic task.'}]}


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_load_disables_history_replay_without_changing_model_authority(session_id):
    class ReplayingPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/set_config_option' and params['configId'] == 'model':
                self.model = params['value']
            result = super().request(method, params, timeout)
            if method in ('session/new', 'session/load'):
                result['configOptions'].append({'id': 'model', 'currentValue': self.model,
                    'options': [{'value': 'old-model'}, {'value': 'grok-selected'}]})
            if method == 'session/load' and not params.get('_meta', {}).get('noReplay'):
                self.notification('session/update', {'sessionId': 'native-exact',
                    'update': {'sessionUpdate': 'agent_message_chunk',
                               'content': {'type': 'text', 'text': 'Old answer.'}}})
            return result
    client = ReplayingPeer('old-model')
    events = []
    session = GrokSession(client, event=events.append)
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id,
                 effort='high', system_prompt_override='Exact current authority.', yolo_mode=False)
    assert not any(e.get('update', {}).get('sessionUpdate') == 'agent_message_chunk' for e in events)
    loads = [p for method, p in client.calls if method == 'session/load']
    assert loads and all(p['_meta']['noReplay'] is True for p in loads)
    assert all(p['_meta']['systemPromptOverride'] == 'Exact current authority.' for p in loads)
    assert all(p['_meta']['yoloMode'] is False and p['_meta']['autoMode'] is False for p in loads)
    assert session.configured_model == 'grok-selected'
    assert session.prompt('Current task.') == 'native output'


def test_exact_resume_model_effort_and_session_fencing():
    client = ProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', session_id='native-exact', effort='high')
    assert ('session/load', {'sessionId':'native-exact','cwd':'/workspace','mcpServers':[],
                             '_meta': {'noReplay': True}}) in client.calls
    assert session.prompt('actual user task', timeout=2) == 'native output'
    assert client.calls[-1][1]['prompt'] == [{'type':'text','text':'actual user task'}]


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_output_schema_is_native_prompt_metadata_on_new_and_resumed_sessions(session_id):
    client = ProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id, effort='high')
    schema = {'type': 'object', 'properties': {'content': {'type': 'string'}}, 'required': ['content']}
    session.prompt('Actual user task', output_schema=schema)
    assert client.calls[-1] == ('session/prompt', {
        'sessionId': 'native-exact', 'prompt': [{'type': 'text', 'text': 'Actual user task'}],
        '_meta': {'outputSchema': schema},
    })
    before = len(client.calls)
    with pytest.raises(AcpError, match='schema must be an object'):
        session.prompt('Actual user task', output_schema=[])
    assert len(client.calls) == before


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_pinned_conversation_authority_replaces_the_native_system_prompt_without_changing_mcp(session_id):
    client = ProtocolPeer()
    session = GrokSession(client)
    authority = 'Synthetic current authority.\n<viventium_feeling_state>current</viventium_feeling_state>'
    servers = [{'name': 'synthetic-broker', 'command': 'synthetic', 'args': [], 'env': []}]
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id, effort='high',
                 mcp_servers=servers, system_prompt_override=authority)
    method, params = next((m, p) for m, p in client.calls if m in ('session/new', 'session/load'))
    assert method == ('session/load' if session_id else 'session/new')
    assert params['_meta'] == {'systemPromptOverride': authority,
                              **({'noReplay': True} if session_id else {})}
    assert params['mcpServers'] == servers
    assert 'rules' not in params['_meta']
    assert not any('Synthetic current authority.' in str(p.get('prompt', [])) for _, p in client.calls)
    with pytest.raises(AcpError, match='one native placement'):
        GrokSession(ProtocolPeer()).open(cwd='/workspace', model='grok-selected',
            rules='old', system_prompt_override='new')


def test_prompt_returns_terminal_assistant_segment_after_tools():
    class ToolPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method != 'session/prompt':
                return super().request(method, params, timeout)
            def update(kind, text=''):
                self.notification('session/update', {'sessionId':'native-exact',
                    'update':{'sessionUpdate':kind, 'content':{'type':'text','text':text}}})
            update('agent_message_chunk', 'Checking your task.')
            update('tool_call')
            update('agent_message_chunk', 'Reading the result.')
            update('tool_call')
            update('agent_message_chunk', 'Final ')
            update('agent_message_chunk', 'answer.')
            return {'stopReason':'end_turn'}

    client = ToolPeer()
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected')
    assert session.prompt('Synthetic task') == 'Final answer.'


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_model_change_reapplies_pinned_authority_before_prompt(session_id):
    class SwitchingPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/set_config_option' and params['configId'] == 'model':
                self.model = params['value']
            result = super().request(method, params, timeout)
            if method in ('session/new', 'session/load'):
                result['configOptions'].append({'id': 'model', 'currentValue': self.model,
                    'options': [{'value': 'old-model'}, {'value': 'grok-selected'}]})
            return result

    client = SwitchingPeer('old-model')
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id, effort='high',
                 system_prompt_override='Current pinned authority.')
    session.prompt('Actual user task')
    model_index = next(i for i, (method, params) in enumerate(client.calls)
        if method == 'session/set_config_option' and params['configId'] == 'model')
    method, params = client.calls[model_index + 1]
    assert method == 'session/load'
    assert params == {'cwd': '/workspace', 'mcpServers': [], 'sessionId': 'native-exact',
                      '_meta': {'systemPromptOverride': 'Current pinned authority.', 'noReplay': True}}
    assert client.calls[-1][0] == 'session/prompt'


@pytest.mark.parametrize('authority', ['', ' \n '])
def test_blank_override_never_falls_back_to_vendor_authority(authority):
    client = ProtocolPeer()
    with pytest.raises(AcpError, match='must not be blank'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected',
                                system_prompt_override=authority)
    assert client.calls == []


def test_model_mismatch_never_silently_substitutes():
    client = ProtocolPeer('other-model')
    with pytest.raises(AcpError, match='model'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


class ActualAccountPeer(ProtocolPeer):
    def __init__(self, model, offered, *, high=True):
        super().__init__(model)
        self.offered = offered
        self.high = high
    def request(self, method, params, timeout=30):
        if method == 'session/set_config_option' and params['configId'] == 'model':
            self.model = params['value']
        result = super().request(method, params, timeout)
        if method in ('session/new', 'session/load'):
            result['configOptions'].append({'id': 'model', 'currentValue': self.model,
                'options': [{'group': 'account', 'options': [{'value': value} for value in self.offered]}]})
            if not self.high:
                result['configOptions'][0]['options'] = [{'value': 'medium'}]
        return result


@pytest.mark.parametrize('requested,offered', [
    ('grok-4.7-build-fast', []), ('other-model', ['grok-4.7']),
])
def test_other_missing_account_models_reject_before_prompt(requested, offered):
    client = ActualAccountPeer('account-default', offered)
    with pytest.raises(AcpError):
        GrokSession(client).open(cwd='/workspace', model=requested, effort='high')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


@pytest.mark.parametrize('initial_model', ['account-default', 'grok-4.7', 'grok-4.7-build-fast'])
def test_actual_account_fast_absence_resumes_standard_and_switches_back(initial_model):
    client = ActualAccountPeer(initial_model, ['grok-4.7'])
    requested = 'grok-4.7-build-fast'
    authority = 'Exact current authority.'
    first = GrokSession(client)
    first.open(cwd='/workspace', model=requested, effort='high',
               system_prompt_override=authority, yolo_mode=True)
    assert first.configured_model == 'grok-4.7' and first.requested_model == requested
    assert first.prompt('First goal.') == 'native output'
    first_changes = [p for m,p in client.calls if m == 'session/set_config_option' and p['configId'] == 'model']
    assert len(first_changes) == (initial_model != 'grok-4.7')
    client.calls.clear()
    second = GrokSession(client)
    second.open(cwd='/workspace', model=requested, session_id=first.session_id, effort='high',
                system_prompt_override=authority, yolo_mode=True)
    assert second.prompt('Second goal.') == 'native output'
    assert second.session_id == first.session_id
    assert not any(m == 'session/set_config_option' and p['configId'] == 'model' for m,p in client.calls)
    assert len([m for m,p in client.calls if m == 'session/load']) == 1
    client.calls.clear()
    client.offered.append(requested)
    third = GrokSession(client)
    third.open(cwd='/workspace', model=requested, session_id=second.session_id, effort='high',
               system_prompt_override=authority, yolo_mode=True)
    assert third.configured_model == requested and third.session_id == first.session_id
    assert third.prompt('Third goal.') == 'native output'
    assert len([p for m,p in client.calls if m == 'session/set_config_option' and p['configId'] == 'model']) == 1
    assert all(p['_meta']['noReplay'] is True and p['_meta']['systemPromptOverride'] == authority
               and p['_meta']['yoloMode'] is True for m,p in client.calls if m == 'session/load')


def test_standard_substitution_checks_high_after_model_selection():
    client = ActualAccountPeer('grok-4.7', ['grok-4.7'], high=False)
    with pytest.raises(AcpError, match='effort is unsupported'):
        GrokSession(client).open(cwd='/workspace', model='grok-4.7-build-fast', effort='high')
    assert not any(m == 'session/prompt' for m,p in client.calls)


@pytest.mark.parametrize('phase', ['initialize', 'session/new', 'reasoning_effort'])
def test_stop_during_open_prevents_native_prompt(phase):
    class CancellingPeer(ActualAccountPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == phase or (phase == 'reasoning_effort' and params.get('configId') == phase):
                session.cancel()
            return result
    client = CancellingPeer('grok-4.7-build-fast', ['grok-4.7-build-fast'])
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-4.7-build-fast', effort='high')
    with pytest.raises(AcpError) as stopped:
        session.prompt('Current goal.')
    assert stopped.value.stop_reason == 'cancelled'
    assert session.cancel_requested is True
    assert not any(m == 'session/prompt' for m,p in client.calls)


def test_actual_account_fast_present_keeps_exact_fast_and_high():
    client = ProtocolPeer('grok-4.7-build-fast')
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-4.7-build-fast', effort='high')
    assert session.configured_model == 'grok-4.7-build-fast'
    assert ('session/set_config_option', {'sessionId':'native-exact', 'configId':'reasoning_effort', 'value':'high'}) in client.calls
    assert not any(method == 'session/set_config_option' and params['configId'] == 'model' for method, params in client.calls)


def test_unsupported_effort_fails_before_prompt():
    client = ProtocolPeer()
    with pytest.raises(AcpError, match='effort'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected', effort='ultra')


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
@pytest.mark.parametrize('report', ['models', 'option', 'notification'])
def test_effort_routing_cannot_silently_change_the_exact_model(session_id, report):
    class RoutingPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == 'session/set_config_option' and params['configId'] == 'reasoning_effort':
                model_option = {'id': 'model', 'currentValue': 'other-model'}
                if report == 'models':
                    result['models'] = {'currentModelId': 'other-model'}
                elif report == 'option':
                    result['configOptions'].append(model_option)
                else:
                    self.notification('session/update', {'sessionId': 'native-exact',
                        'update': {'sessionUpdate': 'config_option_update',
                                   'configOptions': [model_option]}})
            return result

    client = RoutingPeer()
    with pytest.raises(AcpError, match='effort changed the exact configured model'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected',
                                session_id=session_id, effort='high')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_unverified_interject_extension_is_not_advertised():
    client = ProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected')
    with pytest.raises(AcpError, match='interject'):
        session.interject('change', 'message-1')
    session.cancel()
    assert client.calls[-1] == ('session/cancel', {'sessionId':'native-exact'})


def test_permission_callback_must_select_an_offered_option():
    process = peer('''
import sys,json
r=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':'permission','method':'session/request_permission','params':{'options':[{'optionId':'offered','kind':'allow_once'}]}}),flush=True)
a=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':a['result']}),flush=True)
''')
    with AcpClient(process, permission=lambda _: 'not-offered') as client:
        assert client.request('test', {}, timeout=2) == {'outcome':{'outcome':'cancelled'}}


def test_oversized_frame_fails_closed():
    process = peer("import sys;sys.stdin.readline();print('x'*4096,flush=True)")
    with AcpClient(process, max_frame_bytes=1024) as client:
        with pytest.raises(AcpError, match='limit'):
            client.request('test', {}, timeout=2)


def test_protocol_mismatch_fails_before_session_creation():
    class WrongVersion(ProtocolPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == 'initialize':
                result['protocolVersion'] = 99
            return result
    client = WrongVersion()
    with pytest.raises(AcpError, match='protocol'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected')
    assert len(client.calls) == 1


def test_output_limit_is_explicit_instead_of_truncating():
    client = ProtocolPeer()
    session = GrokSession(client, max_output_chars=3)
    session.open(cwd='/workspace', model='grok-selected')
    with pytest.raises(AcpError, match='output exceeds'):
        session.prompt('task')


@pytest.mark.parametrize('method,expected', [
    ('_x.ai/ask_user_question',{'outcome':'cancelled'}),
    ('_x.ai/mcp/elicit',{'outcome':'cancel'}),
    ('_x.ai/exit_plan_mode',{'outcome':'cancelled'}),
])
def test_native_interactions_default_to_cancel_without_auto_approval(method, expected):
    process = peer('''
import sys,json
r=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':'interaction','method':''' + repr(method) + ''','params':{'sessionId':'s'}}),flush=True)
a=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':a['result']}),flush=True)
''')
    with AcpClient(process) as client:
        assert client.request('test',{},timeout=2)==expected


def test_interject_uses_acp_extension_wire_prefix():
    client=ProtocolPeer()
    session=GrokSession(client,reviewed_interject=True)
    session.open(cwd='/workspace',model='grok-selected')
    session._collecting=True
    assert session.interject('change','id-1')['status']=='queued'
    assert client.calls[-1][0]=='_x.ai/interject'


@pytest.mark.parametrize('method', ['session/update', '_x.ai/session/update', '_x.ai/session_notification'])
def test_extended_native_updates_keep_event_identity(method):
    from workers_projects_runtime.native_team import project_native_events
    events=[]
    client=ProtocolPeer()
    session=GrokSession(client,event=events.append)
    session.open(cwd='/workspace',model='grok-selected')
    client.notification(method,{'sessionId':'native-exact','update':{'sessionUpdate':'subagent_spawned',
        'parent_session_id':'native-exact','child_session_id':'child-1','subagent_id':'child-1'},
        '_meta':{'eventId':'event-1'}})
    assert events[-1]['meta']=={'eventId':'event-1'}
    assert events[-1]['update']['subagent_id']=='child-1'
    assert events[-1]['method']==method
    projected = project_native_events('grok', events[-1])
    assert projected[0]['event_type']=='provider.child.started'
    assert projected[0]['payload']['providerEventRef']=='event-1'
    accepted = list(events)
    params = {'sessionId':'native-exact','update':{'sessionUpdate':'subagent_spawned',
        'parent_session_id':'native-exact','child_session_id':'child-1','subagent_id':'child-1'}}
    client.notification('_x.ai/session_notification', {**params, 'sessionId':'other-parent'})
    client.notification('_x.ai/session_notification', {**params, 'update':None})
    client.notification('_x.ai/session_notification', None)
    client.notification('x.ai/session_notification', params)
    client.notification('_x.ai/other_notification', params)
    assert events==accepted


def test_interject_partial_error_is_not_reported_as_queued():
    class PartialError(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method=='_x.ai/interject':
                return {'result':{'status':'queued'},'error':{'code':'failed'}}
            return super().request(method,params,timeout)
    session=GrokSession(PartialError(),reviewed_interject=True)
    session.open(cwd='/workspace',model='grok-selected')
    session._collecting=True
    with pytest.raises(AcpError,match='rejected'):
        session.interject('change','id-1')


@pytest.mark.parametrize("retained", [True, False])
def test_native_session_default_selects_only_exact_offered_model(retained):
    class AdvertisedPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == "session/new":
                result["configOptions"].append({"id": "model", "currentValue": "new-default",
                    "options": [{"value": "new-default"}, {"value": "grok-selected"}]})
            if method == "session/set_config_option" and params["configId"] == "model" and not retained:
                result["configOptions"][0]["currentValue"] = "new-default"
            return result
    client = AdvertisedPeer("new-default")
    session = GrokSession(client)
    if retained:
        session.open(cwd="/workspace", model="grok-selected")
    else:
        with pytest.raises(AcpError, match="exact configured model"):
            session.open(cwd="/workspace", model="grok-selected")
    assert ("session/set_config_option", {"sessionId": "native-exact", "configId": "model", "value": "grok-selected"}) in client.calls
    assert not any(method == "session/prompt" for method, _ in client.calls)


def test_native_model_change_cancels_and_cannot_be_accepted():
    class ChangedPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == "session/prompt":
                self.notification("session/update", {"sessionId": "native-exact", "update": {
                    "sessionUpdate": "config_option_update", "configOptions": [
                        {"id": "model", "currentValue": "other-model"}]}})
            return super().request(method, params, timeout)
    client = ChangedPeer()
    session = GrokSession(client)
    session.open(cwd="/workspace", model="grok-selected")
    with pytest.raises(AcpError, match="changed the exact configured model"):
        session.prompt("Synthetic task")
    assert ("session/cancel", {"sessionId": "native-exact"}) in client.calls
    with pytest.raises(AcpError, match="changed the exact configured model"):
        session.prompt("Cannot reuse changed session")


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_system_rules_use_native_session_authority_not_user_prompt(session_id):
    client = ProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id, rules='Synthetic system authority.')
    method, params = next((m, p) for m, p in client.calls if m in ('session/new', 'session/load'))
    assert params['_meta'] == {'rules': 'Synthetic system authority.',
                              **({'noReplay': True} if session_id else {})}
    assert session.prompt('Synthetic user request') == 'native output'
    assert client.calls[-1][1]['prompt'] == [{'type': 'text', 'text': 'Synthetic user request'}]


@pytest.mark.parametrize("handoff", [False, True])
def test_prompt_retains_typed_public_narration_through_native_tools(handoff):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    graph = {"version": 1, "tools": [{"name": "lc_transfer_to_specialist", "description": "Consult."}]}
    delivery = messaging_delivery_control(audio_eligible=True)
    first = json.dumps({"type": "assistant_response", "tool_name": None,
                        "voice": "eligible", "content": "I will check the current state."})
    final = json.dumps({"type": "tool_call" if handoff else "assistant_response",
                       "tool_name": "lc_transfer_to_specialist" if handoff else None,
                       "voice": "eligible", "content": "" if handoff else "The check is complete."})
    class ToolPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method != 'session/prompt':
                return super().request(method, params, timeout)
            for kind, text in [('agent_message_chunk', first[:85]),
                               ('agent_message_chunk', first[85:]), ('tool_call', ''),
                               ('agent_message_chunk', 'Private progress without a public envelope.'),
                               ('tool_call', ''), ('agent_message_chunk', final)]:
                self.notification('session/update', {'sessionId': 'native-exact', 'update': {
                    'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}})
            return {'stopReason': 'end_turn'}
    session = GrokSession(ToolPeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    output = session.prompt('Synthetic state check', output_schema=conversation_output_schema(graph, delivery))
    payload = json.loads(output)
    assert payload['content'] == 'I will check the current state.\n\n' + ('' if handoff else 'The check is complete.')
    assert 'Private progress' not in output
    assert payload['type'] == ('tool_call' if handoff else 'assistant_response')
    assert payload['tool_name'] == ('lc_transfer_to_specialist' if handoff else None)


@pytest.mark.parametrize("session_id", [None, "native-exact"])
def test_prompt_forwards_terminal_schema_and_preserves_pinned_authority(session_id):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control

    graph = {"version": 1, "tools": [{"name": "lc_transfer_to_specialist", "description": "Consult."}]}
    schema = conversation_output_schema(graph, messaging_delivery_control(audio_eligible=True))
    authority = "Synthetic authority.\n<viventium_feeling_state>synthetic</viventium_feeling_state>"

    class SchemaPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method != "session/prompt":
                return super().request(method, params, timeout)
            self.calls.append((method, params))
            self.notification("session/update", {
                "sessionId": "native-exact",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": json.dumps({
                        "type": "assistant_response", "tool_name": None,
                        "voice": "eligible", "content": "The work was accepted. Its result will follow.",
                    })},
                },
            })
            return {"stopReason": "end_turn"}

    client = SchemaPeer()
    session = GrokSession(client)
    session.open(cwd="/workspace", model="grok-selected", session_id=session_id,
                 effort="high", system_prompt_override=authority)
    output = session.prompt("Synthetic authorized request", output_schema=schema)
    opening = next(params for method, params in client.calls if method in ("session/new", "session/load"))
    assert opening["_meta"]["systemPromptOverride"] == authority
    prompt = next(params for method, params in client.calls if method == "session/prompt")
    assert prompt["_meta"]["outputSchema"] == schema
    assert "Returning type=assistant_response ends your work for this turn." in prompt["_meta"]["outputSchema"]["description"]
    assert json.loads(output)["content"] == "The work was accepted. Its result will follow."


@pytest.mark.parametrize("tail", [',"voice":"skip"}', ',"extra":true}', ''])
def test_public_prefix_cannot_finish_as_a_malformed_or_revised_control(tail):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    class MalformedPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method != 'session/prompt':
                return super().request(method, params, timeout)
            for text in ['{"type":"assistant_response","tool_name":null,"voice":"eligible","content":"Public text', '"'+tail]:
                self.notification('session/update', {'sessionId': 'native-exact', 'update': {
                    'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}})
            return {'stopReason': 'end_turn'}
    session = GrokSession(MalformedPeer())
    session.open(cwd='/workspace', model='grok-selected')
    with pytest.raises(AcpError, match='invalid public response envelope'):
        session.prompt('Synthetic test', output_schema=conversation_output_schema(None, messaging_delivery_control(audio_eligible=True)))


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
@pytest.mark.parametrize('yolo_mode', [None, False, True])
def test_native_permission_mode_preserves_exact_new_and_resumed_authority(session_id, yolo_mode):
    client = ProtocolPeer()
    session = GrokSession(client)
    authority = 'Exact synthetic authority. Pinned tail.'
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id, effort='high',
                 system_prompt_override=authority, yolo_mode=yolo_mode)
    expected_meta = {'systemPromptOverride': authority}
    if session_id:
        expected_meta['noReplay'] = True
    if yolo_mode is not None:
        expected_meta['yoloMode'] = yolo_mode
        if yolo_mode is False:
            expected_meta['autoMode'] = False
    opens = [params for method, params in client.calls if method in ('session/new', 'session/load')]
    assert len(opens) == 1
    assert opens[0]['_meta'] == expected_meta
    assert opens[0]['mcpServers'] == []
    assert session.session_id == 'native-exact'
    assert session.configured_model == 'grok-selected'


@pytest.mark.parametrize('invalid_mode', ['true', 1, {}])
def test_native_permission_mode_rejects_non_boolean_before_native_setup(invalid_mode):
    client = ProtocolPeer()
    with pytest.raises(AcpError, match='permission mode must be a boolean'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected', yolo_mode=invalid_mode)
    assert client.calls == []


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
@pytest.mark.parametrize('yolo_mode', [False, True])
def test_model_reconfiguration_retains_native_permission_mode_on_authority_reload(session_id, yolo_mode):
    class SwitchingPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/set_config_option' and params['configId'] == 'model':
                self.model = params['value']
            result = super().request(method, params, timeout)
            if method in ('session/new', 'session/load'):
                result['configOptions'].append({'id': 'model', 'currentValue': self.model,
                    'options': [{'value': 'old-model'}, {'value': 'grok-selected'}]})
            return result
    client = SwitchingPeer('old-model')
    session = GrokSession(client)
    authority = 'Exact synthetic authority. Pinned tail.'
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id, effort='high',
                 system_prompt_override=authority, yolo_mode=yolo_mode)
    expected_meta = {'systemPromptOverride': authority, 'yoloMode': yolo_mode}
    if yolo_mode is False:
        expected_meta['autoMode'] = False
    opens = [params for method, params in client.calls if method in ('session/new', 'session/load')]
    assert len(opens) == 2
    assert all(params['_meta'] == {**expected_meta,
               **({'noReplay': True} if 'sessionId' in params else {})} for params in opens)
    assert opens[-1]['sessionId'] == 'native-exact'
    assert session.configured_model == 'grok-selected'
