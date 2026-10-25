from __future__ import annotations
import json
import hashlib
from types import SimpleNamespace
import pytest
from workers_projects_runtime.conversation_provider import ConversationProvider, CONVERSATION_TOOL_RESULT_MAX_BYTES
import workers_projects_runtime.conversation_provider as module
_native_tool_evidence = getattr(module, "_native_tool_evidence", None)


def log(*items):
    return '\n'.join(json.dumps(item) for item in items)


def completed(item):
    return {'type': 'item.completed', 'item': item}


def fixture_log():
    return log(
        completed({'id': 'read', 'type': 'command_execution', 'command': 'cat docs/setup.md',
                   'aggregated_output': 'Prerequisites, install and smoke test are documented.', 'status': 'completed', 'exit_code': 0}),
        completed({'id': 'audio', 'type': 'mcp_tool_call', 'server': 'authorized-broker', 'tool': 'transcribe_audio',
                   'arguments': {'file_id': 'owned-file'}, 'result': {'transcript': 'Review the contributor guide. Keep files unchanged.'}, 'status': 'completed'}),
        completed({'id': 'think', 'type': 'reasoning', 'text': 'Never export private reasoning.'}),
    )


def test_canonical_response_retains_current_run_tool_results():
    session = {'worker_id': 'worker', 'conversation_id': 'conversation'}
    worker = {'worker_id': 'worker', 'owner_id': 'owner'}
    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = SimpleNamespace(get_provider_session_by_id=lambda _: session, get_worker=lambda _: worker)
    calls = []
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda w, r: (calls.append((w, r)) or ('codex-cli', fixture_log()))))
    provider._conversation_output = lambda *_, **_kwargs: 'The review is ready.'
    provider._native_usage_snapshots = lambda *_: (None, None, '')
    request = {'request_id': 'request', 'session_id': 'session', 'run_id': 'run', 'owner_id': 'owner',
               'message_id': 'answer', 'native_invocation_id': 'invocation'}
    response = provider._build_canonical_response(request, {'run_id': 'run'}, {'model': 'configured', 'prompt_tokens': 10})
    evidence = response['glasshive']['tool_evidence']
    assert evidence['owner_id'] == 'owner' and evidence['message_id'] == 'answer'
    assert evidence['invocation_id'] == 'invocation' and evidence['run_id'] == 'run'
    assert calls == [(worker, 'run')]
    assert evidence['results'][0]['output']['text'].startswith('Prerequisites')
    assert 'Keep files unchanged' in evidence['results'][1]['output']['text']
    assert 'private reasoning' not in json.dumps(response)
    assert 'tool_evidence' not in provider._build_canonical_response(request, {'run_id': 'foreign'}, {'model': 'configured'})['glasshive']
    assert calls == [(worker, 'run')]
    assert 'tool_evidence' not in provider._build_canonical_response({**request, 'native_invocation_id': ''}, {'run_id': 'run'}, {'model': 'configured'})['glasshive']
    worker['owner_id'] = 'other'
    assert 'tool_evidence' not in provider._build_canonical_response(request, {'run_id': 'run'}, {'model': 'configured'})['glasshive']


def test_native_output_clipping_and_log_omission_are_explicit():
    result = _native_tool_evidence('codex-cli', log(
        {'type': 'glasshive.log_compacted', 'excluded_prefix_bytes': 8000},
        completed({'id': 'large', 'type': 'command_execution', 'command': 'read guide',
                   'aggregated_output': 'é' * CONVERSATION_TOOL_RESULT_MAX_BYTES, 'status': 'completed', 'exit_code': 0})))
    value = result['results'][0]['output']
    assert len(value['text'].encode()) <= CONVERSATION_TOOL_RESULT_MAX_BYTES
    assert value['bytes'] == len(value['text'].encode()) + value['omitted_bytes']
    assert value['omitted_bytes'] > 0 and result['excluded_log_prefix_bytes'] == 8000


def test_native_failure_and_duplicate_receipt_preserve_one_terminal_result():
    event = completed({'id': 'failed', 'type': 'mcp_tool_call', 'tool': 'read', 'arguments': {},
                       'result': {'isError': True, 'content': [{'type': 'text', 'text': 'Access denied'}]}, 'status': 'completed'})
    result = _native_tool_evidence('codex-cli', log(event, event))['results']
    assert len(result) == 1 and result[0]['status'] == 'failed'
    assert 'Access denied' in result[0]['output']['text']


