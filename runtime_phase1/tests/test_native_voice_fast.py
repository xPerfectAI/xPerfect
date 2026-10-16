"""Native call speed uses per-turn options and the existing public envelope contract."""
from __future__ import annotations

import json
from types import SimpleNamespace
import pytest

from workers_projects_runtime import native_input
from workers_projects_runtime.agent_builder_control import messaging_delivery_control
from workers_projects_runtime.conversation_provider import (
    ChatCompletionRequest, ConversationProvider, GLASSHIVE_MODELS, _native_spoken_content,
)
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime


def event(method, *, phase='final_answer', item_id='item-a', text=None, delta=None, thread='thread-a', turn='turn-a'):
    params = {'threadId': thread, 'turnId': turn}
    if method == 'item/agentMessage/delta':
        params.update(itemId=item_id, delta=delta)
    else:
        params['item'] = {'type': 'agentMessage', 'id': item_id, 'phase': phase}
        if text is not None:
            params['item']['text'] = text
    return {'method': method, 'params': params}


def project(events):
    items = {}
    result = [{'type': 'thread.started', 'thread_id': 'thread-a'},
              {'type': 'turn.started', 'thread_id': 'thread-a', 'turn_id': 'turn-a'}]
    for value in events:
        projected = native_input.codex_public_message_event(value, 'thread-a', 'turn-a', items)
        if projected:
            result.append(projected)
    return '\n'.join(map(json.dumps, result))


def envelope(content='Useful first words.', voice='eligible'):
    return json.dumps({'type': 'assistant_response', 'tool_name': None, 'voice': voice, 'content': content})


def spoken(events):
    return _native_spoken_content('codex-cli', project(events), None, messaging_delivery_control(audio_eligible=True))


def test_native_final_words_stream_before_item_completion_and_reconcile_once():
    body = envelope()
    prefix = body[:-2]
    events = [event('item/started'), event('item/agentMessage/delta', delta=prefix)]
    result = spoken(events)
    assert result['text'] == 'Useful first words.'
    assert result['part_complete'] is False
    events += [event('item/completed', text=body)] * 2
    complete = spoken(events)
    assert complete['text'] == result['text']
    assert complete['completed_parts'] == [{'part_index': 1, 'text_end': len(result['text'])}]


@pytest.mark.parametrize('phase', [None, 'commentary'])
def test_unknown_or_commentary_phase_never_releases_native_delta(phase):
    body = envelope()
    events = [event('item/started', phase=phase), event('item/agentMessage/delta', delta=body)]
    assert spoken(events) is None
    events += [event('item/completed', phase=phase, text=body)]
    result = spoken(events)
    assert (result['text'] if result else None) == ('Useful first words.' if phase is None else None)


@pytest.mark.parametrize('change', ['thread', 'turn', 'item', 'missing_item', 'phase'])
def test_native_delta_requires_exact_declared_item_binding(change):
    events = [event('item/started')]
    delta = event('item/agentMessage/delta', delta=envelope())
    if change == 'thread': delta['params']['threadId'] = 'foreign-thread'
    if change == 'turn': delta['params']['turnId'] = 'foreign-turn'
    if change == 'item': delta['params']['itemId'] = 'foreign-item'
    if change == 'missing_item': delta['params'].pop('itemId')
    if change == 'phase': events = [event('item/started', phase='commentary')]
    assert spoken(events + [delta]) is None


def test_native_completed_text_cannot_replace_streamed_prefix():
    events = [event('item/started'), event('item/agentMessage/delta', delta=envelope('Accepted words.')[:-2]),
              event('item/completed', text=envelope('Changed words.'))]
    with pytest.raises(native_input.NativeInputError, match='native_public_text_mismatch'):
        project(events)


