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
    assert description.startswith('Required for start. ') is required
    assert ('fileIds' in schema['allOf'][0]['then']['required']) is required
    assert schema['required'] == ['action']
    validate({'action': 'start', 'sourceOrdinals': [1], 'fileIds': []}, schema)
    validate({'action': 'steer', 'workRef': 'work', 'sourceOrdinals': [1]}, schema)
    if required:
        with pytest.raises(ValidationError):
            validate({'action': 'start', 'sourceOrdinals': [1]}, schema)
    else:
        validate({'action': 'start', 'sourceOrdinals': [1]}, schema)
