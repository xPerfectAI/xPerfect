import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ModelMessagesTypeAdapter, TextPart, UserPromptPart, ToolCallPart, ToolReturnPart
from workers_projects_runtime.direct_runner import complete_tool_history, read_context_replay
from workers_projects_runtime.direct_runtime import DirectModelRuntime
from workers_projects_runtime.conversation_provider import ConversationProvider


def messages():
    return [ModelRequest(parts=[UserPromptPart('Old text retained by the authenticated Main archive.')]),
            ModelResponse(parts=[TextPart('I will read the file.'), ToolCallPart('read_file', {'path':'synthetic.txt'}, tool_call_id='call-1')]),
            ModelRequest(parts=[ToolReturnPart('read_file', {'content':'Evidence, opaque-id-42'}, tool_call_id='call-1')]),
            ModelResponse(parts=[TextPart('The answer is retained by visible source coverage.')])]


def setup_history(tmp_path, profile='direct-xai'):
    runtime = DirectModelRuntime(profile, base_dir=str(tmp_path / 'state'))
    worker = {'worker_id':'worker_fixture', 'tenant_id':'tenant', 'owner_id':'owner',
              'profile':profile, 'model':runtime.direct['model'], 'execution_mode':'host',
              'workspace_root':str(tmp_path / 'workspace'),
              'bootstrap_bundle_json':json.dumps({'run_mode':'conversation', 'env':{'GLASSHIVE_PROVIDER_SESSION_EPOCH':'old-epoch'}})}
    info = runtime.prepare_worker_workspace(worker)
    request = json.loads(runtime._command_stdin_text(worker, 'Current input', info))
    path = Path(request['history_path'])
    path.write_text(json.dumps({'scope':request['history_scope'], 'messages':ModelMessagesTypeAdapter.dump_json(messages()).decode()}))
    return runtime, worker, request


def test_replay_keeps_complete_exact_protocol_parts_without_old_plain_turns():
    source = messages()
    retained, calls = complete_tool_history(source)
    assert calls == ['call-1']
    assert retained == source[1:3]
    assert ModelMessagesTypeAdapter.dump_json(retained) == ModelMessagesTypeAdapter.dump_json(source[1:3])


@pytest.mark.parametrize('defect', ['pending', 'orphan', 'duplicate', 'name'])
def test_replay_rejects_incomplete_or_ambiguous_tool_protocol(defect):
    source = messages()
    if defect == 'pending': source.pop(2)
    elif defect == 'orphan': source.pop(1)
    elif defect == 'duplicate': source.insert(2, source[1])
    else: source[2].parts[0].tool_name = 'different_tool'
    with pytest.raises(ValueError): complete_tool_history(source)


@pytest.mark.parametrize('profile', ['direct-openai', 'direct-anthropic', 'direct-xai'])
def test_epoch_seed_derives_scope_from_runtime_and_hashes_existing_journal(tmp_path, profile):
    runtime, worker, old = setup_history(tmp_path, profile)
    proof = runtime.provider_context_replay(worker)
    assert proof['tool_call_ids'] == ['call-1']
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['env']['GLASSHIVE_PROVIDER_SESSION_EPOCH'] = 'new-epoch'
    bundle['direct_context_replay'] = {**runtime.freeze_provider_context_replay(worker, proof), 'target_epoch':'new-epoch'}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    request = json.loads(runtime._command_stdin_text(worker, 'Full authorized visible replay', runtime.prepare_worker_workspace(worker)))
    assert request['history_path'] != old['history_path']
    assert request['context_replay']['scope'] == old['history_scope']
    retained, calls = read_context_replay(request['context_replay'])
    expected = ModelMessagesTypeAdapter.validate_json(json.loads(Path(old['history_path']).read_text())['messages'])[1:3]
    assert calls == ['call-1'] and retained == expected
    assert Path(old['history_path']).exists()
    Path(old['history_path']).write_text('old live epoch advanced after admission')
    assert read_context_replay(request['context_replay'])[1] == ['call-1']
    Path(request['context_replay']['path']).write_text('immutable seed was corrupted')
    with pytest.raises(ValueError, match='changed after pressure'): read_context_replay(request['context_replay'])


