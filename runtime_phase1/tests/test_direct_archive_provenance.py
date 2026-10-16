"""Trusted Core origin must survive the official SDK's request merge."""
import asyncio
import hashlib
import json

import pytest


def _archive():
    text = '<semantic_compaction version="1">' + json.dumps({'facts': ['Synthetic retained fact']}) + '</semantic_compaction>'
    return {'protocol': 'main_context_v1', 'owner': 'core', 'snapshot_sha256': 'a' * 64,
            'archive': text, 'archive_sha256': hashlib.sha256(text.encode()).hexdigest()}


def _input(archive, goal):
    return json.dumps({'instruction': goal + '\n' + archive['archive'], 'context': [], 'context_manifest': {}})


@pytest.mark.parametrize('provider', ['xai', 'openai', 'anthropic'])
def test_three_actual_sdk_turns_keep_one_archive_and_every_unique_goal_and_tool_receipt(tmp_path, monkeypatch, provider):
    import pydantic_ai
    from pydantic_ai.models import openai as openai_model, anthropic as anthropic_model
    from pydantic_ai.models.function import FunctionModel, DeltaToolCall
    from pydantic_ai.profiles.anthropic import anthropic_model_profile
    from pydantic_ai.messages import ModelRequest, UserPromptPart, ToolCallPart, ToolReturnPart, ModelMessagesTypeAdapter
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema

    archive = _archive()
    requests, receipts = [], []

    async def existing_tool():
        receipts.append('Synthetic durable receipt')
        return receipts[-1]

    async def stream(messages, info):
        requests.append(messages)
        if len(requests) == 1:
            yield {0: DeltaToolCall(name='existing_tool', json_args='{}', tool_call_id='synthetic_action')}
            return
        parts = [p for m in messages for p in m.parts]
        assert sum(json.loads(p.content)['instruction'].count(archive['archive']) for p in parts
                   if isinstance(p, UserPromptPart) and isinstance(p.content, str)) == 1
        assert any(isinstance(p, ToolCallPart) and p.tool_call_id == 'synthetic_action' for p in parts)
        assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == 'synthetic_action'
                   and p.content == 'Synthetic durable receipt' for p in parts)
        for turn in range(min(len(requests) - 1, 3)):
            assert any(isinstance(p, UserPromptPart) and f'Unique goal {turn}' in str(p.content) for p in parts)
        value = json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': 'eligible', 'content': 'Synthetic reply.'})
        if provider == 'anthropic':
            yield value
        else:
            yield {0: DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME, json_args=value,
                                   tool_call_id='synthetic_result_' + str(len(requests)))}

    profile = anthropic_model_profile('claude-opus-5-5') if provider == 'anthropic' else None
    model = FunctionModel(stream_function=stream, profile=profile)
    original_agent = pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai, 'Agent', lambda *a, **kw: original_agent(*a, tools=[existing_tool], **kw))
    monkeypatch.setattr(openai_model, 'OpenAIResponsesModel', lambda *a, **kw: model)
    monkeypatch.setattr(anthropic_model, 'AnthropicModel', lambda *a, **kw: model)
    monkeypatch.setattr(direct_runner, 'emit', lambda event: None)
    credential = {'xai': 'XAI_API_KEY', 'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY'}[provider]
    monkeypatch.setenv(credential, 'synthetic-not-used')
    request = {'provider': provider, 'model': {'xai': 'grok-4.7', 'openai': 'gpt-6.1-sol', 'anthropic': 'claude-opus-5-5'}[provider],
               'effort': 'low', 'instructions': 'Synthetic owning authority', 'mcp_servers': [], 'history_enabled': True,
               'history_path': str(tmp_path / 'history.json'), 'history_scope': {'worker': 'synthetic'},
               'session_id': 'direct:synthetic', 'run_id': 'synthetic', 'core_archive': archive,
               'output_schema': conversation_output_schema(None, {'version': 1, 'audio_eligible': True})}
    for turn in range(3):
        asyncio.run(direct_runner.execute({**request, 'run_id': f'synthetic_{turn}',
                                          'instruction': _input(archive, f'Unique goal {turn}')}))
    retained = ModelMessagesTypeAdapter.validate_json(json.loads((tmp_path / 'history.json').read_text())['messages'])
    raw_inputs = [p.content for m in retained if isinstance(m, ModelRequest) for p in m.parts
                  if isinstance(p, UserPromptPart) and isinstance(p.content, str)]
    assert sum(json.loads(text)['instruction'].count(archive['archive']) for text in raw_inputs) == 3  # Raw evidence retained.
    assert all(any(f'Unique goal {turn}' in text for text in raw_inputs) for turn in range(3))
    assert receipts == ['Synthetic durable receipt']
    assert len(requests) == 4