@pytest.mark.parametrize('case', ['skip', 'content_first', 'invalid_voice', 'duplicate_control', 'partial_escape', 'file', 'citation'])
def test_native_stream_reuses_existing_speech_and_private_suffix_guards(case):
    body = envelope()
    if case == 'skip': body = envelope(voice='skip')[:-2]
    if case == 'content_first': body = '{"content":"Useful first words.","type":"assistant_response"'
    if case == 'invalid_voice': body = body.replace('"eligible"', '"invalid"')
    if case == 'duplicate_control': body = body.replace('"content"', '"voice":"eligible","content"')
    if case == 'partial_escape': body = envelope('Useful words. ')[:-2] + '\\uD83D'
    if case == 'file': body = envelope('Useful words. [File](file:///private/synthetic.html)')[:-2]
    if case == 'citation': body = envelope('Useful words. \ue200cite\ue202private:1\ue201')[:-2]
    result = spoken([event('item/started'), event('item/agentMessage/delta', delta=body)])
    if case in ['content_first', 'invalid_voice', 'duplicate_control']: assert result is None
    elif case == 'skip': assert result['delivery_disposition']['audio'] == 'skip' and result['completed_parts'] == []
    elif case == 'file': assert result['text'] == 'Useful words.'
    elif case == 'partial_escape': assert result is None or '\ud83d' not in result['text']
    else: assert result is None or 'private:1' not in result['text']


def test_multiple_native_final_items_keep_order_without_fake_grok_identity():
    events = []
    for identity, content in [('item-a', 'First.'), ('item-b', 'Second.')]:
        events += [event('item/started', item_id=identity), event('item/agentMessage/delta', item_id=identity, delta=envelope(content)),
                   event('item/completed', item_id=identity, text=envelope(content))]
    result = spoken(events)
    assert result['text'] == 'First.\n\nSecond.'
    assert len(result['completed_parts']) == 2
    assert 'grok.' not in project(events)


@pytest.mark.parametrize('surface', ['web', 'telegram', 'voice'])
def test_native_call_stream_requires_saved_voice_surface(surface):
    provider = object.__new__(ConversationProvider)
    stdout = project([event('item/started'), event('item/agentMessage/delta', delta=envelope()[:-2])])
    provider.store = SimpleNamespace(
        get_provider_session_by_id=lambda _: {'owner_id': 'owner-a'},
        get_worker=lambda _: {'owner_id': 'owner-a'},
    )
    provider.service = SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda *_: ('codex-cli', stdout)))
    record = {'session_id': 'session-a', 'owner_id': 'owner-a', 'replay_decision_json': json.dumps({'completion_contract_v1': {'version': 1, 'surface': surface}})}
    result = provider._native_spoken_content_snapshot(record, {'worker_id': 'worker-a', 'run_id': 'run-a'}, None, messaging_delivery_control(audio_eligible=True))
    assert bool(result) is (surface == 'voice')


@pytest.mark.parametrize('tier', ['fast', 'priority', 'default', 'ultrafast'])
@pytest.mark.parametrize('resume', [False, True])
def test_native_service_tier_is_only_a_turn_override(tier, resume):
    command = ['codex', 'exec'] + (['resume'] if resume else []) + ['--json', '-c', 'approval_policy="never"', '-c', 'service_tier=' + json.dumps(tier)] + (['thread-a'] if resume else []) + ['-']
    launch = native_input.codex_app_launch(command)
    assert launch['turn']['serviceTierForTurn'] == ('priority' if tier == 'fast' else tier)
    assert 'serviceTier' not in launch['thread']
    assert not any('service_tier' in arg for arg in launch['command'])
    following = native_input.codex_app_launch(['codex', 'exec', '--json', '-c', 'approval_policy="never"', '-'])
    assert 'serviceTierForTurn' not in following['turn']


def test_native_service_tier_command_is_independent_of_global_configuration(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / 'runtime'))
    worker = {'bootstrap_bundle_json': json.dumps({'service_tier': 'fast'})}
    command = []
    runtime._append_codex_service_tier_config(command, worker)
    assert command == ['-c', 'service_tier="priority"']
    unconfigured = []
    runtime._append_codex_service_tier_config(unconfigured, {'bootstrap_bundle_json': '{}'})
    assert unconfigured == []
    with pytest.raises(Exception, match='Invalid native Codex service tier'):
        runtime._append_codex_service_tier_config([], {'bootstrap_bundle_json': json.dumps({'service_tier': 'bad tier'})})


def test_request_tier_projection_and_fallback_are_provider_owned():
    provider = object.__new__(ConversationProvider)
    request = ChatCompletionRequest(model='codex-cli:gpt-6.1-sol', service_tier='fast', messages=[{'role': 'user', 'content': 'Synthetic request.'}], metadata={'surface': 'voice'})
    bundle = provider._native_bundle(request, GLASSHIVE_MODELS[request.model], 'low', graph_control=None)
    assert bundle['service_tier'] == 'priority'
    assert bundle['surface'] == 'voice'
    assert bundle['env']['WPR_CODEX_CLI_REASONING_EFFORT'] == 'low'
    fallback = provider._fallback_bundle({'bootstrap_bundle_json': json.dumps(bundle)}, GLASSHIVE_MODELS['claude-code:opus'], 'high')
    assert 'service_tier' not in fallback
    assert 'WPR_CODEX_CLI_REASONING_EFFORT' not in fallback['env']
    assert fallback['env']['WPR_CLAUDE_CODE_EFFORT'] == 'high'


