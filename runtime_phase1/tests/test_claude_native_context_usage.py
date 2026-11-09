"""Run accounting and last-prompt occupancy have separate native usage owners."""
import json
import pytest

from workers_projects_runtime.conversation_provider import _native_usage,_native_context_usage


def log(*events):return '\n'.join(json.dumps(event) for event in events)


def test_native_cached_run_accounting_is_not_last_response_context_or_session_cumulative():
    first={'input_tokens':2,'cache_creation_input_tokens':100,'cache_read_input_tokens':0,'output_tokens':9}
    last={'input_tokens':3,'cache_creation_input_tokens':4,'cache_read_input_tokens':100,'output_tokens':12,'type':'message'}
    aggregate={'input_tokens':5,'cache_creation_input_tokens':104,'cache_read_input_tokens':100,'output_tokens':21,'iterations':[last]}
    wire=log({'type':'assistant','message':{'id':'first','usage':first}},
        {'type':'assistant','message':{'id':'last','usage':last}},
        {'type':'result','usage':aggregate,'modelUsage':{'synthetic':{'inputTokens':999999,'cacheReadInputTokens':999999}}})
    billing=_native_usage('claude-code',wire)
    assert billing=={'prompt_tokens':209,'completion_tokens':21,'total_tokens':230}
    context,scope=_native_context_usage('claude-code',wire,billing,context_window=1000)
    assert context=={'prompt_tokens':107,'completion_tokens':12,'total_tokens':119}
    assert scope=='native_prompt_tokens'


@pytest.mark.parametrize('usage,expected',[
    ({'input_tokens':2,'cache_read_input_tokens':50,'cache_creation_input_tokens':100,'output_tokens':7},152),
    ({'input_tokens':2,'cache_read_input_tokens':50,'cache_creation':{'ephemeral_5m_input_tokens':40,'ephemeral_1h_input_tokens':60},'output_tokens':7},152),
    ({'input_tokens':2,'cache_read_input_tokens':50,'cache_creation_input_tokens':100,'cache_creation':{'ephemeral_1h_input_tokens':100},'output_tokens':7},152),
    ({'prompt_tokens':152,'cache_read_input_tokens':50,'cache_creation_input_tokens':100,'completion_tokens':7},152),
    ({'input_tokens':'2','cache_read_input_tokens':'50','cache_creation_input_tokens':'100','output_tokens':'7'},152),
    ({'input_tokens':0,'cache_read_input_tokens':300,'output_tokens':7},300),
    ({'input_tokens':17,'output_tokens':7},17),
])
def test_legacy_and_current_native_counter_shapes_count_each_input_once(usage,expected):
    wire=log({'type':'result','usage':usage})
    billing=_native_usage('claude-code',wire)
    assert billing=={'prompt_tokens':expected,'completion_tokens':7,'total_tokens':expected+7}
    context,scope=_native_context_usage('claude-code',wire,billing,context_window=1000)
    assert context==billing and scope=='native_prompt_tokens_upper_bound'


def test_latest_assistant_snapshot_supports_partial_native_run_without_terminal_aggregate():
    wire=log({'type':'assistant','message':{'usage':{'input_tokens':2,'cache_read_input_tokens':100,'cache_creation_input_tokens':5,'output_tokens':1}}})
    assert _native_usage('claude-code',wire) is None
    context,scope=_native_context_usage('claude-code',wire,None,context_window=1000)
    assert context=={'prompt_tokens':107,'completion_tokens':1,'total_tokens':108}
    assert scope=='native_prompt_tokens'


def test_duplicate_assistant_blocks_are_not_added_to_prompt_occupancy():
    usage={'input_tokens':2,'cache_read_input_tokens':100,'cache_creation_input_tokens':5,'output_tokens':1}
    wire=log(*[{'type':'assistant','message':{'id':'same-message','usage':usage}} for _ in range(3)])
    context,scope=_native_context_usage('claude-code',wire,None,context_window=1000)
    assert context['prompt_tokens']==107 and scope=='native_prompt_tokens'


def test_partial_per_response_cache_data_uses_existing_aggregate_upper_bound():
    wire=log({'type':'assistant','message':{'usage':{'input_tokens':2,'output_tokens':1}}},
        {'type':'result','usage':{'input_tokens':2,'cache_creation_input_tokens':100,'cache_read_input_tokens':500,'output_tokens':1}})
    billing=_native_usage('claude-code',wire)
    context,scope=_native_context_usage('claude-code',wire,billing,context_window=1000)
    assert context['prompt_tokens']==602 and scope=='native_prompt_tokens_upper_bound'


@pytest.mark.parametrize('invalid',[True,-1,1.5,'invalid',None])
@pytest.mark.parametrize('key',['input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens'])
def test_invalid_native_counts_cannot_become_zero_or_exact_occupancy(key,invalid):
    usage={'input_tokens':2,'cache_read_input_tokens':100,'cache_creation_input_tokens':5,'output_tokens':1}
    usage[key]=invalid
    wire=log({'type':'assistant','message':{'usage':usage}},{'type':'result','usage':usage})
    billing=_native_usage('claude-code',wire)
    assert billing is None
    context,scope=_native_context_usage('claude-code',wire,billing,context_window=1000)
    assert context=={'prompt_tokens':1000,'completion_tokens':0,'total_tokens':1000}
    assert scope=='native_prompt_tokens_upper_bound'


def test_context_snapshot_cannot_exceed_measured_run_accounting():
    wire=log({'type':'assistant','message':{'usage':{'input_tokens':200,'output_tokens':1}}},
        {'type':'result','usage':{'input_tokens':100,'output_tokens':1}})
    billing=_native_usage('claude-code',wire)
    context,scope=_native_context_usage('claude-code',wire,billing,context_window=1000)
    assert context==billing and scope=='native_prompt_tokens_upper_bound'