def test_wrong_owner_and_incomplete_history_do_not_prove_rotation(tmp_path):
    runtime, worker, request = setup_history(tmp_path)
    changed = {**worker, 'owner_id':'other-owner'}
    with pytest.raises(FileNotFoundError): runtime.provider_context_replay(changed)
    path = Path(request['history_path'])
    record = json.loads(path.read_text()); record['messages'] = ModelMessagesTypeAdapter.dump_json(messages()[:2]).decode()
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='incomplete'): runtime.provider_context_replay(worker)


@pytest.mark.parametrize('defect', ['', 'source_missing', 'wrong_epoch', 'incomplete_scan', 'foreign_run', 'missing_call'])
def test_provider_coverage_requires_complete_scoped_source_and_exact_journal_calls(tmp_path, defect):
    runtime, worker, _ = setup_history(tmp_path)
    session = {'session_id':'session', 'worker_id':worker['worker_id'], 'owner_id':'owner', 'tenant_id':'tenant'}
    decision = {'native_context_epoch':'old-epoch', 'instruction_sha256':'a'*64, 'advancement_key':'turn:1'}
    record = {**session, 'request_id':'request', 'run_id':'run', 'state':'completed', 'replay_decision_json':json.dumps(decision)}
    events = [{'type':'grok.session.started', 'session_id':'fixture'},
              {'type':'grok.session.update', 'session_id':'fixture', 'update':{'sessionUpdate':'tool_call', 'toolCallId':'missing' if defect=='missing_call' else 'call-1', 'status':'in_progress', 'rawInput':{'path':'synthetic.txt'}, '_meta':{'x.ai/tool':{'name':'read_file'}}}},
              {'type':'grok.session.update', 'session_id':'fixture', 'update':{'sessionUpdate':'tool_call_update', 'toolCallId':'missing' if defect=='missing_call' else 'call-1', 'status':'completed', 'rawOutput':{'content':'Evidence, opaque-id-42'}}},
              {'type':'grok.session.update', 'session_id':'fixture', 'update':{'sessionUpdate':'agent_message_chunk', 'content':{'type':'text', 'text':'Complete.'}}}]
    events.append({'type':'grok.result', 'session_id':'fixture', 'stop_reason':'end_turn', 'output':'Complete.'})
    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = SimpleNamespace(get_provider_request=lambda _:None if defect=='source_missing' else record,
        list_provider_session_requests_for_native_coverage=lambda *_, **kw:([record], defect!='incomplete_scan'),
        get_worker=lambda *_, **kw:worker, list_nonterminal_runs_for_worker=lambda _:[],
        get_run=lambda _: {'run_id':'run', 'worker_id':'foreign' if defect=='foreign_run' else worker['worker_id'], 'state':'completed'})
    def replay(_):
        proof = runtime.provider_context_replay(worker)
        if defect=='wrong_epoch': proof['source_epoch']='foreign'
        return proof
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_context_replay=replay,
        provider_tool_evidence_log=lambda *_, **kw:('direct-xai', '\n'.join(map(json.dumps, events)))))
    assert provider._native_tool_evidence_coverage_proven(session, {'last_accepted_request_id':'request', 'native_context_epoch':'old-epoch', 'accepted_advancement_keys':['turn:1']}) is (not defect)


