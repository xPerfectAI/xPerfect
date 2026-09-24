import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from workers_projects_runtime import native_input
from workers_projects_runtime.native_mcp_tool_filter import ToolFilter


def context(tmp_path, monkeypatch):
    path=tmp_path/'native-input-context.json'
    value={'version':1,'workerId':'wrk_context','runId':'run_context','contextToken':'private-fixture-token','nativeSessionId':'actual-native-session','nativeTurnId':'actual-user-frame','nativeSessionActive':True}
    native_input.publish(path,value)
    monkeypatch.setenv('GLASSHIVE_NATIVE_INPUT_CONTEXT',str(path))
    monkeypatch.setenv('GLASSHIVE_NATIVE_INPUT_TOKEN',value['contextToken'])
    return path,value


def test_native_context_is_owner_issued_and_keeps_other_metadata(tmp_path,monkeypatch):
    context(tmp_path,monkeypatch)
    projection=ToolFilter({'enabled_tools':['js'],'disabled_tools':[]},native_context=True)
    original={'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'js','arguments':{'code':'read only'},'_meta':{'progressToken':'progress-4','x-codex-turn-metadata':'untrusted caller context'}}}
    forwarded,error=projection.client(original)
    assert error is None
    assert forwarded['params']['_meta']['x-codex-turn-metadata']=={'session_id':'actual-native-session','turn_id':'actual-user-frame'}
    assert forwarded['params']['_meta']['progressToken']=='progress-4'
    assert original['params']['_meta']['x-codex-turn-metadata']=='untrusted caller context'
    denied,error=projection.client({'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'excluded'}})
    assert denied is None and error['error']['code']==-32602


@pytest.mark.parametrize('fault',['foreign_token','ended','missing_session','symlink'])
def test_native_context_rejects_unbound_or_ended_invocation(tmp_path,monkeypatch,fault):
    path,value=context(tmp_path,monkeypatch)
    if fault=='foreign_token':value['contextToken']='different'
    elif fault=='ended':value['nativeSessionActive']=False
    elif fault=='missing_session':value.pop('nativeSessionId')
    if fault=='symlink':
        path.rename(tmp_path/'real.json');path.symlink_to(tmp_path/'real.json')
    else:native_input.publish(path,value)
    with pytest.raises((ValueError,native_input.NativeInputError)):
        ToolFilter({'enabled_tools':['js'],'disabled_tools':[]},native_context=True).client({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'js'}})


def test_original_elicitation_metadata_and_hook_response_remain_intact(tmp_path,monkeypatch):
    context(tmp_path,monkeypatch)
    projection=ToolFilter({'enabled_tools':['js'],'disabled_tools':[]},native_context=True)
    meta={'tool_name':'getAXState','tool_title':'Read app state','connector_name':'Computer Use','tool_params':{'app':'com.example.fixture'},'persist':['session'],'riskLevel':'low'}
    request={'jsonrpc':'2.0','id':'native-approval','method':'elicitation/create','params':{'message':'Native approval.','mode':'form','requestedSchema':{'type':'object','properties':{}},'_meta':meta}}
    forwarded=projection.child(request)
    assert {k:v for k,v in forwarded['params']['_meta'].items() if k!='anthropic/permissionDisplay'}==meta
    assert forwarded['params']['_meta']['anthropic/permissionDisplay']=={'title':'Read app state','displayName':'Computer Use'}
    assert request['params']['_meta']==meta
    # The final native policy/hook response passes back unchanged, including decline.
    response={'jsonrpc':'2.0','id':'native-approval','result':{'action':'decline','_meta':{'native-policy':'kept'}}}
    assert projection.client(response)==(response,None)
    existing={'title':'Server-owned title'}
    request['params']['_meta']={**meta,'anthropic/permissionDisplay':existing}
    assert projection.child(request)['params']['_meta']['anthropic/permissionDisplay']==existing
    assert ToolFilter({'enabled_tools':['js'],'disabled_tools':[]}).child(request)==request


def test_relay_projects_observed_session_and_actual_user_frame_then_expires(tmp_path):
    path=tmp_path/'native-input-context.json'
    native_input.publish(path,{'version':1,'workerId':'wrk_context','runId':'run_context','contextToken':'private-fixture-token'})
    child=tmp_path/'child.py'
    child.write_text('''import json,os,sys,time
from pathlib import Path
init=json.loads(sys.stdin.readline()); user=json.loads(sys.stdin.readline())
assert user['message']['content']=='Original bytes.'
assert user['uuid']
path=Path(os.environ['GLASSHIVE_NATIVE_INPUT_CONTEXT'])
assert json.loads(path.read_text())['contextToken']==os.environ['GLASSHIVE_NATIVE_INPUT_TOKEN']
print(json.dumps({'type':'system','subtype':'init','session_id':'native-issued-session'}),flush=True)
end=time.monotonic()+3
while time.monotonic()<end:
    context=json.loads(path.read_text())
    if context.get('nativeSessionActive'):break
    time.sleep(.01)
assert context['nativeSessionId']=='native-issued-session'
assert context['nativeTurnId']==user['uuid']
print(json.dumps({'type':'result','subtype':'success','result':'Actual frame context preserved.'}),flush=True)
''')
    proc=subprocess.run([sys.executable,native_input.__file__,str(path),sys.executable,str(child)],input='Original bytes.',capture_output=True,text=True,timeout=8)
    assert proc.returncode==0,proc.stderr
    assert json.loads(proc.stdout.splitlines()[-1])['result']=='Actual frame context preserved.'
    final=native_input.read_object(path)
    assert final['nativeSessionId']=='native-issued-session'
    assert final['nativeSessionActive'] is False