def test_claude_only_projects_paired_native_tool_results():
    result = _native_tool_evidence('claude-code', log(
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'read', 'name': 'Read', 'input': {'file_path': 'docs/setup.md'}}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'read', 'content': 'The guide includes a smoke test.'},
                                                {'type': 'tool_result', 'tool_use_id': 'foreign', 'content': 'unpaired'}]}}))
    assert [x['id'] for x in result['results']] == ['read']
    assert result['results'][0]['output']['text'] == 'The guide includes a smoke test.'


def test_adjacent_terminal_native_records_keep_observed_payload_and_failure_detail():
    result = _native_tool_evidence('codex-cli', log(
        completed({'id': 'web', 'type': 'web_search', 'query': 'SDK setup', 'sources': [{'url': 'https://example.test/guide'}]}),
        completed({'id': 'file', 'type': 'file_change', 'status': 'completed', 'changes': [{'path': 'note.md', 'kind': 'add'}]}),
        completed({'id': 'error', 'type': 'mcp_tool_call', 'tool': 'Read', 'error': 'Permission denied'})))['results']
    assert len(result) == 3 and 'example.test/guide' in result[0]['output']['text']
    assert 'note.md' in result[1]['output']['text']
    assert result[2]['status'] == 'failed' and 'Permission denied' in result[2]['output']['text']


def test_aggregate_bound_and_unrepresentable_identity_are_explicit():
    events = [completed({'id': str(i), 'type': 'command_execution', 'command': 'read',
                         'aggregated_output': 'x' * CONVERSATION_TOOL_RESULT_MAX_BYTES, 'exit_code': 0}) for i in range(40)]
    events.append(completed({'id': 'x' * 257, 'type': 'command_execution', 'command': 'read', 'exit_code': 0}))
    result = _native_tool_evidence('codex-cli', log(*events))
    assert len(json.dumps(result['results'], ensure_ascii=False).encode()) <= module.CONVERSATION_REPLAY_MAX_BYTES_DEFAULT
    assert len(result['results']) + result['omitted_results'] == 41


def graph_fixture():
    context = {'main_context_protocol': 'main_context_v1', 'main_context_owner': 'core',
               'main_context_snapshot_sha256': 'a' * 64, 'context_epoch': 'b' * 64,
               'logical_turn_id': 'turn', 'logical_turn_revision': 1,
               'instruction_sha256': 'c' * 64, 'request_authority_sha256': 'd' * 64}
    anchor = {'request_id': 'before', 'tenant_id': 'tenant', 'owner_id': 'owner', 'stream_id': 'stream',
              'message_id': 'answer', 'created_at': '2026-01-01T00:00:00Z', 'session_id': 'first',
              'run_id': 'run-before', 'native_invocation_id': 'native', 'state': 'completed',
              'replay_decision_json': json.dumps(context)}
    after = {**anchor, 'request_id': 'after', 'session_id': 'second', 'run_id': 'run-after',
             'native_invocation_id': '', 'created_at': '2026-01-01T00:00:01Z'}
    sessions = {key: {'worker_id': key, 'agent_id': key, 'conversation_id': 'conversation'}
                for key in ['first', 'second']}
    workers = {key: {'worker_id': key, 'owner_id': 'owner'} for key in sessions}
    runs = {'run-before': {'run_id': 'run-before', 'worker_id': 'first'},
            'run-after': {'run_id': 'run-after', 'worker_id': 'second'}}
    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = SimpleNamespace(get_provider_session_by_id=sessions.get, get_worker=workers.get,
        get_run=runs.get, list_provider_graph_requests=lambda *_: ([anchor, after], 0))
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda _w, run: (
        'codex-cli', log(completed({'id': 'same-local-call-id', 'type': 'mcp_tool_call', 'tool': 'read_record',
                                   'arguments': {}, 'result': {'value': run}, 'status': 'completed'})))))
    return provider, anchor, after, context