def test_official_sdk_consumes_seeded_tools_once_then_persists_new_epoch(tmp_path, monkeypatch):
    import asyncio
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    runtime, worker, old = setup_history(tmp_path)
    original_bytes = Path(old['history_path']).read_bytes()
    proof = runtime.provider_context_replay(worker)
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['env']['GLASSHIVE_PROVIDER_SESSION_EPOCH'] = 'new-epoch'
    bundle['direct_context_replay'] = {**runtime.freeze_provider_context_replay(worker, proof), 'target_epoch':'new-epoch'}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    request = json.loads(runtime._command_stdin_text(worker, 'Authorized full text archive and current goal.', runtime.prepare_worker_workspace(worker)))
    request['output_schema'] = None
    request['mcp_servers'] = []
    observed, events = [], []
    async def stream(history, info):
        observed.append(history)
        calls = [part for message in history for part in message.parts if isinstance(part, ToolCallPart)]
        returns = [part for message in history for part in message.parts if isinstance(part, ToolReturnPart)]
        assert len(calls) == len(returns) == 1
        assert calls[0].tool_call_id == returns[0].tool_call_id == 'call-1'
        assert returns[0].content == {'content':'Evidence, opaque-id-42'}
        assert 'Old text retained' not in ModelMessagesTypeAdapter.dump_json(history).decode()
        yield 'I retain the exact tool evidence.'
    monkeypatch.setenv('XAI_API_KEY', 'synthetic-not-used')
    monkeypatch.setattr(openai_model, 'OpenAIResponsesModel', lambda *_, **kw:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner, 'emit', events.append)
    asyncio.run(direct_runner.execute(request))
    asyncio.run(direct_runner.execute({**request, 'instruction':'Continue without replaying actions.'}))
    assert len(observed) == 2
    assert len([event for event in events if event.get('stage') == 'context_tool_replay']) == 1
    assert Path(old['history_path']).read_bytes() == original_bytes
    new_history = ModelMessagesTypeAdapter.validate_json(json.loads(Path(request['history_path']).read_text())['messages'])
    assert complete_tool_history(new_history)[1] == ['call-1']


def test_pressure_transition_binds_exact_journal_to_the_new_epoch(tmp_path):
    runtime, worker, _ = setup_history(tmp_path)
    updates = []
    provider = ConversationProvider.__new__(ConversationProvider)
    session = {'session_id':'session', 'worker_id':worker['worker_id'], 'owner_id':'owner', 'history_count':4, 'workspace_dir':str(tmp_path)}
    provider.store = SimpleNamespace(get_worker=lambda *_, **kw:worker, list_nonterminal_runs_for_worker=lambda _:[],
        update_worker=lambda *_, **kw:updates.append(kw), update_provider_session_history=lambda *_, **kw:None)
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_context_replay=runtime.provider_context_replay,
        freeze_provider_context_replay=runtime.freeze_provider_context_replay))
    provider._native_bundle = lambda *a:{'run_mode':'conversation'}
    payload = SimpleNamespace(metadata=SimpleNamespace(message_id='new-turn', idempotency_key='idempotency'))
    model = SimpleNamespace(harness_profile='direct-xai', native_model='grok-4.7')
    updated, epoch = provider._advance_native_context_epoch_for_pressure(session, payload, model, 'high', {'native_context_epoch':'old-epoch'})
    bundle = json.loads(updates[0]['bootstrap_bundle_json'])
    assert bundle['direct_context_replay'] == {
        'source_epoch':'old-epoch', 'target_epoch':epoch,
        'sha256':runtime.provider_context_replay(worker)['sha256']}
    assert epoch != 'old-epoch'
    assert json.loads(updated['context_manifest_json'])['previous_native_context_epoch'] == 'old-epoch'


def test_unknown_native_tool_protocol_never_silently_loses_evidence():
    from pydantic_ai.messages import NativeToolCallPart
    with pytest.raises(ValueError, match='unsupported native tool'):
        complete_tool_history([ModelResponse(parts=[NativeToolCallPart('web_search', {}, tool_call_id='native-1')])])


