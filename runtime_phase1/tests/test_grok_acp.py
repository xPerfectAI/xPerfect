from __future__ import annotations

import json
import subprocess
import sys
from threading import Event, Thread

import pytest

from workers_projects_runtime.grok_acp import AcpClient, AcpError, GrokSession


@pytest.mark.parametrize('second', ['The answer.', 'A useful addition.'])
@pytest.mark.parametrize('minimal_wire', [False, True])
def test_prompt_frames_complete_same_turn_native_generations(second, minimal_wire):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    def envelope(text):
        return json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': text})
    class GenerationPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            for stream, text in [(100, 'The answer.'), (200, second)]:
                self.notification('session/update', {'sessionId': 'native-exact', '_meta': {
                    'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream},
                    'update': {'sessionUpdate': 'agent_message_chunk',
                               'content': {'type': 'text', 'text': envelope(text)}}})
                self.notification('_x.ai/session_notification', {'sessionId': 'native-exact',
                    'update': {'sessionUpdate': 'response_completed'}})
            return {'stopReason': 'end_turn'}
    session = GrokSession(GenerationPeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    result = json.loads(session.prompt('Synthetic request', output_schema=conversation_output_schema(
        None, messaging_delivery_control(audio_eligible=True)), minimal_wire_output=minimal_wire))
    assert result['content'] == 'The answer.\n\n' + second
    assert result['voice'] == 'eligible'
    assert session.delivery_disposition_ranges() == [{'start': 0, 'end': len(result['content']),
        'disposition': {'version': 1, 'audio': 'eligible', 'required': True, 'valid': True, 'source': 'model'}}]


@pytest.mark.parametrize('change', ['prompt', 'turn', 'unfinished', 'invalid_voice', 'unavailable_graph'])
@pytest.mark.parametrize('minimal_wire', [False, True])
def test_prompt_rejects_unsupported_native_generation_controls(change, minimal_wire):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    class GenerationPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            for index, stream in enumerate([100, 200]):
                text = json.dumps({'type': 'tool_call' if index and change == 'unavailable_graph' else 'assistant_response',
                    'tool_name': 'unavailable_transfer' if index and change == 'unavailable_graph' else None,
                    'voice': 'invalid' if index and change == 'invalid_voice' else 'eligible', 'content': 'Synthetic answer.'})
                self.notification('session/update', {'sessionId': 'native-exact', '_meta': {
                    'promptId': 'foreign' if index and change == 'prompt' else 'prompt-exact',
                    'turnStartMs': 51 if index and change == 'turn' else 50, 'streamStartMs': stream},
                    'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}})
                if change != 'unfinished':
                    self.notification('_x.ai/session_notification', {'sessionId': 'native-exact',
                        'update': {'sessionUpdate': 'response_completed'}})
            return {'stopReason': 'end_turn'}
    session = GrokSession(GenerationPeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    with pytest.raises(AcpError):
        session.prompt('Synthetic request', output_schema=conversation_output_schema(
            graph, messaging_delivery_control(audio_eligible=True)), minimal_wire_output=minimal_wire)


@pytest.mark.parametrize('first_voice', ['eligible', 'skip'])
@pytest.mark.parametrize('final_voice', ['eligible', 'skip'])
@pytest.mark.parametrize('minimal_wire', [False, True])
def test_prompt_keeps_native_tool_retirement_separate_from_new_generation(first_voice, final_voice, minimal_wire):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    class ToolGenerationPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            def emit(kind, text='', stream=100):
                self.notification('session/update', {'sessionId': 'native-exact', '_meta': {
                    'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream},
                    'update': {'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}})
            envelope = lambda voice: json.dumps({'type': 'assistant_response', 'tool_name': None,
                                                 'voice': voice, 'content': 'Same public text.'})
            emit('agent_message_chunk', envelope(first_voice))
            emit('response_completed')
            for _ in range(2):
                emit('tool_call')
                emit('agent_thought_chunk', 'Private reasoning.')
                emit('response_completed')
            emit('agent_message_chunk', envelope(final_voice), 200)
            emit('response_completed', stream=200)
            return {'stopReason': 'end_turn'}
    session = GrokSession(ToolGenerationPeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    result = json.loads(session.prompt('Synthetic request', output_schema=conversation_output_schema(
        None, messaging_delivery_control(audio_eligible=True)), minimal_wire_output=minimal_wire))
    assert result == {'type': 'assistant_response', 'tool_name': None, 'voice': 'skip' if 'skip' in (first_voice, final_voice) else 'eligible',
                      'content': 'Same public text.\n\nSame public text.'}


@pytest.mark.parametrize('boundary', ['tool', 'generation'])
@pytest.mark.parametrize('kind,voice', [('assistant_response', 'skip'), ('tool_call', 'eligible'), ('tool_call', 'skip')])
@pytest.mark.parametrize('minimal_wire', [False, True])
def test_prompt_native_generation_terminal_preserves_existing_part_controls(boundary, kind, voice, minimal_wire):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    first = json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': 'I will check.'})
    final = {'type': kind, 'tool_name': 'lc_transfer_to_specialist' if kind == 'tool_call' else None,
             'voice': voice, 'content': 'The final contribution.'}
    class GenerationPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            def emit(kind, text='', stream=100):
                self.notification('session/update', {'sessionId': 'native-exact', '_meta': {
                    'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream},
                    'update': {'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}})
            emit('agent_message_chunk', first)
            emit('tool_call' if boundary == 'tool' else 'response_completed')
            emit('agent_message_chunk', json.dumps(final), 200 if boundary == 'generation' else 100)
            emit('response_completed', stream=200 if boundary == 'generation' else 100)
            return {'stopReason': 'end_turn'}
    session = GrokSession(GenerationPeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    output = json.loads(session.prompt('Synthetic request', output_schema=conversation_output_schema(
        graph, messaging_delivery_control(audio_eligible=True)), minimal_wire_output=minimal_wire))
    assert output == {**final, 'content': 'I will check.\n\nThe final contribution.'}


@pytest.mark.parametrize('voice', ['eligible', 'skip'])
@pytest.mark.parametrize('minimal_wire', [False, True])
def test_prompt_silent_predecessor_does_not_fail_optional_generation(voice, minimal_wire):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    class GenerationPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            for stream, audio, text in [(100, 'skip', 'Silent body.'), (200, voice, 'Later contribution.')]:
                self.notification('session/update', {'sessionId': 'native-exact', '_meta': {
                    'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}, 'update': {
                    'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps({
                        'type': 'assistant_response', 'tool_name': None, 'voice': audio, 'content': text})}}})
                self.notification('_x.ai/session_notification', {'sessionId': 'native-exact', 'update': {
                    'sessionUpdate': 'response_completed'}})
            return {'stopReason': 'end_turn'}
    session = GrokSession(GenerationPeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    output = json.loads(session.prompt('Synthetic request', output_schema=conversation_output_schema(
        None, messaging_delivery_control(audio_eligible=True)), minimal_wire_output=minimal_wire))
    assert output == {'type': 'assistant_response', 'tool_name': None, 'voice': 'skip',
                      'content': 'Silent body.\n\nLater contribution.'}
    assert [r['disposition']['audio'] for r in session.delivery_disposition_ranges()] == (['skip'] if voice == 'skip' else ['skip', 'eligible'])


@pytest.mark.parametrize('voice', ['eligible', 'skip'])
@pytest.mark.parametrize('minimal_wire', [False, True])
def test_graph_terminal_declines_pending_interject_and_preserves_main(voice, tmp_path, minimal_wire):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    from workers_projects_runtime.grok_control import ControlMailbox, submit_control
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    terminal = {'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist', 'voice': voice, 'content': 'Consulting.'}
    controls = {'run_id': 'run-exact', 'attempt_id': 'attempt-exact', 'session_id': 'native-exact',
                'action': 'interject', 'text': 'Owned background context.', 'request_fingerprint': 'b' * 64}
    class GenerationPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            self.calls.append((method, params))
            mailbox = ControlMailbox(tmp_path, run_id='run-exact', attempt_id='attempt-exact', session=session, event=lambda _: None)
            assert submit_control(tmp_path, controls, message_id='a' * 64, timeout=0)['status'] == 'pending'
            def emit(payload, stream):
                self.notification('session/update', {'sessionId': 'native-exact', '_meta': {
                    'promptId': 'prompt-exact', 'turnStartMs': 50, 'streamStartMs': stream}, 'update': {
                    'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps(payload)}}})
                self.notification('_x.ai/session_notification', {'sessionId': 'native-exact', 'update': {
                    'sessionUpdate': 'response_completed'}})
            emit(terminal, 100)
            mailbox.start()
            try:
                receipt = submit_control(tmp_path, controls, message_id='a' * 64)
                assert receipt['status'] == 'rejected' and 'graph transfer' in receipt['error']
                assert not any(method == '_x.ai/interject' for method, _ in self.calls)
                # A native interjection already queued before selection can still race
                # this terminal graph choice. It must not replace or fail valid Main.
                emit({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': 'Late addition.'}, 200)
                self.notification('session/update', {'sessionId': 'native-exact', 'update': {'sessionUpdate': 'tool_call'}})
            finally:
                mailbox.close()
            return {'stopReason': 'end_turn'}
    client = GenerationPeer()
    session = GrokSession(client, reviewed_interject=True)
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    output = json.loads(session.prompt('Synthetic request', output_schema=conversation_output_schema(
        graph, messaging_delivery_control(audio_eligible=True)), minimal_wire_output=minimal_wire))
    assert output == terminal
    assert session._output_error is None and session.cancel_requested is False
    assert len([method for method, _ in client.calls if method == 'session/prompt']) == 1


def test_graph_only_prompt_declines_interject_without_failing_normal_main():
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    terminal = {'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist', 'content': 'Consulting.'}
    class GraphPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            self.calls.append((method, params))
            # Decline before the first native output, not by inspecting its content.
            with pytest.raises(AcpError, match='output schema'):
                session.interject('Owned background input.', 'stable-input')
            self.notification('session/update', {'sessionId': 'native-exact', 'update': {
                'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps(terminal)}}})
            return {'stopReason': 'end_turn'}
    client = GraphPeer();session = GrokSession(client, reviewed_interject=True)
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    assert json.loads(session.prompt('Synthetic task.', output_schema=conversation_output_schema(graph, None))) == terminal
    assert not any(method in ('_x.ai/interject', 'session/cancel') for method, _ in client.calls)
    assert len([method for method, _ in client.calls if method == 'session/prompt']) == 1
    assert session._output_error is None


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
    def request(self, method, params, timeout=30, *, send_guard=None):
        if send_guard is not None:
            send_guard()
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


class SwitchingRestrictedProtocolPeer(RestrictedProtocolPeer):
    def __init__(self, *, restricted=True):
        super().__init__(() if restricted else ('read_file',),
                         () if restricted else ('read_file',))
        self.model = 'old-model'
        self.authority = None
        self.restricted = restricted

    def request(self, method, params, timeout=30):
        if method == 'session/set_config_option' and params['configId'] == 'model':
            self.model = params['value']
            self.authority = None  # Native model selection can rebuild its system head.
        if method in ('session/new', 'session/load'):
            self.authority = params.get('_meta', {}).get('systemPromptOverride')
        result = super().request(method, params, timeout)
        if method in ('session/new', 'session/load', 'session/set_config_option'):
            # The config-option owner returns current options for the selected model.
            result['configOptions'] = [
                {'id': 'model', 'currentValue': self.model,
                 'options': [{'value': 'old-model'}, {'value': 'grok-selected'}]},
                {'id': 'reasoning_effort', 'currentValue': self.effort,
                 'options': [{'value': 'medium'}, {'value': 'high'}]},
            ]
            result['models'] = {'currentModelId': self.model}
        if method == 'session/prompt':
            assert self.authority == 'Exact current synthetic authority.'
            assert self.model == 'grok-selected' and self.effort == 'high'
            if self.restricted:
                assert self.final_tools == ()
        return result


@pytest.mark.parametrize('restricted', [True, False])
def test_model_and_effort_selection_reapply_authority_once_before_prompt(restricted):
    client = SwitchingRestrictedProtocolPeer(restricted=restricted)
    session = GrokSession(client)
    session.open(cwd='/synthetic-native', model='grok-selected', effort='high',
                 system_prompt_override='Exact current synthetic authority.',
                 native_tools_disabled=restricted)
    selections = [(method, params.get('configId')) for method, params in client.calls
                  if method in ('session/set_config_option', 'session/load')]
    expected = [('session/set_config_option', 'model'),
                ('session/set_config_option', 'reasoning_effort'), ('session/load', None)]
    assert selections == (expected if restricted else [expected[0], expected[2], expected[1]])
    loads = [params for method, params in client.calls if method == 'session/load']
    assert len(loads) == 1
    assert loads[0]['sessionId'] == session.session_id == 'native-exact'
    assert loads[0]['_meta']['systemPromptOverride'] == 'Exact current synthetic authority.'
    assert loads[0]['_meta']['noReplay'] is True
    if restricted:
        assert loads[0]['mcpServers'] == []
        assert loads[0]['_meta']['agentProfile']['agentsMd'] is False
        assert loads[0]['_meta']['yoloMode'] is False
        assert session._native_tools_verified
    else:
        assert 'agentProfile' not in loads[0]['_meta']
    assert session.configured_model == 'grok-selected'
    assert session._options(session.session_state, 'reasoning_effort')['currentValue'] == 'high'
    assert session.prompt('Exact synthetic user task.') == 'native output'


def test_restricted_model_selection_keeps_intermediate_reload_for_missing_effort_options():
    class IncompletePeer(SwitchingRestrictedProtocolPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == 'session/set_config_option' and params['configId'] == 'model':
                result['configOptions'] = [result['configOptions'][0]]
            return result
    client = IncompletePeer()
    session = GrokSession(client)
    session.open(cwd='/synthetic-native', model='grok-selected', effort='high',
                 system_prompt_override='Exact current synthetic authority.', native_tools_disabled=True)
    assert len([method for method, _ in client.calls if method == 'session/load']) == 2
    assert session._native_tools_verified
    assert session.prompt('Exact synthetic user task.') == 'native output'


@pytest.mark.parametrize('drift', ['model', 'effort', 'session', 'tools'])
def test_restricted_deferred_reload_drift_never_prompts(drift):
    class DriftPeer(SwitchingRestrictedProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/load' and drift == 'tools':
                self.final_tools = ('read_file',)
            result = super().request(method, params, timeout)
            if method == 'session/load':
                if drift == 'model':
                    result['models']['currentModelId'] = 'other-model'
                elif drift == 'effort':
                    result['configOptions'][1]['currentValue'] = 'medium'
                elif drift == 'session':
                    result['sessionId'] = 'other-session'
            return result
    client = DriftPeer()
    session = GrokSession(client)
    with pytest.raises(AcpError):
        session.open(cwd='/synthetic-native', model='grok-selected', effort='high',
                     system_prompt_override='Exact current synthetic authority.', native_tools_disabled=True)
    assert not session._native_tools_verified
    assert not any(method == 'session/prompt' for method, _ in client.calls)
    with pytest.raises(AcpError):
        session.prompt('Exact synthetic user task.')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


@pytest.mark.parametrize('failure', ['rejected', 'effort', 'models', 'option', 'notification'])
def test_restricted_effort_failure_after_model_selection_never_prompts(failure):
    class FailedEffortPeer(SwitchingRestrictedProtocolPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == 'session/set_config_option' and params['configId'] == 'reasoning_effort':
                if failure == 'rejected':
                    raise AcpError('Synthetic native effort rejection')
                if failure == 'effort':
                    result['configOptions'][1]['currentValue'] = 'medium'
                elif failure == 'models':
                    result['models']['currentModelId'] = 'other-model'
                elif failure == 'option':
                    result.pop('models')
                    result['configOptions'][0]['currentValue'] = 'other-model'
                elif failure == 'notification':
                    self.notification('session/update', {'sessionId': 'native-exact', 'update': {
                        'sessionUpdate': 'config_option_update',
                        'configOptions': [{'id': 'model', 'currentValue': 'other-model'}]}})
            return result
    client = FailedEffortPeer()
    session = GrokSession(client)
    with pytest.raises(AcpError):
        session.open(cwd='/synthetic-native', model='grok-selected', effort='high',
                     system_prompt_override='Exact current synthetic authority.', native_tools_disabled=True)
    assert not session._native_tools_verified
    assert not any(method == 'session/prompt' for method, _ in client.calls)


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
                              **({'noReplay': True} if session_id else {'modelId': 'grok-selected'})}
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
    class RefusingPeer(ProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/set_config_option' and params['configId'] == 'model':
                self.calls.append((method, params))
                raise AcpError('Native model is unavailable', code=-32602)
            return super().request(method, params, timeout)
    client = RefusingPeer('other-model')
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
            if params['value'] not in self.offered:
                self.calls.append((method, params))
                raise AcpError('Native model is unavailable', code=-32602)
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
        def request(self, method, params, timeout=30, *, send_guard=None):
            if send_guard is not None:
                send_guard()
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
                              **({'noReplay': True} if session_id else {'modelId': 'grok-selected'})}
    assert session.prompt('Synthetic user request') == 'native output'
    assert client.calls[-1][1]['prompt'] == [{'type': 'text', 'text': 'Synthetic user request'}]


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize('minimal_wire', [False, True])
def test_prompt_retains_typed_public_narration_through_native_tools(handoff, minimal_wire):
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
    output = session.prompt('Synthetic state check', output_schema=conversation_output_schema(graph, delivery), minimal_wire_output=minimal_wire)
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
    else:
        expected_meta['modelId'] = 'grok-selected'
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
               **({'noReplay': True} if 'sessionId' in params else {'modelId': 'grok-selected'})} for params in opens)
    assert opens[-1]['sessionId'] == 'native-exact'
    assert session.configured_model == 'grok-selected'


@pytest.mark.parametrize("method,meta,expected", [
    ("session/prompt", {"outputSchema": {"type":"object", "description":"Synthetic private schema"}}, True),
    ("initialize", {"outputSchema": {"type":"object"}}, False),
    ("session/prompt", {"outputSchema": "unsupported"}, False),
    ("session/prompt", {"systemPromptOverride": "Synthetic private authority"}, False),
])
def test_rpc_timing_binds_only_actual_prompt_output_schema_hash(method, meta, expected):
    import hashlib
    process = peer('''
import sys,json
r=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'ok':True}}),flush=True)
''')
    events=[]
    with AcpClient(process, request_timing=events.append) as client:
        assert client.request(method, {"prompt":[{"type":"text","text":"Synthetic private request"}],
                                       "_meta":meta}, timeout=2)=={"ok":True}
    assert len(events)==1
    assert ("output_schema_sha256" in events[0]) is expected
    if expected:
        assert events[0]["output_schema_sha256"]==hashlib.sha256(json.dumps(meta["outputSchema"],
            sort_keys=True,separators=(',', ':'),ensure_ascii=False).encode()).hexdigest()
    assert "Synthetic" not in json.dumps(events)


def test_rpc_schema_hash_observation_remains_optional():
    process = peer('''
import sys,json
r=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'ok':True}}),flush=True)
''')
    with AcpClient(process) as client:
        assert client.request("session/prompt", {"_meta":{"outputSchema":{"type":"object"}}},timeout=2)=={"ok":True}


class FreshModelHintProtocolPeer(SwitchingRestrictedProtocolPeer):
    def __init__(self, *, honor=True, restricted=False):
        super().__init__(restricted=restricted)
        self.honor = honor

    def request(self, method, params, timeout=30):
        if method == 'session/new' and self.honor:
            hint = params.get('_meta', {}).get('modelId')
            if hint:
                self.model = hint
        return super().request(method, params, timeout)


def test_fresh_exact_model_hint_avoids_external_switch_and_authority_reload():
    client = FreshModelHintProtocolPeer()
    session = GrokSession(client)
    authority = 'Exact current synthetic authority.'
    servers = [{'name': 'synthetic-broker', 'command': 'synthetic', 'args': [], 'env': []}]
    session.open(cwd='/workspace', model='grok-selected', effort='high',
                 system_prompt_override=authority, mcp_servers=servers, yolo_mode=False)
    opening = next(params for method, params in client.calls if method == 'session/new')
    assert opening['_meta'] == {'modelId': 'grok-selected', 'systemPromptOverride': authority,
                               'yoloMode': False, 'autoMode': False}
    assert opening['mcpServers'] == servers
    assert not any(method == 'session/load' for method, params in client.calls)
    assert not any(method == 'session/set_config_option' and params['configId'] == 'model'
                   for method, params in client.calls)
    assert client.authority == authority and client.effort == 'high'
    assert session.configured_model == 'grok-selected'
    assert session.prompt('Synthetic user task.') == 'native output'


def test_ignored_fresh_hint_keeps_model_selection_and_exact_authority_reload():
    client = FreshModelHintProtocolPeer(honor=False)
    session = GrokSession(client)
    authority = 'Exact current synthetic authority.'
    session.open(cwd='/workspace', model='grok-selected', effort='high',
                 system_prompt_override=authority, yolo_mode=False)
    assert any(method == 'session/set_config_option' and params['configId'] == 'model'
               for method, params in client.calls)
    reloads = [params for method, params in client.calls if method == 'session/load']
    assert len(reloads) == 1
    assert reloads[0]['_meta'] == {'systemPromptOverride': authority, 'noReplay': True,
                                 'yoloMode': False, 'autoMode': False}
    assert client.authority == authority and client.effort == 'high'
    assert session.prompt('Synthetic user task.') == 'native output'


def test_resume_has_no_model_hint_and_retains_exact_current_authority():
    client = FreshModelHintProtocolPeer()
    client.model = 'grok-selected'
    session = GrokSession(client)
    authority = 'Exact synthetic refreshed authority. Pinned tail.'
    session.open(cwd='/workspace', model='grok-selected', session_id='native-exact', effort='high',
                 system_prompt_override=authority, yolo_mode=False)
    opening = next(params for method, params in client.calls if method == 'session/load')
    assert opening['sessionId'] == 'native-exact'
    assert opening['_meta'] == {'systemPromptOverride': authority, 'noReplay': True,
                               'yoloMode': False, 'autoMode': False}
    assert client.authority == authority and client.effort == 'high'
    assert session.session_id == 'native-exact'


def test_restricted_fresh_hint_keeps_final_empty_inventory_and_authority_guards():
    client = FreshModelHintProtocolPeer(restricted=True)
    session = GrokSession(client)
    authority = 'Exact synthetic restricted authority.'
    session.open(cwd='/workspace', model='grok-selected', effort='high',
                 system_prompt_override=authority, native_tools_disabled=True)
    openings = [params for method, params in client.calls if method in ('session/new', 'session/load')]
    assert len(openings) == 2
    assert openings[0]['_meta']['modelId'] == 'grok-selected'
    assert 'modelId' not in openings[1]['_meta']
    for params in openings:
        assert params['_meta']['agentProfile'] == {
            'name': 'native-tools-disabled', 'description': 'Native tools disabled',
            'tools': ['ToolSearch'], 'disallowedTools': ['search_tool', 'use_tool'],
            'agentsMd': False, 'discoverSkills': False}
        assert params['_meta']['systemPromptOverride'] == authority
        assert params['_meta']['yoloMode'] is False and params['_meta']['autoMode'] is False
        assert params['mcpServers'] == []
    assert session._native_tools_verified and not session._native_tools_invalid
    assert client.authority == authority and client.effort == 'high'


def test_unsupported_fresh_model_hint_still_refuses_before_prompt():
    class UnsupportedHintPeer(FreshModelHintProtocolPeer):
        def request(self, method, params, timeout=30):
            if method == 'session/set_config_option' and params['configId'] == 'model':
                self.calls.append((method, params))
                raise AcpError('Native rejected exact configured model', code=-32602)
            result = super().request(method, params, timeout)
            if method == 'session/new':
                result['configOptions'][0]['options'] = [{'value': 'old-model'}]
            return result
    client = UnsupportedHintPeer(honor=False)
    with pytest.raises(AcpError, match='exact configured model'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected', effort='high',
                                system_prompt_override='Exact synthetic authority.')
    assert not any(method == 'session/prompt' for method, params in client.calls)


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_initial_catalog_snapshot_does_not_veto_native_exact_model_recovery(session_id):
    class DelayedCatalogPeer(ActualAccountPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == ('session/load' if session_id else 'session/new') and not any(
                m == 'session/set_config_option' and p['configId'] == 'model'
                for m, p in self.calls
            ):
                # The native account refresh has not reached this first snapshot.
                result['configOptions'][-1]['options'] = [{'value': 'account-default'}]
            return result
    client = DelayedCatalogPeer('account-default', ['grok-4.7-build-fast'])
    session = GrokSession(client)
    authority = 'Exact synthetic authority.'
    session.open(cwd='/workspace', model='grok-4.7-build-fast', session_id=session_id,
                 effort='high', system_prompt_override=authority, yolo_mode=False)
    assert session.configured_model == 'grok-4.7-build-fast'
    changes = [p for m, p in client.calls if m == 'session/set_config_option' and p['configId'] == 'model']
    assert len(changes) == 1 and changes[0]['value'] == 'grok-4.7-build-fast'
    assert session._options(session.session_state, 'reasoning_effort')['currentValue'] == 'high'
    reloads = [p for m, p in client.calls if m == 'session/load']
    assert reloads[-1]['_meta']['systemPromptOverride'] == authority
    assert reloads[-1]['_meta']['yoloMode'] is False
    assert not any(m == 'session/prompt' for m, p in client.calls)
    assert session.prompt('Synthetic task.') == 'native output'


def test_fresh_model_hint_keeps_effort_model_drift_fence():
    class EffortDriftPeer(FreshModelHintProtocolPeer):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == 'session/set_config_option' and params['configId'] == 'reasoning_effort':
                result['models']['currentModelId'] = 'other-model'
            return result
    client = EffortDriftPeer()
    with pytest.raises(AcpError, match='reasoning effort changed'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected', effort='high',
                                system_prompt_override='Exact synthetic authority.')
    assert not any(method == 'session/prompt' for method, params in client.calls)


def test_ignored_fresh_fast_hint_preserves_authorized_catalog_fallback_and_authority():
    class MissingFastHintPeer(ActualAccountPeer):
        def request(self, method, params, timeout=30):
            if method in ('session/new', 'session/load'):
                self.authority = params.get('_meta', {}).get('systemPromptOverride')
            if method == 'session/set_config_option' and params['configId'] == 'model':
                self.authority = None
            return super().request(method, params, timeout)
    client = MissingFastHintPeer('account-default', ['grok-4.7'])
    session = GrokSession(client)
    authority = 'Exact current synthetic fallback authority.'
    session.open(cwd='/workspace', model='grok-4.7-build-fast', effort='high',
                 system_prompt_override=authority, yolo_mode=False)
    opening = next(params for method, params in client.calls if method == 'session/new')
    assert opening['_meta']['modelId'] == 'grok-4.7-build-fast'
    changes = [params for method, params in client.calls
               if method == 'session/set_config_option' and params['configId'] == 'model']
    assert len(changes) == 1 and changes[0]['value'] == 'grok-4.7'
    reloads = [params for method, params in client.calls if method == 'session/load']
    assert len(reloads) == 1
    assert reloads[0]['_meta'] == {'systemPromptOverride': authority, 'noReplay': True,
                                 'yoloMode': False, 'autoMode': False}
    assert client.authority == authority
    assert session.configured_model == 'grok-4.7'
    assert session.requested_model == 'grok-4.7-build-fast'
    assert session._options(session.session_state, 'reasoning_effort')['currentValue'] == 'high'
    assert session.prompt('Synthetic user task.') == 'native output'


def test_cancelled_collecting_session_cannot_interject():
    client = ProtocolPeer()
    session = GrokSession(client, reviewed_interject=True)
    session.open(cwd='/workspace', model='grok-selected')
    session._collecting = True
    session.cancel()
    with pytest.raises(AcpError, match='cancelled'):
        session.interject('Completed context', 'stable-1')
    assert not any(method == '_x.ai/interject' for method, _ in client.calls)


def test_cancel_overtakes_interjection_waiting_for_transport_write():
    from threading import Event, Lock, Thread
    from types import SimpleNamespace
    entered, cancel_entered = Event(), Event()
    wire, result = [], {}
    client = object.__new__(AcpClient)
    client._lock, client._write_lock = Lock(), Lock()
    client._pending, client._next_id, client._failure = {}, 0, None
    client.request_timing, client.max_frame_bytes = None, 1024 * 1024
    class Pipe:
        def write(self, encoded):
            message = json.loads(encoded)
            wire.append(message)
            if message.get('method') == '_x.ai/interject':
                client._pending[message['id']].set_result({'result': {'status': 'queued'}})
        def flush(self):
            pass
    client.process = SimpleNamespace(stdin=Pipe())
    def send(message, **kwargs):
        if message['method'] == '_x.ai/interject':
            entered.set()
        if message['method'] == 'session/cancel':
            cancel_entered.set()
        return AcpClient._send(client, message, **kwargs)
    client._send = send
    session = GrokSession(client, reviewed_interject=True)
    session.session_id, session._collecting = 'native-a', True
    def interject():
        try:
            result['receipt'] = session.interject('Context', 'stable-1')
        except AcpError as error:
            result['error'] = str(error)
    client._write_lock.acquire()
    operation = Thread(target=interject)
    cancellation = Thread(target=session.cancel)
    try:
        operation.start()
        assert entered.wait(1)
        cancellation.start()
        assert cancel_entered.wait(1)
    finally:
        client._write_lock.release()
        operation.join(2)
        cancellation.join(2)
    assert not operation.is_alive() and not cancellation.is_alive()
    assert 'cancelled' in result.get('error', '')
    assert [message['method'] for message in wire] == ['session/cancel']


@pytest.mark.parametrize('constrain', [True, False])
@pytest.mark.parametrize('terminal', ['assistant', 'transfer', 'invalid_pair', 'invalid_voice'])
def test_declared_contract_survives_native_constraint_transport_choice(constrain, terminal):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    schema = conversation_output_schema(graph, messaging_delivery_control(audio_eligible=True))
    envelope = {'type': 'tool_call' if terminal == 'transfer' else 'assistant_response',
                'tool_name': 'lc_transfer_to_specialist' if terminal in ('transfer', 'invalid_pair') else None,
                'voice': 'invalid' if terminal == 'invalid_voice' else 'eligible', 'content': 'Useful result.'}
    events = []
    class EnvelopePeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt':
                return super().request(method, params, timeout, **kwargs)
            self.calls.append((method, params))
            # Native functions use their own typed protocol before the final envelope.
            self.notification('session/update', {'sessionId': 'native-exact', 'update': {
                'sessionUpdate': 'tool_call', 'toolCallId': 'synthetic-tool', 'status': 'completed'}})
            self.notification('session/update', {'sessionId': 'native-exact', '_meta': {
                'promptId': 'synthetic-prompt', 'turnStartMs': 10, 'streamStartMs': 20}, 'update': {
                'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps(envelope)}}})
            return {'stopReason': 'end_turn'}
    client = EnvelopePeer();session = GrokSession(client, event=events.append)
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    if terminal in ('invalid_pair', 'invalid_voice'):
        with pytest.raises(AcpError, match='invalid public response envelope'):
            session.prompt('Synthetic request', output_schema=schema, minimal_wire_output=not constrain)
    else:
        output = session.prompt('Synthetic request', output_schema=schema, minimal_wire_output=not constrain)
        assert json.loads(output) == envelope
    request = next(params for method, params in reversed(client.calls) if method == 'session/prompt')
    assert '_meta' in request
    assert ('description' not in request['_meta']['outputSchema']) is (not constrain)
    assert session._interject_schema_supported is True
    assert session._conversation_parts.controls[0]['tools'][0]['name'] == 'lc_transfer_to_specialist'
    assert any(event.get('update', {}).get('sessionUpdate') == 'tool_call' for event in events)


@pytest.mark.parametrize('invalid', ['not_boolean', 'no_schema', 'unknown_schema', 'restricted'])
def test_minimal_wire_output_contract_rejects_unbound_or_restricted_controls_before_rpc(invalid):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    client = ProtocolPeer();session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected')
    if invalid == 'restricted':
        session._native_tools_disabled = True
    schema = conversation_output_schema(None, messaging_delivery_control(audio_eligible=True))
    if invalid == 'no_schema': schema = None
    if invalid == 'unknown_schema': schema = {'type': 'object'}
    before = len(client.calls)
    with pytest.raises(AcpError):
        session.prompt('Synthetic request', output_schema=schema, minimal_wire_output='false' if invalid == 'not_boolean' else True)
    assert len(client.calls) == before


@pytest.mark.parametrize('missing', ['schema', 'authority', 'native_tools', 'known_controls'])
def test_runner_minimal_wire_contract_requires_composed_admitted_inputs(tmp_path, monkeypatch, missing):
    from io import StringIO
    from workers_projects_runtime import grok_acp_runner as runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(sys, 'stdin', StringIO('Synthetic request.'))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Invalid input reached native launch'))
    schema = tmp_path / 'schema.json';schema.write_text(json.dumps(
        {'type': 'object'} if missing == 'known_controls' else conversation_output_schema(None, messaging_delivery_control(audio_eligible=True))))
    authority = tmp_path / 'authority.txt';authority.write_text('Synthetic authority.')
    args = ['--binary', 'synthetic-grok', '--model', 'grok-selected', '--output-schema-minimal-wire']
    if missing != 'schema': args += ['--output-schema-file', str(schema)]
    if missing != 'authority': args += ['--system-prompt-file', str(authority)]
    if missing == 'native_tools':
        workspace = tmp_path / 'empty';workspace.mkdir(mode=0o700)
        args += ['--native-tools-disabled', '--restricted-workspace', str(workspace)]
    with pytest.raises(ValueError, match='minimal-wire'):
        runner.main(args)


@pytest.mark.parametrize('minimal_wire', [False, True])
@pytest.mark.parametrize('cancel_after', [False, True])
def test_runner_keeps_declared_controls_with_minimal_wire_constraint(tmp_path, monkeypatch, capfd, minimal_wire, cancel_after):
    import hashlib
    from contextlib import contextmanager
    from io import StringIO
    from workers_projects_runtime import grok_acp_runner as runner
    from workers_projects_runtime.agent_builder_control import (
        conversation_output_schema, conversation_output_contract_instructions, messaging_delivery_control)
    schema = conversation_output_schema(None, messaging_delivery_control(audio_eligible=True))
    schema_file = tmp_path / 'schema.json';schema_file.write_text(json.dumps(schema))
    authority_file = tmp_path / 'authority.txt';authority_file.write_text(conversation_output_contract_instructions(schema))
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(sys, 'stdin', StringIO('Synthetic request.'))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *args, **kwargs: object())
    envelope = {'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': 'Useful result.'}
    class EnvelopePeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt': return super().request(method, params, timeout, **kwargs)
            self.calls.append((method, params))
            self.notification('session/update', {'sessionId': 'native-exact', 'update': {
                'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps(envelope)}}})
            return {'stopReason': 'end_turn'}
    client = EnvelopePeer()
    @contextmanager
    def fake_client(*args, **kwargs): yield client
    monkeypatch.setattr(runner, 'AcpClient', fake_client)
    args = ['--binary', 'synthetic-grok', '--model', 'grok-selected', '--effort', 'high',
            '--system-prompt-file', str(authority_file), '--output-schema-file', str(schema_file)]
    if minimal_wire: args += ['--output-schema-minimal-wire']
    if cancel_after:
        original_prompt = runner.GrokSession.prompt
        def stopped_prompt(session, *args, **kwargs):
            output = original_prompt(session, *args, **kwargs)
            session.cancel()
            return output
        monkeypatch.setattr(runner.GrokSession, 'prompt', stopped_prompt)
    assert runner.main(args) == (2 if cancel_after else 0)
    request = next(params for method, params in client.calls if method == 'session/prompt')
    assert '_meta' in request
    assert ('description' not in request['_meta']['outputSchema']) is minimal_wire
    events = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    contract = next(event for event in events if event['type'] == 'grok.output_contract')
    assert contract['mode'] == ('minimal_wire_constraint' if minimal_wire else 'native_constraint')
    assert contract['declared_schema_sha256'] == hashlib.sha256(json.dumps(schema,
        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    assert 'output_schema_sha256' not in contract
    assert contract['wire_schema_sha256'] == hashlib.sha256(json.dumps(request['_meta']['outputSchema'],
        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if cancel_after:
        assert not any(event['type'] == 'grok.result' for event in events)
        assert any(event.get('failure_class') == 'native_turn_cancelled' for event in events)
        return
    terminal = next(event for event in events if event['type'] == 'grok.result')
    assert json.loads(terminal['output']) == envelope
    assert terminal['deliveryDispositionRanges'] == [{'start': 0, 'end': len(envelope['content']),
        'disposition': {'version': 1, 'audio': 'eligible', 'required': True, 'valid': True, 'source': 'model'}}]
    assert 'Synthetic request.' not in json.dumps(events)


def test_graph_only_grok_command_reaches_runner_with_original_strict_metadata(tmp_path, monkeypatch, capfd):
    from pathlib import Path
    from contextlib import contextmanager
    from io import StringIO
    from types import SimpleNamespace
    from workers_projects_runtime import grok_acp_runner as runner
    from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
    from workers_projects_runtime.openclaw_runtime import RuntimeInfo
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    bundle = {'run_mode': 'conversation', 'developer_instructions': 'Synthetic authority.',
              'agent_builder_control': graph, 'provider_capabilities': {'native_tools': True}}
    worker = {'worker_id': 'synthetic-grok', 'owner_id': 'local', 'profile': 'grok-build',
              'model': 'grok-selected', 'workspace_dir': str(tmp_path / 'workspace'),
              'execution_mode': 'host', 'trusted_run_lane': 'conversation',
              'bootstrap_bundle_json': json.dumps(bundle)}
    runtime = HostGrokBuildRuntime(str(tmp_path / 'state'))
    runtime.sandbox = SimpleNamespace(home_mount='/workspace/home', box=None)
    info = RuntimeInfo('grok-build', 'grok-selected', '', None, None, None,
                       str(tmp_path / 'state'), str(tmp_path / 'workspace'), None)
    command = runtime._grok_command(worker, info, host=True)
    assert '--output-schema-minimal-wire' not in command
    schema = json.loads(Path(command[command.index('--output-schema-file') + 1]).read_text())
    assert 'voice' not in schema['properties']
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'native-home'))
    monkeypatch.setattr(sys, 'stdin', StringIO('Synthetic request.'))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *args, **kwargs: object())
    envelope = {'type': 'assistant_response', 'tool_name': None, 'content': 'Useful result.'}
    class EnvelopePeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method != 'session/prompt': return super().request(method, params, timeout, **kwargs)
            self.calls.append((method, params))
            self.notification('session/update', {'sessionId': 'native-exact', 'update': {
                'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': json.dumps(envelope)}}})
            return {'stopReason': 'end_turn'}
    client = EnvelopePeer()
    @contextmanager
    def fake_client(*args, **kwargs): yield client
    monkeypatch.setattr(runner, 'AcpClient', fake_client)
    assert runner.main(command[2:]) == 0
    request = next(params for method, params in client.calls if method == 'session/prompt')
    assert request['_meta']['outputSchema'] == schema
    events = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    assert next(event for event in events if event['type'] == 'grok.output_contract')['mode'] == 'native_constraint'
    assert json.loads(next(event for event in events if event['type'] == 'grok.result')['output']) == envelope


def test_minimal_wire_cancelled_turn_never_prompts_or_replays():
    from workers_projects_runtime.agent_builder_control import conversation_output_schema, messaging_delivery_control
    client = ProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    session.cancel()
    with pytest.raises(AcpError, match='cancelled before prompting'):
        session.prompt('Synthetic task.', output_schema=conversation_output_schema(
            None, messaging_delivery_control(audio_eligible=True)), minimal_wire_output=True)
    assert not any(method == 'session/prompt' for method, _ in client.calls)


class McpReadyPeer(ProtocolPeer):
    def __init__(self, initial=None):
        super().__init__()
        self.initial = initial

    def manifest(self, tools, identity='native-exact'):
        self.notification('session/update', {'sessionId': identity, 'update': {
            'sessionUpdate': 'available_commands_update', '_meta': {'tools': tools}}})

    def request(self, method, params, timeout=30, **kwargs):
        if method in ('session/new', 'session/load'):
            self.manifest(self.initial)
        return super().request(method, params, timeout, **kwargs)


MCP_SERVERS = [{'name': 'authorized-broker', 'type': 'stdio', 'command': 'synthetic'}]
REQUIRED_MCP_SERVERS = ['authorized-broker']
READY_BROKER_TOOLS = ['authorized-broker__capabilities_list']


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_required_mcp_inventory_waits_for_current_session_before_inference(session_id):
    client = McpReadyPeer(['read_file', 'search_tool'])
    session = GrokSession(client)
    waiting = Event()
    finished = Event()
    failures = []
    wait = session._native_mcp_observation.wait
    def observed_wait(timeout):
        assert timeout == 30
        waiting.set()
        return wait(timeout=2)
    session._native_mcp_observation.wait = observed_wait
    def open_session():
        try:
            session.open(cwd='/workspace', model='grok-selected', effort='high',
                         session_id=session_id, mcp_servers=MCP_SERVERS,
                         required_mcp_servers=REQUIRED_MCP_SERVERS)
        except Exception as exc:
            failures.append(exc)
        finally:
            finished.set()
    thread = Thread(target=open_session)
    thread.start()
    try:
        assert waiting.wait(timeout=1)
        assert not finished.is_set()
        client.manifest(READY_BROKER_TOOLS, 'foreign-session')
        assert not session._native_tools_manifest.is_set()
        client.manifest(['authorized-broker__'])
        assert not session._native_tools_manifest.is_set()
        client.manifest(['read_file', *READY_BROKER_TOOLS])
        assert finished.wait(timeout=1)
        assert failures == []
        assert not any(method == 'session/prompt' for method, _ in client.calls)
        assert session.prompt('Resume the same authorized work.') == 'native output'
        assert session.session_id == 'native-exact' and session.configured_model == 'grok-selected'
    finally:
        client.manifest(READY_BROKER_TOOLS)
        thread.join(timeout=3)


@pytest.mark.parametrize('tools', [None, [], ['read_file'], ['authorized-broker__'], ['foreign__active_work_action'],
                                 [*READY_BROKER_TOOLS, {'name': 'malformed'}]])
def test_missing_required_mcp_inventory_is_typed_before_prompt(monkeypatch, tools):
    session = GrokSession(McpReadyPeer(tools))
    monkeypatch.setattr(session._native_mcp_observation, 'wait', lambda timeout: False)
    with pytest.raises(AcpError) as error:
        session.open(cwd='/workspace', model='grok-selected', mcp_servers=MCP_SERVERS,
                     required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert error.value.code == -32000 and error.value.stop_reason == 'mcp_unavailable'
    assert not any(method == 'session/prompt' for method, _ in session.client.calls)


@pytest.mark.parametrize('servers', [None, [{'name': 'resource-only', 'type': 'stdio', 'command': 'synthetic'}]])
def test_no_required_tool_projection_adds_no_native_readiness_wait(monkeypatch, servers):
    session = GrokSession(ProtocolPeer())
    def unexpected_wait(*_args, **_kwargs):
        raise AssertionError('Optional or absent MCP introduced a tool barrier')
    monkeypatch.setattr(session._native_mcp_observation, 'wait', unexpected_wait)
    session.open(cwd='/workspace', model='grok-selected', mcp_servers=servers)
    assert session.prompt('Synthetic ordinary request.') == 'native output'


def test_ready_current_mcp_manifest_proceeds_without_waiting(monkeypatch):
    session = GrokSession(McpReadyPeer(READY_BROKER_TOOLS))
    monkeypatch.setattr(session._native_mcp_observation, 'wait',
                        lambda timeout: session._native_tools_manifest.is_set())
    session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                 mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert session.prompt('Continue.') == 'native output'


def test_required_mcp_rechecks_model_reload_and_missing_final_inventory(monkeypatch):
    class ReloadPeer(McpReadyPeer):
        def __init__(self):
            super().__init__(READY_BROKER_TOOLS)
            self.model = 'old-model'
        def request(self, method, params, timeout=30, **kwargs):
            if method == 'session/load':
                self.initial = ['read_file']
            if method == 'session/set_config_option' and params['configId'] == 'model':
                self.model = params['value']
            result = super().request(method, params, timeout, **kwargs)
            if method in ('session/new', 'session/load'):
                result['configOptions'].append({'id': 'model', 'currentValue': self.model,
                    'options': [{'value': 'old-model'}, {'value': 'grok-selected'}]})
            return result
    session = GrokSession(ReloadPeer())
    monkeypatch.setattr(session._native_mcp_observation, 'wait',
                        lambda timeout: session._native_tools_manifest.is_set())
    with pytest.raises(AcpError, match='did not become ready'):
        session.open(cwd='/workspace', model='grok-selected', mcp_servers=MCP_SERVERS,
                     required_mcp_servers=REQUIRED_MCP_SERVERS, system_prompt_override='Current authority.')
    assert not any(method == 'session/prompt' for method, _ in session.client.calls)


def test_required_mcp_inventory_disappearing_after_open_blocks_prompt(monkeypatch):
    client = McpReadyPeer(READY_BROKER_TOOLS)
    session = GrokSession(client)
    monkeypatch.setattr(session._native_mcp_observation, 'wait',
                        lambda timeout: session._native_tools_manifest.is_set())
    session.open(cwd='/workspace', model='grok-selected', mcp_servers=MCP_SERVERS,
                 required_mcp_servers=REQUIRED_MCP_SERVERS)
    client.manifest(['read_file'])
    with pytest.raises(AcpError, match='did not become ready'):
        session.prompt('Continue.')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_mcp_readiness_failure_preserves_typed_runner_and_runtime_contract():
    from workers_projects_runtime.grok_acp_runner import _stopped_turn
    from workers_projects_runtime.failure_classification import classify_cli_failure
    error = AcpError('Private diagnostic is not the user message', code=-32000, stop_reason='mcp_unavailable')
    failure, message = _stopped_turn(error, GrokSession(ProtocolPeer()), None)
    assert failure == 'native_mcp_unavailable'
    result = classify_cli_failure(stdout=json.dumps({'type': 'grok.error', 'failure_class': failure,
        'message': 'synthetic untrusted diagnostic'}), stderr='', runtime_name='grok-build', exit_code=2)
    assert result.structured and result.failure_class == failure and result.retryable is False
    assert result.user_message == 'The configured tool connection was not ready before the model turn.'


@pytest.mark.parametrize('required', [['foreign'], ['authorized-broker__'], [{}]])
def test_required_mcp_identity_cannot_expand_projected_servers(required):
    client = ProtocolPeer()
    with pytest.raises(AcpError, match='identity is not configured'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected',
                                mcp_servers=MCP_SERVERS, required_mcp_servers=required)
    assert client.calls == []


def test_cancelled_mcp_readiness_does_not_become_a_ready_turn(monkeypatch):
    session = GrokSession(McpReadyPeer(['read_file']))
    def cancel_wait(timeout):
        session.cancel()
        return session._native_tools_manifest.is_set()
    monkeypatch.setattr(session._native_mcp_observation, 'wait', cancel_wait)
    with pytest.raises(AcpError) as error:
        session.open(cwd='/workspace', model='grok-selected', mcp_servers=MCP_SERVERS,
                     required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert error.value.stop_reason == 'cancelled'
    assert not any(method == 'session/prompt' for method, _ in session.client.calls)



def test_broker_connection_readiness_allows_policy_omitted_control_tools():
    client = McpReadyPeer(['read_file', 'authorized-broker__capabilities_list'])
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', mcp_servers=MCP_SERVERS,
                 required_mcp_servers=['authorized-broker'])
    assert session.prompt('Use only the granted capabilities.') == 'native output'
    assert 'authorized-broker__active_work_action' not in client.initial


# Native completion usage is bounded to the current session and prompt.
NATIVE_RESPONSE_USAGE = {'input_tokens': 147824, 'output_tokens': 614,
    'cache_read_input_tokens': 1152, 'cache_creation_input_tokens': 0, 'reasoning_tokens': 591}


@pytest.mark.parametrize('usage,expected', [
    (NATIVE_RESPONSE_USAGE, {'prompt_tokens': 148976, 'completion_tokens': 614, 'total_tokens': 149590}),
    ({'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 12,
      'cache_creation_input_tokens': 3}, {'prompt_tokens': 15, 'completion_tokens': 0, 'total_tokens': 15}),
    ({'input_tokens': 0, 'output_tokens': 0}, {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}),
    (None, None), ({}, None), ([], None),
    ({'input_tokens': -1, 'output_tokens': 2}, None),
    ({'input_tokens': True, 'output_tokens': 2}, None),
    ({'input_tokens': '12', 'output_tokens': 2}, None),
    ({'input_tokens': 12.5, 'output_tokens': 2}, None),
    ({'input_tokens': 12, 'output_tokens': 2, 'cache_read_input_tokens': None}, None),
    ({'input_tokens': 12, 'output_tokens': False}, None),
])
def test_native_response_usage_preserves_cache_and_reasoning_semantics(usage, expected):
    from workers_projects_runtime.grok_acp import _native_response_usage
    assert _native_response_usage(usage) == expected


@pytest.mark.parametrize('model_calls,expected', [
    (1, {'prompt_tokens': 148976, 'completion_tokens': 614, 'total_tokens': 149590}),
    (2, None), (0, None), (True, None), ('1', None), (None, None),
])
def test_turn_usage_fallback_never_counts_cache_or_tool_loops_twice(model_calls, expected):
    from workers_projects_runtime.grok_acp import _native_single_call_turn_usage
    assert _native_single_call_turn_usage({'inputTokens': 148976, 'outputTokens': 614,
        'cachedReadTokens': 1152, 'cacheCreationTokens': 0, 'reasoningTokens': 591,
        'modelCalls': model_calls}) == expected


@pytest.mark.parametrize('updates,expected', [
    ([('response_completed', NATIVE_RESPONSE_USAGE)], 148976),
    ([('response_completed', {'input_tokens': 10, 'output_tokens': 2}),
      ('response_completed', NATIVE_RESPONSE_USAGE),
      ('turn_completed', {'inputTokens': 999999, 'outputTokens': 616, 'modelCalls': 2})], 148976),
    ([('response_completed', NATIVE_RESPONSE_USAGE),
      ('response_completed', {'input_tokens': 'bad', 'output_tokens': 2})], None),
    ([('turn_completed', {'inputTokens': 148976, 'outputTokens': 614,
                         'cachedReadTokens': 1152, 'modelCalls': 1})], 148976),
    ([('turn_completed', {'inputTokens': 148976, 'outputTokens': 614, 'modelCalls': 2})], None),
])
def test_usage_is_the_latest_current_prompt_measurement(updates, expected):
    class UsagePeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            result = super().request(method, params, timeout, **kwargs)
            if method == 'session/prompt':
                for kind, usage in updates:
                    self.notification('session/update', {'sessionId': 'native-exact',
                        'update': {'sessionUpdate': kind, 'usage': usage}})
            return result
    session = GrokSession(UsagePeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    session.prompt('Synthetic request')
    assert (session.native_usage or {}).get('prompt_tokens') == expected
    retained = session.native_usage
    # Foreign sessions and late current-session notifications cannot replace it.
    for identity in ['foreign', 'native-exact']:
        session._update('session/update', {'sessionId': identity,
            'update': {'sessionUpdate': 'response_completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}})
    assert session.native_usage == retained


def test_new_prompt_does_not_reuse_previous_usage_and_foreign_active_update_is_ignored():
    class UsagePeer(ProtocolPeer):
        generation = 0
        def request(self, method, params, timeout=30, **kwargs):
            result = super().request(method, params, timeout, **kwargs)
            if method == 'session/prompt':
                self.generation += 1
                self.notification('session/update', {'sessionId': 'foreign',
                    'update': {'sessionUpdate': 'response_completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}})
                if self.generation == 1:
                    self.notification('session/update', {'sessionId': 'native-exact',
                        'update': {'sessionUpdate': 'response_completed', 'usage': NATIVE_RESPONSE_USAGE}})
            return result
    session = GrokSession(UsagePeer())
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    session._update('session/update', {'sessionId': 'native-exact',
        'update': {'sessionUpdate': 'response_completed', 'usage': NATIVE_RESPONSE_USAGE}})
    assert session.native_usage is None
    session.prompt('First synthetic request')
    assert session.native_usage['prompt_tokens'] == 148976
    session.prompt('Second synthetic request')
    assert session.native_usage is None


def test_interjection_keeps_latest_response_usage_without_readding_turn_total():
    class InterjectionPeer(ProtocolPeer):
        def request(self, method, params, timeout=30, **kwargs):
            result = super().request(method, params, timeout, **kwargs)
            if method == 'session/prompt':
                self.notification('session/update', {'sessionId': 'native-exact',
                    'update': {'sessionUpdate': 'response_completed',
                               'usage': {'input_tokens': 20, 'output_tokens': 3}}})
                session.interject('One synthetic clarification', 'synthetic-interjection')
                self.notification('session/update', {'sessionId': 'native-exact',
                    'update': {'sessionUpdate': 'response_completed', 'usage': NATIVE_RESPONSE_USAGE}})
                self.notification('session/update', {'sessionId': 'native-exact',
                    'update': {'sessionUpdate': 'turn_completed',
                               'usage': {'inputTokens': 148996, 'outputTokens': 617, 'modelCalls': 2}}})
            return result
    session = GrokSession(InterjectionPeer(), reviewed_interject=True)
    session.open(cwd='/workspace', model='grok-selected', effort='high')
    session.prompt('Synthetic request')
    assert session.native_usage == {'prompt_tokens': 148976, 'completion_tokens': 614, 'total_tokens': 149590}


def test_native_runner_emits_one_usage_from_latest_response_without_tool_loop_double_count(tmp_path):
    import os
    import workers_projects_runtime.grok_acp_runner as runner
    fake = tmp_path / 'native-peer'
    fake.write_text(f'#!{sys.executable}\n' + '''import sys,json
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');params=m.get('params',{});result={}
 def emit(kind,usage=None):
  update={'sessionUpdate':kind}
  if usage is not None:update['usage']=usage
  if kind=='agent_message_chunk':update['content']={'type':'text','text':'Native answer.'}
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-exact','update':update}}),flush=True)
 if method=='initialize':result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'defaultAuthMethodId':'cached_token'}}
 elif method=='session/new':result={'sessionId':'native-exact','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  emit('agent_message_chunk')
  emit('response_completed',{'input_tokens':10,'cache_read_input_tokens':2,'output_tokens':3})
  emit('response_completed',{'input_tokens':20,'cache_read_input_tokens':4,'cache_creation_input_tokens':1,'output_tokens':5,'reasoning_tokens':4})
  emit('turn_completed',{'inputTokens':37,'outputTokens':8,'modelCalls':2})
  result={'stopReason':'end_turn'}
 if 'id' in m:print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    process = subprocess.run([sys.executable, runner.__file__, '--binary', str(fake),
        '--model', 'grok-selected'], input='Synthetic request', text=True, capture_output=True,
        timeout=10, env={**os.environ, 'GROK_HOME': str(tmp_path / 'native-home')})
    assert process.returncode == 0, process.stderr
    events = [json.loads(line) for line in process.stdout.splitlines()]
    measured = [event for event in events if isinstance(event.get('usage'), dict)]
    assert len(measured) == 1
    assert measured[0]['type'] == 'grok.result'
    assert measured[0]['usage'] == {'prompt_tokens': 25, 'completion_tokens': 5, 'total_tokens': 30}


@pytest.mark.parametrize('method,fields,expected', [
    ('_x.ai/mcp/server_status', {'name': 'broker', 'status': 'ready', 'tools': None}, {'server': 'broker', 'status': 'ready'}),
    ('_x.ai/mcp/init_progress', {'total': 1, 'connected': 1}, {'total': 1, 'connected': 1}),
    ('_x.ai/mcp_initialized', {'mcpToolCount': 23, 'elapsedMs': 65}, {'mcpToolCount': 23, 'elapsedMs': 65}),
    ('_x.ai/mcp/tools_changed', {'serverName': 'broker', 'tools': ['private-tool-name']}, {'server': 'broker', 'tool_count': 1}),
])
def test_mcp_lifecycle_events_keep_only_current_redacted_metadata(method, fields, expected):
    events = []
    session = GrokSession(ProtocolPeer(), event=events.append)
    session.session_id = 'current-session'
    session._required_mcp_servers = ('broker',)
    session._update(method, {'sessionId': 'current-session', **fields,
                            'url': 'private-url', 'headers': {'Authorization': 'private-bearer'},
                            'reason': 'private-reason', 'arguments': 'private-arguments'})
    assert events == [{'type': 'grok.mcp.lifecycle', 'session_id': 'current-session',
                       'method': method, **expected}]
    assert not session._native_tools_manifest.is_set()
    assert 'private' not in json.dumps(events)


@pytest.mark.parametrize('params', [
    {'sessionId': 'foreign-session', 'name': 'broker', 'status': 'ready'},
    {'sessionId': 'current-session', 'name': 'foreign-server', 'status': 'ready'},
    {'sessionId': 'current-session', 'name': 'broker', 'status': 'private-status'},
])
def test_mcp_lifecycle_rejects_foreign_identity_and_unknown_status(params):
    events = []
    session = GrokSession(ProtocolPeer(), event=events.append)
    session.session_id = 'current-session'
    session._required_mcp_servers = ('broker',)
    session._update('_x.ai/mcp/server_status', params)
    assert events == []


def test_mcp_lifecycle_rejects_malformed_counts_without_affecting_readiness():
    events = []
    session = GrokSession(ProtocolPeer(), event=events.append)
    session.session_id = 'current-session'
    session._update('_x.ai/mcp_initialized', {'sessionId': 'current-session', 'mcpToolCount': True,
                                             'elapsedMs': -1, 'connected': 'private-count'})
    assert events == [{'type': 'grok.mcp.lifecycle', 'session_id': 'current-session',
                       'method': '_x.ai/mcp_initialized'}]
    assert not session._native_tools_manifest.is_set()


def native_ready_registry(name='authorized-broker', **changes):
    state = {'enabled': True, 'status': 'ready', 'tools': [{'name': 'active_work_list', 'enabled': True}]}
    state.update(changes)
    return {'result': {'sessionMcpResolved': True, 'servers': [{'name': name, 'session': state}]}}


class NativeRegistryPeer(McpReadyPeer):
    def __init__(self, registry):
        super().__init__(['read_file', 'search_tool'])
        self.registry = registry
    def request(self, method, params, timeout=30, **kwargs):
        if method == '_x.ai/mcp/list':
            self.calls.append((method, params))
            assert 0 < timeout <= 30
            return self.registry
        result = super().request(method, params, timeout, **kwargs)
        if method in ('session/load', 'session/new'):
            self.notification('_x.ai/mcp_initialized', {'sessionId': 'native-exact', 'mcpToolCount': 23, 'elapsedMs': 65})
        return result


@pytest.mark.parametrize('session_id', [None, 'native-exact'])
def test_live_native_registry_admits_required_tools_without_cosmetic_catalogue(session_id):
    client = NativeRegistryPeer(native_ready_registry())
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', session_id=session_id,
                 mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert not session._native_tools_manifest.is_set()
    assert [call for call in client.calls if call[0] == '_x.ai/mcp/list'] == [
        ('_x.ai/mcp/list', {'sessionId': 'native-exact', 'cache': True})]
    assert session.prompt('Synthetic allowed goal.') == 'native output'


@pytest.mark.parametrize('registry', [
    {}, {'result': None}, {'error': 'synthetic', 'result': native_ready_registry()['result']},
    {'result': {'sessionMcpResolved': False, 'servers': native_ready_registry()['result']['servers']}},
    native_ready_registry(name='foreign-broker'), native_ready_registry(enabled=False),
    native_ready_registry(status='initializing'), native_ready_registry(tools=[]),
    native_ready_registry(tools=[{'name': 'active_work_list', 'enabled': False}]),
    native_ready_registry(tools=[{'name': '', 'enabled': True}]),
    native_ready_registry(tools=[{'name': 'active_work_list', 'enabled': 'true'}]),
    native_ready_registry(authRequired=True), native_ready_registry(setupRequired=True),
    {'result': {'sessionMcpResolved': True, 'servers': native_ready_registry()['result']['servers'] * 2}},
])
def test_live_native_registry_fails_closed_for_missing_foreign_disabled_or_zero_tools(monkeypatch, registry):
    client = NativeRegistryPeer(registry)
    session = GrokSession(client)
    monkeypatch.setattr(session._native_tools_manifest, 'wait', lambda timeout: False)
    with pytest.raises(AcpError) as error:
        session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                     mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert error.value.stop_reason == 'mcp_unavailable'
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_native_registry_inspection_keeps_shared_deadline_and_cancellation(monkeypatch):
    client = NativeRegistryPeer(native_ready_registry())
    session = GrokSession(client)
    def cancel_request(method, params, timeout=30, **kwargs):
        if method == '_x.ai/mcp/list':
            session.cancel_requested = True
        return NativeRegistryPeer.request(client, method, params, timeout, **kwargs)
    client.request = cancel_request
    with pytest.raises(AcpError) as error:
        session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                     mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert error.value.stop_reason == 'cancelled'
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_native_registry_is_not_queried_for_ready_cosmetic_inventory():
    client = McpReadyPeer(READY_BROKER_TOOLS)
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                 mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert not any(method == '_x.ai/mcp/list' for method, _ in client.calls)


@pytest.mark.parametrize('identity,count', [('foreign-session', 23), ('', 23),
    ('native-exact', True), ('native-exact', -1), ('native-exact', '23')])
def test_registry_inspection_requires_current_typed_initialization(monkeypatch, identity, count):
    class InvalidInitPeer(McpReadyPeer):
        def request(self, method, params, timeout=30, **kwargs):
            result = super().request(method, params, timeout, **kwargs)
            if method in ('session/load', 'session/new'):
                self.notification('_x.ai/mcp_initialized', {'sessionId': identity, 'mcpToolCount': count})
            return result
    client = InvalidInitPeer(['read_file'])
    session = GrokSession(client)
    monkeypatch.setattr(session._native_mcp_observation, 'wait', lambda timeout: False)
    with pytest.raises(AcpError) as error:
        session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                     mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
    assert error.value.stop_reason == 'mcp_unavailable'
    assert not any(method in ('_x.ai/mcp/list', 'session/prompt') for method, _ in client.calls)


def test_native_registry_inspection_cannot_extend_readiness_deadline(monkeypatch):
    import workers_projects_runtime.grok_acp as acp_module
    client = NativeRegistryPeer(native_ready_registry())
    session = GrokSession(client)
    session.session_id = 'native-exact'
    session._required_mcp_servers = tuple(REQUIRED_MCP_SERVERS)
    session._native_mcp_initialized = True
    session._native_mcp_observation.set()
    ticks = iter([10.0, 40.0])
    monkeypatch.setattr(acp_module.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(AcpError) as error:
        session._require_mcp_tools()
    assert error.value.stop_reason == 'mcp_unavailable'
    assert client.calls == []


@pytest.mark.parametrize('cosmetic_ready', [False, True])
def test_unsupported_registry_inspection_preserves_remaining_cosmetic_deadline(monkeypatch, cosmetic_ready):
    class UnsupportedRegistryPeer(NativeRegistryPeer):
        def request(self, method, params, timeout=30, **kwargs):
            if method == '_x.ai/mcp/list':
                self.calls.append((method, params))
                raise AcpError('Unsupported native extension', code=-32601)
            return super().request(method, params, timeout, **kwargs)
    client = UnsupportedRegistryPeer(native_ready_registry())
    session = GrokSession(client)
    observed_budget = []
    def remaining_wait(timeout):
        assert 0 < timeout <= 30
        observed_budget.append(timeout)
        if cosmetic_ready:
            client.manifest(READY_BROKER_TOOLS)
        return session._native_tools_manifest.is_set()
    monkeypatch.setattr(session._native_tools_manifest, 'wait', remaining_wait)
    if cosmetic_ready:
        session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                     mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
    else:
        with pytest.raises(AcpError) as error:
            session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                         mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
        assert error.value.stop_reason == 'mcp_unavailable'
    assert len(observed_budget) == 1
    assert len([method for method, _ in client.calls if method == '_x.ai/mcp/list']) == 1
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_actual_cancel_wakes_blocked_mcp_observation_without_registry_or_prompt():
    client = McpReadyPeer(['read_file'])
    session = GrokSession(client)
    waiting = Event()
    finished = Event()
    failures = []
    wait = session._native_mcp_observation.wait
    def observed_wait(timeout):
        waiting.set()
        return wait(timeout)
    session._native_mcp_observation.wait = observed_wait
    def open_session():
        try:
            session.open(cwd='/workspace', model='grok-selected', session_id='native-exact',
                         mcp_servers=MCP_SERVERS, required_mcp_servers=REQUIRED_MCP_SERVERS)
        except AcpError as error:
            failures.append(error.stop_reason)
        finally:
            finished.set()
    thread = Thread(target=open_session)
    thread.start()
    try:
        assert waiting.wait(timeout=1)
        assert not finished.is_set()
        session.cancel()
        assert finished.wait(timeout=1)
        assert failures == ['cancelled']
        assert not any(method in ('_x.ai/mcp/list', 'session/prompt') for method, _ in client.calls)
    finally:
        session.cancel()
        thread.join(timeout=2)
