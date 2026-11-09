from dataclasses import dataclass
from unittest.mock import AsyncMock
from pydantic import TypeAdapter
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.abstract import ToolsetTool
import pytest
import asyncio
from functools import wraps

def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run
from workers_projects_runtime.coordination_toolset import CoordinationToolset, START, CONTROL, ROSTER, coordination_tool_manifest

@dataclass
class Context:
    tool_name: str = ''

class Source:
    def __init__(self):
        self.call_tool = AsyncMock(return_value={'status':'accepted','workRef':'work-1'})
    async def get_tools(self, ctx):
        schemas = {
            START: {'properties': {'inheritContext':{'type':'boolean'}, 'instruction':{'type':'string'}, 'title':{'type':'string'}, 'resourceClass':{'type':'string'}, 'sourceOrdinals':{'type':'array','items':{'type':'integer','minimum':1,'maximum':3}}, 'fileIds':{'type':'array','items':{'enum':['file-owned']}}}, 'required':['sourceOrdinals']},
            CONTROL: {'properties': {'inheritContext':{'type':'boolean'},'workRef':{'type':'string'},'action':{'type':'string'},'instruction':{'type':'string'},'nativeInput':{'type':'object'},'sourceOrdinals':{'type':'array'},'fileIds':{'type':'array','items':{'type':'string'}}}},
            ROSTER: {'properties':{'scope':{'type':'string'}}},
            'schedule_create': {'properties':{}}, 'web_search':{'properties':{}},
        }
        return {name: ToolsetTool(toolset=self, tool_def=ToolDefinition(name=name, parameters_json_schema={'type':'object',**schema}), max_retries=0,args_validator=TypeAdapter(dict[str, object]).validator) for name,schema in schemas.items()}

@run_async
async def test_native_canonical_mcp_contract_passes_through_without_a_second_translation():
    source = Source()
    original = await source.get_tools(Context())
    canonical = {'hand_off_to': original[START], 'list_workers': original[ROSTER],
                 'unrelated_action': original['web_search']}
    source.get_tools = AsyncMock(return_value=canonical)
    facade = CoordinationToolset(source)
    tools = await facade.get_tools(Context())
    assert set(tools) == {'hand_off_to', 'list_workers'}
    args = {'action': 'start', 'sourceOrdinals': [2], 'fileIds': []}
    await facade.call_tool('hand_off_to', args, Context(), tools['hand_off_to'])
    assert source.call_tool.call_args.args[:2] == ('hand_off_to', args)
    with pytest.raises(ValueError):
        await facade.call_tool('unrelated_action', {}, Context(), original['web_search'])

@run_async
async def test_only_two_tools_and_no_brief_generation():
    source=Source(); facade=CoordinationToolset(source); tools=await facade.get_tools(Context())
    assert set(tools)=={'hand_off_to','list_workers'}
    description=tools['hand_off_to'].tool_def.description
    assert 'authorized capability projection independently from this conversation' in description
    assert 'missing direct tool does not establish worker inability' in description
    assert 'within the user\'s execution and consultation limits' in description
    assert 'do not rewrite a brief' in description
    assert 'acceptance of background work only' in description
    assert 'a claim that the requested effect occurred requires result evidence' in description
    schema=tools['hand_off_to'].tool_def.parameters_json_schema
    assert not {'title','resourceClass','inheritContext'} & set(schema['properties'])
    assert 'verbatim clause' in schema['properties']['instruction']['description']
    assert schema['allOf'][0]['then']['required']==['sourceOrdinals']
    result=await facade.call_tool('hand_off_to',{'action':'start','sourceOrdinals':[2],'fileIds':[]},Context(),tools['hand_off_to'])
    assert result['status']=='accepted'
    call=source.call_tool.call_args.args
    assert call[0]==START and call[1]=={'sourceOrdinals':[2],'fileIds':[],'inheritContext':True}

@run_async
@pytest.mark.parametrize('action',['queue','message','steer','pause','resume','stop','retry','dismiss'])
async def test_exact_existing_worker_controls_are_preserved(action):
    source=Source(); facade=CoordinationToolset(source); tools=await facade.get_tools(Context())
    await facade.call_tool('hand_off_to',{'action':action,'workRef':'work-7'},Context(),tools['hand_off_to'])
    name,args,*_=source.call_tool.call_args.args
    assert name==CONTROL and args['workRef']=='work-7' and args['action']==action
    assert args.get('inheritContext',False)==(action in {'queue','message','steer','retry'})

@run_async
async def test_roster_and_unknown_actions_do_not_gain_authority():
    source=Source(); facade=CoordinationToolset(source); tools=await facade.get_tools(Context())
    await facade.call_tool('list_workers',{'scope':'history'},Context(),tools['list_workers'])
    assert source.call_tool.call_args.args[:2]==(ROSTER,{'scope':'history'})
    with pytest.raises(ValueError):
        await facade.call_tool('hand_off_to',{'action':'start','title':'invented recap'},Context(),tools['hand_off_to'])
    with pytest.raises(ValueError):
        await facade.call_tool('hand_off_to',{'action':'steer'},Context(),tools['hand_off_to'])