def test_pressure_seed_drops_repeated_plain_history_but_keeps_large_tool_results_exact(tmp_path):
    runtime, worker, request = setup_history(tmp_path)
    source = messages()
    source[2].parts[0].content = {'content':'tool evidence ' * 1000, 'id':'opaque-evidence-42'}
    source = [ModelRequest(parts=[UserPromptPart('Already replayed visible source ' * 20000)])] + source[1:]
    path = Path(request['history_path'])
    raw = json.dumps({'scope':request['history_scope'], 'messages':ModelMessagesTypeAdapter.dump_json(source).decode()})
    path.write_text(raw)
    proof = runtime.provider_context_replay(worker)
    assert proof['bytes'] < len(raw.encode()) / 20
    replayed, _ = read_context_replay({'path':str(path), 'scope':request['history_scope'], 'sha256':proof['sha256']})
    assert ModelMessagesTypeAdapter.dump_json(replayed) == ModelMessagesTypeAdapter.dump_json(source[1:3])


def test_retained_tool_seed_is_counted_until_real_provider_usage_exists():
    from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS
    model = GLASSHIVE_MODELS['direct-xai:grok-4.7']
    unmeasured = ConversationProvider._native_occupancy_projection(model,
        {'native_context_seed_bytes':100000, 'observed_chars_per_token':4}, {'instruction_bytes':400})
    measured = ConversationProvider._native_occupancy_projection(model,
        {'native_context_seed_bytes':100000, 'latest_native_prompt_tokens':2000, 'observed_chars_per_token':4}, {'instruction_bytes':400})
    assert unmeasured['retained_tool_seed_tokens'] == 25000
    assert unmeasured['projected_occupancy_tokens'] == 25100
    assert measured['retained_tool_seed_tokens'] == 0
    assert measured['projected_occupancy_tokens'] == 2100


@pytest.mark.parametrize('defect', ['unfinished_predecessor', 'snapshot_unavailable'])
def test_pressure_replay_failure_defers_before_any_epoch_or_worker_mutation(tmp_path, defect):
    runtime, worker, _ = setup_history(tmp_path)
    provider = ConversationProvider.__new__(ConversationProvider)
    updates = []
    session = {'session_id':'session', 'worker_id':worker['worker_id'], 'owner_id':'owner', 'history_count':4, 'workspace_dir':str(tmp_path)}
    provider.store = SimpleNamespace(get_worker=lambda *_, **kw:worker,
        list_nonterminal_runs_for_worker=lambda _:['unfinished'] if defect=='unfinished_predecessor' else [],
        update_worker=lambda *_, **kw:updates.append(kw), update_provider_session_history=lambda *_, **kw:updates.append(kw))
    def unavailable(*_): raise OSError('synthetic snapshot I/O race')
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_context_replay=runtime.provider_context_replay,
        freeze_provider_context_replay=unavailable))
    provider._native_bundle = lambda *a:{'run_mode':'conversation'}
    payload = SimpleNamespace(metadata=SimpleNamespace(message_id='new-turn', idempotency_key='idempotency'))
    model = SimpleNamespace(harness_profile='direct-xai', native_model='grok-4.7')
    result, epoch = provider._advance_native_context_epoch_for_pressure(session, payload, model, 'high', {'native_context_epoch':'old-epoch'})
    assert result == session and epoch == 'old-epoch' and not updates


def test_pressure_freezes_existing_proof_without_reading_or_parsing_the_live_journal_again(tmp_path):
    runtime, worker, request = setup_history(tmp_path)
    proof = runtime.provider_context_replay(worker)
    Path(request['history_path']).write_text('a predecessor advanced the live journal')
    frozen = runtime.freeze_provider_context_replay(worker, proof)
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['env']['GLASSHIVE_PROVIDER_SESSION_EPOCH']='new-epoch'
    bundle['direct_context_replay']={**frozen, 'target_epoch':'new-epoch'}
    worker['bootstrap_bundle_json']=json.dumps(bundle)
    next_request=json.loads(runtime._command_stdin_text(worker,'New source',runtime.prepare_worker_workspace(worker)))
    assert read_context_replay(next_request['context_replay'])[1] == ['call-1']
