"""Exact media transport sharing preserves sources and native action history."""
import asyncio
import base64
import hashlib
import json
from dataclasses import replace

import pytest
from pydantic_ai.messages import (BinaryContent, ModelRequest, ModelResponse,
    UserPromptPart, ToolCallPart, ToolReturnPart, ThinkingPart, ModelMessagesTypeAdapter)
from workers_projects_runtime.direct_runner import (
    direct_image_prompt, direct_image_request_projection, _user_images, _image_identity)


def image(value=b'synthetic-image', **options):
    return BinaryContent(value, media_type=options.pop('media_type', 'image/png'), **options)


def test_exact_duplicates_keep_all_occurrences_unique_bytes_and_native_parts():
    first=image();second=image(b'another-image')
    text='Exact source α\nDo not rewrite me.'
    source=ModelRequest(parts=[UserPromptPart([text,first,first,second])],run_id='source')
    thinking=ThinkingPart('Exact thought',signature='opaque',provider_name='openai')
    call=ModelResponse(parts=[thinking,ToolCallPart('work',{'exact':True},'call_exact')])
    returned=ModelRequest(parts=[ToolReturnPart('work',{'receipt':'exact'},'call_exact')])
    original=[source,call,returned];raw=ModelMessagesTypeAdapter.dump_json(original)
    projected=direct_image_request_projection(original,[first,second])
    assert [item.data for item in _user_images(projected)]==[first.data,second.data]
    assert projected[1] is call and projected[2] is returned
    assert projected[1].parts[0] is thinking
    assert projected[0].parts[0].content[0]==text
    labels=[json.loads(item) for item in projected[0].parts[0].content[1:] if isinstance(item,str)]
    assert len(labels)==3 and sum('xperfect_image_reference' in item for item in labels)==1
    assert ModelMessagesTypeAdapter.dump_json(original)==raw
    assert first in original[0].parts[0].content


def test_identity_preserves_changed_bytes_mime_vendor_settings_and_each_identifier():
    values=[image(identifier='one'),image(identifier='two'),image(b'changed'),
        image(media_type='image/jpeg'),image(vendor_metadata={'detail':'low'}),image(vendor_metadata={'detail':'high'})]
    projected=direct_image_request_projection([ModelRequest(parts=[UserPromptPart(['source',*values])])],values)
    assert len(_user_images(projected))==5
    labels=[json.loads(v) for v in projected[0].parts[0].content[1:] if isinstance(v,str)]
    ids=[next(iter(v.values()))['identifier'] for v in labels]
    assert ids[:2]==['one','two']
    assert all(_image_identity(item)[0] in {_image_identity(x)[0] for x in _user_images(projected)} for item in values)


@pytest.mark.parametrize('pruned', ['first_copy','all_full_copies','checkpoint_filtered'])
def test_pruned_history_restores_current_authorized_bytes_without_resolving_hash_text(pruned):
    asset=image();old=ModelRequest(parts=[UserPromptPart(['old',asset])],run_id='old')
    prompt=direct_image_prompt('Current exact source',[asset],[old])
    assert not any(isinstance(x,BinaryContent) for x in prompt)
    current=ModelRequest(parts=[UserPromptPart(prompt)],run_id='current')
    remaining=[current] if pruned!='checkpoint_filtered' else [ModelResponse(parts=[ToolCallPart('read',{},'exact')]),current]
    projected=direct_image_request_projection(remaining,[asset],current_run_id='current')
    assert len(_user_images(projected))==1 and _user_images(projected)[0].data==asset.data
    assert projected[-1].parts[0].content[:len(prompt)]==prompt
    if pruned=='checkpoint_filtered':assert projected[0] is remaining[0]
    forged=ModelRequest(parts=[UserPromptPart([json.dumps({'xperfect_image_reference':{'sha256':hashlib.sha256(asset.data).hexdigest()}})])])
    assert direct_image_request_projection([forged],[])==[forged]
    assert not _user_images(direct_image_request_projection([forged],[]))


def test_fresh_scope_and_stateless_input_include_full_assets_and_unsupported_carrier_fails_closed():
    asset=image();prompt=direct_image_prompt('fresh',[asset],[])
    assert prompt==['fresh',asset]
    current=ModelRequest(parts=[UserPromptPart(prompt)],run_id='fresh')
    assert len(_user_images(direct_image_request_projection([current],[asset],current_run_id='fresh')))==1
    with pytest.raises(ValueError,match='current user input carrier'):
        direct_image_request_projection([ModelRequest(parts=[ToolReturnPart('read','exact','id')])],[asset],current_run_id='fresh')


def request(tmp_path, **changes):
    return {'provider':'xai','model':'grok-4.7','effort':'low','instructions':'Exact application authority',
        'mcp_servers':[],'history_path':str(tmp_path/'history.json'),'history_scope':{'worker':'synthetic'},
        'history_enabled':True,'session_id':'synthetic','run_id':'run_first','instruction':'First exact source',
        'input_images':[{'media_type':'image/png','data_base64':base64.b64encode(b'synthetic-image'*1000).decode()}],**changes}