@pytest.mark.parametrize("rebound", [False, True])
def test_graph_evidence_keeps_tools_before_and_after_handoff_without_answer_admission(rebound):
    provider, anchor, after, _ = graph_fixture()
    if rebound:
        original = provider.store.get_provider_session_by_id
        provider.store.get_provider_session_by_id = lambda session_id: {
            **original(session_id), "worker_id": "replacement-worker",
        }
    result = provider.graph_tool_evidence(anchor)
    assert [r['run_id'] for r in result['requests']] == ['run-before', 'run-after']
    assert all(r['evidence_available'] for r in result['requests'])
    assert result['requests'][1]['results'][0]['output']['text'] == '{"value":"run-after"}'
    assert after['native_invocation_id'] == ''
    assert result['anchor_invocation_id'] == 'native'
    assert all(r['instruction_sha256'] == 'c' * 64 for r in result['requests'])


def test_graph_evidence_uses_same_owner_run_and_reports_missing_coverage():
    provider, anchor, _, _ = graph_fixture()
    provider.store.get_run = lambda _: {'run_id': 'foreign', 'worker_id': 'second'}
    assert provider.graph_tool_evidence(anchor)['omitted_requests'] == 2
    provider, anchor, _, _ = graph_fixture()
    provider.service.runtime.provider_activity_log = lambda *_: ('codex-cli', '')
    result = provider.graph_tool_evidence(anchor)
    assert not any(r['evidence_available'] for r in result['requests'])
    assert all(r['results'] == [] for r in result['requests'])


def test_graph_journal_query_rejects_other_owner_attempt_source_revision_and_snapshot():
    import sqlite3
    from workers_projects_runtime.store import Store
    provider, anchor, after, context = graph_fixture()
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    columns = list(anchor)
    conn.execute('CREATE TABLE provider_requests (' + ', '.join(f'{key} TEXT' for key in columns) + ')')
    rows = [anchor, after]
    for field in ['owner_id', 'tenant_id', 'message_id', 'stream_id']:
        rows.append({**after, field: 'foreign', 'request_id': field})
    for field in ['logical_turn_id', 'logical_turn_revision', 'main_context_snapshot_sha256', 'context_epoch']:
        rows.append({**after, 'request_id': field,
                     'replay_decision_json': json.dumps({**context, field: 2 if field.endswith('revision') else 'foreign'})})
    rows.append({**after, 'request_id': 'old-attempt', 'created_at': '2025-01-01T00:00:00Z'})
    for row in rows:
        conn.execute('INSERT INTO provider_requests VALUES (' + ','.join('?' for _ in columns) + ')',
                     [row[key] for key in columns])
    store = Store.__new__(Store)
    store._connect = lambda: conn
    actual, omitted = store.list_provider_graph_requests(anchor, context)
    assert [row['request_id'] for row in actual] == ['before', 'after']
    assert omitted == 0
    conn.close()


def event(kind, call_id='call', session='session', **fields):
    return {'type': 'grok.session.update', 'session_id': session,
            'update': {'sessionUpdate': kind, 'toolCallId': call_id, **fields}}


def start():
    return {'type': 'grok.session.started', 'session_id': 'session'}


def invocation(name='use_tool', call_id='call', **arguments):
    return event('tool_call', call_id, _meta={'x.ai/tool': {'name': name}}, rawInput=arguments)


def log(*records):
    return '\n'.join(json.dumps(value) for value in records)


def project(*records):
    return module._native_tool_evidence('grok-build', log(start(), *records))


def typed_mcp(status='ok'):
    return {'type': 'MCP', 'output': {'OkayOutput': json.dumps({'status': status, 'workRef': 'owned-work'})}}


def test_grok_pairs_refinement_and_terminal_without_title_or_thought_content():
    result = project(invocation(tool_name='worker_delegate_once', tool_input={'goal': 'synthetic'}),
        event('agent_thought_chunk', content={'type': 'text', 'text': 'private-thought-marker'}),
        event('tool_call_update', rawInput={'tool_name': 'worker_delegate_once', 'tool_input': {'goal': 'synthetic'}, 'variant': 'UseTool'}),
        event('tool_call_update', status='completed', rawOutput=typed_mcp()))
    assert len(result['results']) == 1 and result['omitted_results'] == 0
    item = result['results'][0]
    assert item['name'] == 'use_tool' and item['status'] == 'completed'
    assert json.loads(item['arguments']['text'])['variant'] == 'UseTool'
    assert json.loads(item['output']['text']) == typed_mcp()
    assert 'private-thought-marker' not in json.dumps(result)