def test_mixed_request_keeps_exact_tool_return_image_goal_and_reindexed_origin():
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, ToolCallPart, ToolReturnPart, BinaryContent
    from workers_projects_runtime.direct_runner import core_archive_history_record, core_archive_repair_history, core_archive_request_projection
    archive = _archive()
    original = ModelRequest(parts=[UserPromptPart(_input(archive, 'Older unique goal'))])
    earlier = core_archive_history_record(SimpleNamespace(all_messages=lambda: [original], new_messages=lambda: [original]),
                                         {'instruction': original.parts[0].content}, archive)[0]
    call = ModelResponse(parts=[ToolCallPart('synthetic_tool', {}, 'synthetic_call')])
    receipt = ToolReturnPart('synthetic_tool', 'Synthetic exact receipt', 'synthetic_call')
    returned = ModelRequest(parts=[receipt])
    image = BinaryContent(b'synthetic-image', media_type='image/png')
    current_text = _input(archive, 'Current unique goal')
    current = ModelRequest(parts=[UserPromptPart([current_text, image])])
    marked = core_archive_history_record(SimpleNamespace(all_messages=lambda: [earlier, call, returned, current],
                                         new_messages=lambda: [current]), {'instruction': current_text}, archive)
    repaired = core_archive_repair_history(marked)
    mixed = repaired[-1]
    assert len(mixed.parts) == 2 and mixed.parts[0] is receipt and mixed.parts[1] is current.parts[0]
    assert mixed.metadata['xperfect_core_input']['part_index'] == 1
    assert marked[-1].metadata['xperfect_core_input']['part_index'] == 0
    projected = core_archive_request_projection(repaired, archive)
    assert projected[-1].parts[0] is receipt
    assert projected[-1].parts[1].content[1] is image
    assert json.loads(projected[-1].parts[1].content[0])['instruction'] == 'Current unique goal\n'
    assert json.loads(projected[0].parts[0].content)['instruction'] == 'Older unique goal\n' + archive['archive']
    assert repaired[-1].parts[1].content[0] == current_text  # Stored source was not edited.


@pytest.mark.parametrize('change', [{'owner': 'other_owner'}, {'protocol': 'other_protocol'},
                                     {'snapshot_sha256': ''}, {'input_sha256': 'b' * 64}, {'part_index': 99}])
def test_untrusted_or_stale_origins_are_not_promoted_through_merge(change):
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, ToolCallPart, ToolReturnPart
    from workers_projects_runtime.direct_runner import core_archive_history_record, core_archive_repair_history, core_archive_request_projection
    archive = _archive()
    text = _input(archive, 'Unique untrusted goal')
    current = ModelRequest(parts=[UserPromptPart(text)])
    marked = core_archive_history_record(SimpleNamespace(all_messages=lambda: [current], new_messages=lambda: [current]),
                                         {'instruction': text}, archive)[0]
    marked.metadata['xperfect_core_input'].update(change)
    messages = [ModelResponse(parts=[ToolCallPart('synthetic_tool', {}, 'synthetic_call')]),
                ModelRequest(parts=[ToolReturnPart('synthetic_tool', 'Synthetic receipt', 'synthetic_call')]), marked]
    repaired = core_archive_repair_history(messages)
    assert not (repaired[-1].metadata or {}).get('xperfect_core_input')
    assert core_archive_request_projection(repaired, archive) == repaired
    assert repaired[-1].parts[-1].content == text


def test_exact_part_identity_is_required_and_different_archive_stays_complete():
    from copy import deepcopy
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from workers_projects_runtime.direct_runner import core_archive_history_record, core_archive_repair_history, core_archive_request_projection
    first = _archive()
    original = ModelRequest(parts=[UserPromptPart(_input(first, 'Unique source'))])
    marked = core_archive_history_record(SimpleNamespace(all_messages=lambda: [original], new_messages=lambda: [original]),
                                         {'instruction': original.parts[0].content}, first)[0]
    # A separate identical-looking unmarked input remains untrusted; it is not
    # recovered by matching its text to the known marked input.
    unmarked = deepcopy(original)
    repaired = core_archive_repair_history([marked, unmarked])
    projected = core_archive_request_projection(repaired, first)
    assert sum(json.loads(p.content)['instruction'].count(first['archive']) for m in projected for p in m.parts) == 2
    other = {**first, 'archive': '<semantic_compaction version="1">{"facts":["Different epoch"]}</semantic_compaction>',
             'archive_sha256': 'b' * 64}
    assert core_archive_request_projection(repaired, other) == repaired