@run_async
async def test_action_specific_file_and_current_source_contracts_survive_projection():
    from jsonschema import validate, ValidationError
    tools=await CoordinationToolset(Source()).get_tools(Context())
    schema=tools['hand_off_to'].tool_def.parameters_json_schema
    validate({'action':'start','sourceOrdinals':[2],'fileIds':['file-owned']},schema)
    validate({'action':'steer','workRef':'work','sourceOrdinals':[2],'fileIds':['another-owned-ref']},schema)
    for args in [
        {'action':'start','sourceOrdinals':[2],'fileIds':['outside-start-selection']},
        {'action':'start','sourceOrdinals':[4]},
        {'action':'steer','workRef':'work','sourceOrdinals':[4]},
        {'action':'start'},
    ]:
        with pytest.raises(ValidationError): validate(args,schema)


@run_async
@pytest.mark.parametrize('action',['start','queue','message','steer','retry'])
async def test_optional_declared_instruction_is_forwarded_without_a_rewritten_brief(action):
    source=Source(); facade=CoordinationToolset(source); tools=await facade.get_tools(Context())
    original = 'Keep the original browser task too.'
    args = {'action': action, 'sourceOrdinals': [1], 'instruction': original}
    if action != 'start': args['workRef'] = 'work-7'
    await facade.call_tool('hand_off_to',args,Context(),tools['hand_off_to'])
    target, sent, *_ = source.call_tool.call_args.args
    assert target == (START if action == 'start' else CONTROL)
    assert sent['instruction'] == original
    assert sent['inheritContext'] is True
    assert sent['sourceOrdinals'] == [1]


@run_async
async def test_instruction_cannot_expand_undeclared_or_non_guidance_controls():
    source=Source(); facade=CoordinationToolset(source); tools=await facade.get_tools(Context())
    for action in ('pause','resume','stop','dismiss'):
        with pytest.raises(ValueError,match='coordination_instruction_unavailable'):
            await facade.call_tool('hand_off_to',{'action':action,'workRef':'work-7',
                'instruction':'Original selected text.'},Context(),tools['hand_off_to'])
    facade._original[START].tool_def.parameters_json_schema['properties'].pop('instruction')
    with pytest.raises(ValueError,match='coordination_instruction_unavailable'):
        await facade.call_tool('hand_off_to',{'action':'start','instruction':'Original selected text.'},
            Context(),tools['hand_off_to'])
    source.call_tool.assert_not_called()