@pytest.mark.parametrize('status', ['blocked', 'cancelled', 'denied', 'error', 'failed', 'rejected'])
def test_grok_completed_mcp_transport_keeps_typed_operation_failure(status):
    result = project(invocation(), event('tool_call_update', status='completed', rawOutput=typed_mcp(status)))
    assert result['results'][0]['status'] == 'failed'
    assert json.loads(json.loads(result['results'][0]['output']['text'])['output']['OkayOutput'])['status'] == status


@pytest.mark.parametrize('output,status', [
    ({'type': 'MCP', 'output': {'ErrorOutput': 'error'}}, 'failed'),
    ({'type': 'Bash', 'exit_code': 1, 'output_for_prompt': 'command failed'}, 'failed'),
    ({'type': 'Bash', 'exit_code': 0, 'timed_out': True}, 'failed'),
    ({'type': 'Bash', 'exit_code': 0, 'timed_out': False}, 'completed'),
    ({'type': 'MCP', 'output': {'OkayOutput': 'ordinary text containing the word failed'}}, 'completed'),
])
def test_grok_typed_result_status_has_no_prose_rule(output, status):
    result = project(invocation(), event('tool_call_update', status='completed', rawOutput=output))
    assert result['results'][0]['status'] == status


@pytest.mark.parametrize('records', [
    [event('tool_call_update', status='completed', rawOutput={})],
    [invocation(), event('tool_call_update', status='completed')],
    [invocation(), event('tool_call_update', status='in_progress', rawOutput={})],
    [event('tool_call', rawInput={}), event('tool_call_update', status='completed', rawOutput={})],
    [invocation(), event('tool_call_update', _meta={'x.ai/tool': {'name': 'foreign'}}, status='completed', rawOutput={})],
])
def test_grok_incomplete_or_conflicting_records_are_omitted(records):
    result = project(*records)
    assert result['results'] == [] and result['omitted_results'] == 1


def test_grok_foreign_session_and_duplicate_terminal_cannot_add_evidence():
    terminal = event('tool_call_update', status='completed', rawOutput={})
    result = project(invocation(), event('tool_call_update', session='foreign', status='completed', rawOutput={'foreign': True}), terminal, terminal)
    assert len(result['results']) == 1 and 'foreign' not in json.dumps(result)
    assert module._native_tool_evidence('grok-build', log(invocation(), terminal))['results'] == []
    result = project(invocation(), terminal, start())
    assert result['results'] == [] and result['omitted_results'] > 0


def test_grok_cancelled_terminal_and_pending_are_not_success():
    result = project(invocation(), event('tool_call_update', status='cancelled', rawOutput={'cancelled': True}))
    assert result['results'][0]['status'] == 'cancelled'
    assert project(invocation())['omitted_results'] == 1


def test_grok_uses_existing_bounds_secret_path_redaction_and_utf8_hashes():
    result = project(invocation(file_path='/Users/synthetic/private.txt', api_key='sk-testsecretmarker'),
        event('tool_call_update', status='completed', rawOutput={'type': 'ReadFile', 'FileContent': {'absolute_path': '/Users/synthetic/private.txt', 'content': 'é' * 16000}}))
    item = result['results'][0]
    assert '/Users/synthetic' not in json.dumps(item) and 'sk-testsecretmarker' not in json.dumps(item)
    for field in ['arguments', 'output']:
        value = item[field]
        assert len(value['text'].encode()) <= module.CONVERSATION_TOOL_RESULT_MAX_BYTES
        assert value['bytes'] == len(value['text'].encode()) + value['omitted_bytes']
        if not value['omitted_bytes']:
            assert hashlib.sha256(value['text'].encode()).hexdigest() == value['sha256']
    assert item['output']['omitted_bytes'] > 0