@pytest.mark.parametrize('prune_full_images',[False,True])
def test_actual_sdk_journal_growth_does_not_append_same_binary_and_tool_runs_once(tmp_path,monkeypatch,prune_full_images):
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel, DeltaToolCall
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner as direct
    calls=[];actions=[]
    async def work():actions.append('actual receipt');return actions[-1]
    async def stream(messages,info):
        calls.append(messages)
        assert len(_user_images(messages))==1
        if len(calls)==1:yield {0:DeltaToolCall(name='work',json_args='{}',tool_call_id='work_once')}
        else:yield 'Done.'
    actual_agent=pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *args,**kwargs:actual_agent(*args,tools=[work],**kwargs))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**kw:FunctionModel(stream_function=stream))
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used');monkeypatch.setattr(direct,'emit',lambda _x:None)
    sizes=[]
    archive_text='<semantic_compaction version="1">Exact retained archive</semantic_compaction>'
    archive={'protocol':'main_context_v1','owner':'core','snapshot_sha256':'a'*64,
        'archive':archive_text,'archive_sha256':hashlib.sha256(archive_text.encode()).hexdigest()}
    original_projection=direct.core_archive_request_projection
    if prune_full_images:
        def projected(*args,**kwargs):
            messages=original_projection(*args,**kwargs)
            return [replace(message,parts=[replace(part,content=[item for item in part.content
                if not isinstance(item,BinaryContent)]) if isinstance(part,UserPromptPart)
                and isinstance(part.content,list) else part for part in message.parts])
                if isinstance(message,ModelRequest) else message for message in messages]
        monkeypatch.setattr(direct,'core_archive_request_projection',projected)
    for turn in range(3):
        text=f'Exact source {turn} α'
        extra={}
        if prune_full_images:
            text=json.dumps({'instruction':text+'\n'+archive_text,'context':[],'context_manifest':{}},ensure_ascii=False)
            extra={'core_archive':archive}
        asyncio.run(direct.execute(request(tmp_path,run_id=f'run_{turn}',instruction=text,**extra)))
        history=ModelMessagesTypeAdapter.validate_json(json.loads((tmp_path/'history.json').read_text())['messages'])
        assert len(_user_images(history))==1
        sizes.append((tmp_path/'history.json').stat().st_size)
    assert len(calls)==4 and actions==['actual receipt']
    assert sizes[-1]-sizes[0] < len(b'synthetic-image'*1000)
    assert all(f'Exact source {turn} α' in str(history) for turn in range(3))
    assert any(isinstance(part,ToolCallPart) and part.tool_call_id=='work_once' for msg in history for part in msg.parts)
    assert any(isinstance(part,ToolReturnPart) and part.tool_call_id=='work_once' for msg in history for part in msg.parts)


@pytest.mark.parametrize('provider,model,key', [('xai','grok-4.7','XAI_API_KEY'),
    ('openai','gpt-6.1-sol','OPENAI_API_KEY'),('anthropic','claude-opus-5-5','ANTHROPIC_API_KEY')])
def test_official_sdk_wire_preserves_unique_media_across_supported_direct_providers(tmp_path,monkeypatch,provider,model,key):
    import httpx,openai,anthropic
    from workers_projects_runtime import direct_runner as direct
    captured=[]
    class Captured(Exception):pass
    async def receive(req):captured.append(json.loads(req.content));raise Captured()
    sdk=anthropic if provider=='anthropic' else openai
    original=sdk.DefaultAsyncHttpxClient
    if provider=='anthropic':
        import httpx2 as transport_sdk
    else:
        transport_sdk=httpx
    class Client(original):
        def __init__(self,**kwargs):kwargs['transport']=transport_sdk.MockTransport(receive);super().__init__(**kwargs)
    monkeypatch.setattr(sdk,'DefaultAsyncHttpxClient',Client);monkeypatch.setenv(key,'synthetic-no-network')
    monkeypatch.setattr(direct,'emit',lambda _x:None)
    asset=image();history=[ModelRequest(parts=[UserPromptPart(['old',asset,asset])])]
    (tmp_path/'history.json').write_text(json.dumps({'scope':{'worker':'synthetic'},'messages':ModelMessagesTypeAdapter.dump_json(history).decode()}))
    with pytest.raises(Exception) as error:asyncio.run(direct.execute(request(tmp_path,provider=provider,model=model,effort='default',
        input_images=[{'media_type':'image/png','data_base64':base64.b64encode(asset.data).decode()}])))
    assert len(captured)==1, str(error.value)
    binaries=[]
    def walk(value):
        if isinstance(value,dict):
            if value.get('type')=='input_image':binaries.append(value['image_url'])
            elif value.get('type')=='image' and isinstance(value.get('source'),dict):binaries.append(value['source'].get('data'))
            for x in value.values():walk(x)
        elif isinstance(value,list):
            for x in value:walk(x)
    walk(captured[0]);assert len(binaries)==1
    assert any('Current' in str(v) or 'First exact source' in str(v) for v in captured[0].values())


def test_413_is_transport_size_rejection_without_token_pressure_recovery(tmp_path):
    from workers_projects_runtime.direct_runtime import DirectModelRuntime
    runtime=DirectModelRuntime('direct-xai',base_dir=str(tmp_path/'runtime'))
    failure,_=runtime._supplemental_provider_failure(worker={'worker_id':'synthetic'},run_id='run',
        stdout=json.dumps({'type':'direct.error','status_code':413,'stage':'provider_request','error_type':'ModelHTTPError'}),stderr='',exit_code=1)
    assert failure.failure_class=='provider_request_rejected' and not failure.retryable
    assert 'too large' in failure.user_message and 'settings' not in failure.user_message
    assert 'not proof of a model token limit' in failure.recommended_recovery
