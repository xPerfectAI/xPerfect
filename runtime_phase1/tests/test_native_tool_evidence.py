from __future__ import annotations
import json
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
    provider._conversation_output = lambda *_: 'The review is ready.'
    provider._native_usage_snapshot = lambda *_: None
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
    assert len(json.dumps(result['results']).encode()) <= module.CONVERSATION_REPLAY_MAX_BYTES_DEFAULT
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