@run_async
async def test_versioned_tool_contract_is_the_only_facade_description_owner():
    from pathlib import Path
    import hashlib,json
    from workers_projects_runtime import coordination_toolset
    path = Path(coordination_toolset.__file__).with_name('prompts') / 'coordination-tools.json'
    manifest = coordination_tool_manifest()
    assert manifest['id'] == 'xperfect.coordination-tools'
    assert manifest['version'] == 3
    assert manifest['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest['source'].endswith('prompts/coordination-tools.json')
    tools = await CoordinationToolset(Source()).get_tools(Context())
    assert tools['hand_off_to'].tool_def.description == json.loads(path.read_text())['tools']['hand_off_to']
    assert tools['hand_off_to'].tool_def.parameters_json_schema['properties']['instruction']['description'] == manifest['instruction_selection']
    assert 'Conditions stated elsewhere in the selected original input remain visible' in manifest['tools']['hand_off_to']
    assert 'do not assign other objectives to include them in the clause' in manifest['tools']['hand_off_to']
    assert 'do not include other objectives just to repeat those conditions' in manifest['instruction_selection']


@run_async
@pytest.mark.parametrize('required', [False, True])
async def test_start_file_choice_description_follows_exact_typed_required_metadata(required):
    from jsonschema import validate, ValidationError
    class ActualRequiredSource(Source):
        async def get_tools(self, ctx):
            tools = await super().get_tools(ctx)
            if required:
                tools[START].tool_def.parameters_json_schema['required'].append('fileIds')
            return tools
    tools = await CoordinationToolset(ActualRequiredSource()).get_tools(Context())
    schema = tools['hand_off_to'].tool_def.parameters_json_schema
    description = schema['properties']['fileIds']['description']
    assert 'omit for no added historical files' in description
    assert 'fileIds' not in schema['allOf'][0]['then']['required']
    assert schema['required'] == []
    validate({'action': 'start', 'sourceOrdinals': [1], 'fileIds': []}, schema)
    validate({'action': 'steer', 'workRef': 'work', 'sourceOrdinals': [1]}, schema)
    validate({'sourceOrdinals': [1]}, schema)
    validate({'action': 'start', 'sourceOrdinals': [1]}, schema)


@run_async
async def test_canonical_existing_work_control_retains_only_its_declared_schema_and_authority():
    source=Source();declared=await source.get_tools(Context())
    source.get_tools=AsyncMock(return_value={'hand_off_to':declared[START],
        'list_workers':declared[ROSTER],CONTROL:declared[CONTROL],
        'schedule_create':declared['schedule_create'],'web_search':declared['web_search']})
    facade=CoordinationToolset(source);tools=await facade.get_tools(Context())
    assert set(tools)=={'hand_off_to','list_workers',CONTROL}
    assert tools[CONTROL].tool_def.parameters_json_schema is declared[CONTROL].tool_def.parameters_json_schema
    assert tools[CONTROL].args_validator is declared[CONTROL].args_validator
    original={'action':'steer','workRef':'owned-work','instruction':'Keep the exact original guidance.',
              'sourceOrdinals':[2],'nativeInput':{'requestId':'owned-question'}}
    await facade.call_tool(CONTROL,original,Context(),tools[CONTROL])
    assert source.call_tool.call_args.args[:2]==(CONTROL,original)
    assert source.call_tool.call_args.args[3] is declared[CONTROL]
    for name in ('schedule_create','web_search'):
        with pytest.raises(ValueError,match='coordination_tool_unavailable'):
            await facade.call_tool(name,{},Context(),declared[name])


@run_async
async def test_canonical_control_is_not_invented_when_not_granted_or_filtered_out():
    source=Source();declared=await source.get_tools(Context())
    # This is the wrapped view after the Direct runner's existing enabled/disabled filter.
    source.get_tools=AsyncMock(return_value={'hand_off_to':declared[START],'list_workers':declared[ROSTER]})
    facade=CoordinationToolset(source);tools=await facade.get_tools(Context())
    assert CONTROL not in tools
    with pytest.raises(ValueError,match='coordination_tool_unavailable'):
        await facade.call_tool(CONTROL,{'action':'stop','workRef':'ungranted-work'},Context(),declared[CONTROL])
    source.call_tool.assert_not_called()


@run_async
async def test_canonical_roster_and_control_do_not_gain_a_new_handoff_grant():
    source=Source();declared=await source.get_tools(Context())
    source.get_tools=AsyncMock(return_value={'list_workers':declared[ROSTER],CONTROL:declared[CONTROL]})
    facade=CoordinationToolset(source);tools=await facade.get_tools(Context())
    assert set(tools)=={'list_workers',CONTROL}
    await facade.call_tool(CONTROL,{'action':'stop','workRef':'owned-existing-work'},Context(),tools[CONTROL])
    assert source.call_tool.call_args.args[:2]==(CONTROL,{'action':'stop','workRef':'owned-existing-work'})
    with pytest.raises(ValueError,match='coordination_tool_unavailable'):
        await facade.call_tool('hand_off_to',{},Context(),declared[START])


@run_async
@pytest.mark.parametrize('marker,granted', [('original-context-v1',True),('wrong-contract',True),(None,True),('original-context-v1',False)])
async def test_actual_mcp_metadata_preserves_granted_control_only_contract(marker,granted):
    from fastmcp import FastMCP,Client
    from pydantic_ai import Agent
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse,ToolCallPart,TextPart
    from workers_projects_runtime.coordination_toolset import COORDINATION_CONTRACT_META_KEY
    server=FastMCP('Synthetic canonical control');calls=[];seen=[]
    @server.tool(name=CONTROL,meta={COORDINATION_CONTRACT_META_KEY:marker} if marker else {})
    async def control(action:str,workRef:str):
        calls.append({'action':action,'workRef':workRef});return {'status':'accepted'}
    backend=MCPToolset(Client(server),max_retries=0)
    filtered=backend.filtered(lambda ctx,tool:granted)
    facade=CoordinationToolset(filtered)
    def model(messages,info):
        seen.append(info.function_tools)
        if granted and not calls:
            return ModelResponse(parts=[ToolCallPart(CONTROL,{'action':'stop','workRef':'owned-original-goal'})])
        return ModelResponse(parts=[TextPart('Done.')])
    agent=Agent(FunctionModel(model),toolsets=[facade])
    if granted and marker!='original-context-v1':
        with pytest.raises(ValueError,match='coordination_source_binding_unavailable'):
            await agent.run('Synthetic exact existing-work control.')
        assert calls==[]
    else:
        await agent.run('Synthetic exact existing-work control.')
        if granted:
            assert seen[0][0].metadata['meta'][COORDINATION_CONTRACT_META_KEY]==marker
            assert [tool.name for tool in seen[0]]==[CONTROL]
            assert calls==[{'action':'stop','workRef':'owned-original-goal'}]
        else:
            assert all(not tools for tools in seen) and calls==[]
