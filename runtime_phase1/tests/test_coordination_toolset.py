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
from workers_projects_runtime.coordination_toolset import CoordinationToolset, START, CONTROL, ROSTER

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
    schema=tools['hand_off_to'].tool_def.parameters_json_schema
    assert not {'title','instruction','resourceClass','inheritContext'} & set(schema['properties'])
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
        await facade.call_tool('hand_off_to',{'action':'start','instruction':'invented recap'},Context(),tools['hand_off_to'])
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
