from copy import deepcopy
import pytest
from workers_projects_runtime import native_mcp_tool_filter as module
FIELD='_viventium_permission_scope'
def projection(): return module.ToolFilter({'enabled_tools':None,'disabled_tools':[]},native_context=True)
def request(scopes=None, id='approval'):
    return {'jsonrpc':'2.0','id':id,'method':'elicitation/create','params':{'message':'Allow access?','mode':'form','requestedSchema':{'type':'object','properties':{'reason':{'type':'string'}},'required':['reason'],'additionalProperties':False},'_meta':{'persist':scopes or ['session'],'preserved':'yes'}}}
def test_offered_scope_is_optional_and_preserves_original():
    p=projection(); original=request(); snapshot=deepcopy(original); result=p.child(original)
    schema=result['params']['requestedSchema']
    assert schema['properties'][FIELD]['oneOf']==[{'const':'session','title':'For this task'}]
    assert 'default' not in schema['properties'][FIELD]
    assert schema['required']==['reason'] and schema['additionalProperties'] is False
    assert result['params']['_meta']==original['params']['_meta']
    assert original==snapshot
@pytest.mark.parametrize('scope',['session','always'])
def test_explicit_scope_round_trip(scope):
    p=projection();p.child(request(['session','always']))
    reply={'jsonrpc':'2.0','id':'approval','result':{'action':'accept','content':{'reason':'owner decision',FIELD:scope}}}
    output,error=p.client(reply)
    assert error is None
    assert output['result']=={'action':'accept','content':{'reason':'owner decision'},'_meta':{'persist':scope}}
    assert FIELD in reply['result']['content']
@pytest.mark.parametrize('action',['accept','decline','cancel'])
def test_no_choice_never_adds_persistence(action):
    p=projection();p.child(request())
    reply={'jsonrpc':'2.0','id':'approval','result':{'action':action,'content':{'reason':'one request'}}}
    assert p.client(reply)==(reply,None)
@pytest.mark.parametrize('action',['decline','cancel'])
def test_native_hook_decline_is_authoritative(action):
    p=projection();p.child(request())
    reply={'jsonrpc':'2.0','id':'approval','result':{'action':action,'content':{FIELD:'session'},'_meta':{'policy':'preserved'}}}
    assert p.client(reply)[0]['result']=={'action':action,'content':{},'_meta':{'policy':'preserved'}}
def test_hook_metadata_is_preserved():
    p=projection();p.child(request())
    reply={'jsonrpc':'2.0','id':'approval','result':{'action':'accept','content':{FIELD:'session'},'_meta':{'persist':'once','policy':'preserved'}}}
    assert p.client(reply)[0]['result']['_meta']=={'persist':'once','policy':'preserved'}
def test_unoffered_scope_rejected():
    p=projection();p.child(request())
    with pytest.raises(ValueError,match='not offered'):
        p.client({'id':'approval','result':{'action':'accept','content':{FIELD:'always'}}})
def test_no_advertisement_or_collision_passes_through():
    for kind in ['no_meta','collision','unknown','url']:
        r=request()
        if kind=='no_meta':r['params'].pop('_meta')
        elif kind=='collision':r['params']['requestedSchema']['properties'][FIELD]={'type':'string'}
        elif kind=='unknown':r['params']['_meta']['persist']=['unsupported']
        else:r['params']['mode']='url'
        assert projection().child(r)==r
def test_batches_keep_separate_native_requests():
    p=projection();out=p.child([request(id=1),request(['always'],id='1')]);assert len(out)==2
    replies=[{'id':1,'result':{'action':'accept','content':{FIELD:'session'}}},{'id':'1','result':{'action':'accept','content':{FIELD:'always'}}}]
    output,error=p.client(replies)
    assert error is None
    assert [r['result']['_meta']['persist'] for r in output]==['session','always']
def test_unknown_reply_is_not_a_grant():
    p=projection();reply={'id':'unrelated','result':{'action':'accept','content':{FIELD:'always'}}}
    assert p.client(reply)==(reply,None)