def test_grok_canonical_carrier_keeps_exact_owner_run_invocation_checks():
    session = {'worker_id': 'worker', 'conversation_id': 'conversation'}
    worker = {'worker_id': 'worker', 'owner_id': 'owner'}
    provider = module.ConversationProvider.__new__(module.ConversationProvider)
    provider.store = SimpleNamespace(get_provider_session_by_id=lambda _: session, get_worker=lambda _: worker)
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda *_: ('grok-build', log(start(), invocation(), event('tool_call_update', status='completed', rawOutput=typed_mcp())))))
    provider._conversation_output = lambda *_, **kwargs: 'Complete.'
    provider._native_usage_snapshot = lambda *_: None
    request = {'request_id': 'request', 'session_id': 'session', 'run_id': 'run', 'owner_id': 'owner', 'message_id': 'answer', 'native_invocation_id': 'invocation'}
    response = provider._build_canonical_response(request, {'run_id': 'run'}, {'model': 'configured'})
    assert response['glasshive']['tool_evidence']['results'][0]['name'] == 'use_tool'
    assert 'tool_evidence' not in provider._build_canonical_response(request, {'run_id': 'foreign'}, {'model': 'configured'})['glasshive']
    worker['owner_id'] = 'foreign'
    assert 'tool_evidence' not in provider._build_canonical_response(request, {'run_id': 'run'}, {'model': 'configured'})['glasshive']


def test_grok_graph_carrier_preserves_handoff_and_exact_source_scope():
    provider, anchor, _, _ = graph_fixture()
    provider.service.runtime.provider_activity_log = lambda *_: ('grok-build', log(start(), invocation(), event('tool_call_update', status='completed', rawOutput=typed_mcp('blocked'))))
    result = provider.graph_tool_evidence(anchor)
    assert len(result['requests']) == 2 and all(r['evidence_available'] for r in result['requests'])
    assert all(r['results'][0]['status'] == 'failed' for r in result['requests'])
    provider.store.get_run = lambda _: {'run_id': 'foreign', 'worker_id': 'first'}
    assert provider.graph_tool_evidence(anchor)['omitted_requests'] == 2


@pytest.mark.parametrize('with_tool', [False, True])
def test_grok_native_epoch_guard_stays_conservative_without_replayed_tool_carrier(with_tool):
    provider = module.ConversationProvider.__new__(module.ConversationProvider)
    session = {'session_id': 'session', 'worker_id': 'worker', 'owner_id': 'owner', 'tenant_id': 'local'}
    decision = {'native_context_epoch': 'epoch', 'instruction_sha256': 'a' * 64, 'advancement_key': 'key'}
    previous = {**session, 'request_id': 'request', 'run_id': 'run', 'state': 'completed', 'replay_decision_json': json.dumps(decision)}
    provider.store = SimpleNamespace(get_provider_request=lambda _: previous,
        list_provider_session_requests_for_native_coverage=lambda *_, **kwargs: ([previous], True),
        get_worker=lambda *_, **kwargs: {'worker_id': 'worker', 'owner_id': 'owner'},
        get_run=lambda _: {'run_id': 'run', 'worker_id': 'worker', 'state': 'completed'})
    records = [start(), event('agent_message_chunk', content={'type': 'text', 'text': 'Complete.'})]
    if with_tool:
        records += [invocation(), event('tool_call_update', status='completed', rawOutput=typed_mcp())]
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda *_: ('grok-build', log(*records))))
    assert provider._native_tool_evidence_coverage_proven(session, {'last_accepted_request_id': 'request', 'native_context_epoch': 'epoch', 'accepted_advancement_keys': ['key']}) is False



def test_grok_aggregate_bounds_and_invalid_name_are_explicit():
    records = []
    for i in range(40):
        records.extend([invocation(call_id=str(i)), event('tool_call_update', call_id=str(i), status='completed', rawOutput='é' * 16000)])
    records.extend([invocation(name='x' * 257, call_id='invalid'), event('tool_call_update', call_id='invalid', status='completed', rawOutput={})])
    result = project(*records)
    assert len(json.dumps(result['results'], ensure_ascii=False).encode()) <= module.CONVERSATION_REPLAY_MAX_BYTES_DEFAULT
    assert len(result['results']) + result['omitted_results'] == 41