def test_call_completion_surface_is_durable_and_global_incremental_flag_stays_off():
    provider = object.__new__(ConversationProvider)
    request = ChatCompletionRequest(model='codex-cli:gpt-6.1-sol', messages=[{'role': 'user', 'content': 'Synthetic request.'}], metadata={'surface': 'voice'})
    assert provider._completion_contract(request)['surface'] == 'voice'
    catalog = GLASSHIVE_MODELS[request.model].api_payload(readiness={})
    assert catalog['service_tier_choices'] == ['default', 'priority']
    assert catalog['capabilities']['incremental_text'] is False


@pytest.mark.parametrize('method', ['item/started', 'item/completed'])
def test_native_phase_cannot_change_after_early_final_answer(method):
    events = [event('item/started'), event('item/agentMessage/delta', delta=envelope()[:-2]),
              event(method, phase='commentary', text=envelope())]
    with pytest.raises(native_input.NativeInputError, match='native_public_phase_mismatch'):
        project(events)


@pytest.mark.parametrize('kind', ['absent_session', 'absent_turn', 'foreign_completion', 'foreign_delta'])
def test_public_projection_rechecks_native_generation_receipts(kind):
    stdout = project([event('item/started'), event('item/agentMessage/delta', delta=envelope()[:-2])])
    records = [json.loads(line) for line in stdout.splitlines()]
    if kind == 'absent_session': records = records[1:]
    if kind == 'absent_turn': records = [r for r in records if r['type'] != 'turn.started']
    if kind == 'foreign_delta': records[-1]['turn_id'] = 'foreign-turn'
    if kind == 'foreign_completion':
        records[-1] = {'type': 'codex.message.completed', 'thread_id': 'foreign-thread', 'turn_id': 'turn-a', 'item_id': 'item-a', 'phase': 'final_answer', 'text': envelope()}
    assert _native_spoken_content('codex-cli', '\n'.join(map(json.dumps, records)), None, messaging_delivery_control(audio_eligible=True)) is None


@pytest.mark.parametrize('status,expected', [(429, 'provider_rate_limited'), (401, 'provider_auth_missing'), (503, 'provider_response_failed')])
def test_native_codex_http_status_code_keeps_failure_type(status, expected):
    from workers_projects_runtime.failure_classification import classify_cli_failure
    control = json.dumps({'type': 'turn.failed', 'error': {'message': 'Synthetic provider refusal.',
        'codexErrorInfo': {'responseTooManyFailedAttempts': {'httpStatusCode': status}}}, 'native_status': 'failed'})
    result = classify_cli_failure(stdout=control, stderr='', runtime_name='codex-cli', exit_code=1)
    assert result.failure_class == expected
    assert result.structured is True
    assert result.retry_after_s is None


def test_authored_http_status_code_is_not_provider_failure_authority():
    from workers_projects_runtime.failure_classification import classify_cli_failure
    authored = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'A synthetic example.',
        'httpStatusCode': 429, 'error': {'codexErrorInfo': {'responseTooManyFailedAttempts': {'httpStatusCode': 429}}}}})
    result = classify_cli_failure(stdout=authored, stderr='', runtime_name='codex-cli', exit_code=1)
    assert result.failure_class != 'provider_rate_limited'


def test_native_typed_rate_limit_reaches_existing_http_and_stream_error_consumers():
    from workers_projects_runtime.failure_classification import classify_cli_failure
    from workers_projects_runtime.conversation_provider import _provider_failure_http_status, _provider_failure_error
    control = json.dumps({'type': 'turn.failed', 'error': {'codexErrorInfo': {
        'responseTooManyFailedAttempts': {'httpStatusCode': 429}}}})
    classification = classify_cli_failure(stdout=control, stderr='', runtime_name='codex-cli', exit_code=1)
    run = {'failure_class': classification.failure_class}
    assert _provider_failure_http_status(run) == 429
    assert _provider_failure_error(run) == ('rate_limit_error', 'rate_limit_exceeded')
