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


def test_exact_resume_model_effort_and_session_fencing():
    client = ProtocolPeer()
    session = GrokSession(client)
    session.open(cwd='/workspace', model='grok-selected', session_id='native-exact', effort='high')
    assert ('session/load', {'sessionId':'native-exact','cwd':'/workspace','mcpServers':[]}) in client.calls
    assert session.prompt('actual user task', timeout=2) == 'native output'
    assert client.calls[-1][1]['prompt'] == [{'type':'text','text':'actual user task'}]


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


def test_model_mismatch_never_silently_substitutes():
    client = ProtocolPeer('other-model')
    with pytest.raises(AcpError, match='model'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected')
    assert not any(method == 'session/prompt' for method, _ in client.calls)


def test_unsupported_effort_fails_before_prompt():
    client = ProtocolPeer()
    with pytest.raises(AcpError, match='effort'):
        GrokSession(client).open(cwd='/workspace', model='grok-selected', effort='ultra')


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


def test_extended_native_updates_keep_event_identity():
    events=[]
    client=ProtocolPeer()
    session=GrokSession(client,event=events.append)
    session.open(cwd='/workspace',model='grok-selected')
    client.notification('_x.ai/session/update',{'sessionId':'native-exact','update':{'sessionUpdate':'subagent_spawned','subagent_id':'child-1'},'_meta':{'eventId':'event-1'}})
    assert events[-1]['meta']=={'eventId':'event-1'}
    assert events[-1]['update']['subagent_id']=='child-1'


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
