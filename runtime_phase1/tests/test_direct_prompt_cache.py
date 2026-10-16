"""Official Responses cache routing is scoped; it grants no authority or server history."""
import asyncio
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest
from workers_projects_runtime.direct_runtime import DirectModelRuntime, DIRECT_PROVIDERS
from workers_projects_runtime.agent_builder_control import PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT


def worker(tmp_path, profile='direct-xai', **bundle):
    return {'worker_id':'synthetic-worker','owner_id':'synthetic-owner','tenant_id':'synthetic-tenant',
        'profile':profile,'execution_mode':'host','model':DIRECT_PROVIDERS[profile]['model'],
        'workspace_root':str(tmp_path/'workspace'),'trusted_run_lane':'conversation',
        'bootstrap_bundle_json':json.dumps({'run_mode':'conversation',**bundle})}


def native_request(tmp_path, profile='direct-xai', **bundle):
    runtime=DirectModelRuntime(profile,base_dir=str(tmp_path/'state'))
    w=worker(tmp_path,profile,**bundle);info=runtime.prepare_worker_workspace(w)
    return runtime,w,info,runtime._request(w,'Synthetic current input',info)


@pytest.mark.parametrize('profile',['direct-openai','direct-xai'])
def test_cache_key_stable_domain_separated_and_scope_bound(tmp_path,monkeypatch,profile):
    from workers_projects_runtime import direct_runtime as owner
    runtime,w,info,first=native_request(tmp_path,profile)
    second=runtime._request(w,'Another current input',info)
    key=first['prompt_cache_key'];scope=json.dumps(first['history_scope'],sort_keys=True)
    assert key==second['prompt_cache_key']==hashlib.sha256(('xperfect-prompt-cache-v1\n'+scope).encode()).hexdigest()
    assert key not in first['history_path'] and len(key)==64 and all(c in '0123456789abcdef' for c in key)
    assert not any(raw in key for raw in ['synthetic-owner','synthetic-tenant','synthetic-worker'])
    for field in ['owner_id','tenant_id','worker_id']:
        changed={**w,field:'different-'+field}
        assert runtime._request(changed,'Same',info)['prompt_cache_key']!=key
    assert runtime._request(w,'Same',replace_model(info))['prompt_cache_key']!=key
    altered=worker(tmp_path,profile,env={'GLASSHIVE_PROVIDER_SESSION_EPOCH':'new-epoch'})
    assert runtime._request(altered,'Same',info)['prompt_cache_key']!=key
    monkeypatch.setattr(owner,'mission_provider_account_selection',lambda _:SimpleNamespace(account_id='different-account'))
    assert runtime._request(w,'Same',info)['prompt_cache_key']!=key


def replace_model(info):
    from dataclasses import replace
    return replace(info,model='different-model')


@pytest.mark.parametrize('profile',list(DIRECT_PROVIDERS))
def test_no_cache_association_for_stateless_or_private_and_no_anthropic(tmp_path,profile):
    _,_,_,plain=native_request(tmp_path,profile)
    assert bool(plain['prompt_cache_key'])==(profile!='direct-anthropic')
    _,_,_,stateless=native_request(tmp_path,profile,env={'GLASSHIVE_PROVIDER_SESSION_MODE':'stateless'})
    assert stateless['prompt_cache_key'] is None
    _,_,_,private=native_request(tmp_path,profile,env={'GLASSHIVE_PROVIDER_SESSION_MODE':'stateless'},
        provider_capabilities={'native_tools':False,'response_contract':PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT},access_mode='read_only')
    assert private['prompt_cache_key'] is None


@pytest.mark.parametrize('provider,history,private,expected',[
    ('xai',True,False,True),('openai',True,False,True),('anthropic',True,False,False),
    ('xai',False,False,False),('openai',True,True,False)])
def test_official_sdk_http_body_carries_only_eligible_cache_key_without_retention(tmp_path,monkeypatch,provider,history,private,expected):
    from workers_projects_runtime import direct_runner
    httpx=importlib.import_module('httpx2' if provider=='anthropic' else 'httpx')
    sdk=importlib.import_module('anthropic' if provider=='anthropic' else 'openai')
    original=sdk.DefaultAsyncHttpxClient;captured=[]
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(401,json={'error':{'message':'Synthetic test rejection','type':'authentication_error'}})
    class Client(original):
        def __init__(self,*a,**kw):
            kw['transport']=httpx.MockTransport(handler);super().__init__(*a,**kw)
    monkeypatch.setattr(sdk,'DefaultAsyncHttpxClient',Client)
    monkeypatch.setenv({'xai':'XAI_API_KEY','openai':'OPENAI_API_KEY','anthropic':'ANTHROPIC_API_KEY'}[provider],'synthetic-not-used')
    monkeypatch.setattr(direct_runner,'emit',lambda e:None)
    key=hashlib.sha256(b'synthetic-scope').hexdigest()
    request={'provider':provider,'model':{'xai':'grok-4.7','openai':'gpt-6.1-sol','anthropic':'claude-opus-5-5'}[provider],
        'effort':'high','instructions':'Exact unchanged authority','instruction':'Synthetic input','mcp_servers':[],
        'history_enabled':history,'history_path':str(tmp_path/'history.json'),'history_scope':{'worker':'synthetic'},
        'session_id':'synthetic','run_id':'synthetic','prompt_cache_key':key}
    if private:
        request['response_contract']=PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT
        request['output_schema']={'type':'object','additionalProperties':True}
    with pytest.raises(Exception):asyncio.run(direct_runner.execute(request))
    assert len(captured)==1
    assert captured[0].get('prompt_cache_key')==(key if expected else None)
    assert 'prompt_cache_retention' not in captured[0]
    assert captured[0].get('store') is False if provider!='anthropic' else 'store' not in captured[0]
