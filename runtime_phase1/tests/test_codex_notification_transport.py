"""Native notification media does not become a second conversation input."""
import io
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from workers_projects_runtime import native_input as owner, codex_native_warm as warm
from workers_projects_runtime.conversation_provider import _codex_async_question_content, _native_tool_evidence


class BoundedStream(io.StringIO):
    def readline(self, size=-1):
        assert size == owner.CODEX_RPC_MAX_EVENT_BYTES + 1
        return super().readline(size)


def read_frames(data, *, stream=None):
    rpc = object.__new__(owner.CodexRpc)
    rpc.process = SimpleNamespace(stdout=stream or BoundedStream(data), stdin=io.StringIO())
    rpc.events = queue.Queue()
    rpc.lock = threading.Lock()
    waiter = queue.Queue()
    rpc.waiters = {'waiting': waiter}
    rpc.closed = False
    rpc.closed_reason = 'native_control_closed'
    rpc.read_failure = None
    rpc._read()
    events = []
    while not rpc.events.empty():
        events.append(rpc.events.get_nowait())
    return rpc, events, waiter


@pytest.mark.parametrize('resume', [False, True])
def test_native_media_copy_setting_preserves_original_input_and_cold_warm_authority(tmp_path, resume):
    profile = {'features': {'plugins': True}, 'developer_instructions': 'Exact authority'}
    command = ['codex', 'exec', '--profile', 'synthetic', *(['resume'] if resume else []),
               '-m', 'selected-model', '-c', 'model_reasoning_effort="low"',
               '-c', 'approval_policy="never"', '--image', str(tmp_path / 'first.png'),
               '--image', str(tmp_path / 'second.png'), *(['synthetic-thread'] if resume else []), '-']
    launch = owner.codex_app_launch(command, native_profile=profile)
    assert launch['images'] == [{'type': 'localImage', 'path': str(tmp_path / name)}
                                for name in ('first.png', 'second.png')]
    assert launch['thread']['model'] == 'selected-model'
    assert launch['turn']['effort'] == 'low'
    assert launch['thread']['config'] == {**profile,
        'model_reasoning_effort': 'low', 'approval_policy': 'never',
        'features': {'plugins': True, 'omit_app_server_notification_media': True}}
    assert profile == {'features': {'plugins': True}, 'developer_instructions': 'Exact authority'}
    assert launch['command'][-2:] == ['--enable', 'omit_app_server_notification_media']
    overrides = warm.command_config(launch['command'])
    _, projected = warm.project_config('[features]\nplugins=true\n', {}, overrides)
    assert projected['features']['omit_app_server_notification_media'] is True
    if not resume:
        assert launch['thread']['experimentalRawEvents'] is True


def test_native_text_notification_and_completion_survive_large_original_image_input():
    # Native omission changes its notification copy, not the already-journaled input.
    original = {'role': 'user', 'type': 'message', 'content': [
        {'type': 'input_text', 'text': 'Exact conversation text'},
        *[{'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'a' * 700_000}
          for _ in range(18)]]}
    before = json.dumps(original)
    notification = {'method': 'rawResponseItem/completed', 'params': {
        'threadId': 'session-a', 'turnId': 'turn-a',
        'item': {**original, 'content': original['content'][:1]}}}
    terminal = {'method': 'turn/completed', 'params': {'threadId': 'session-a',
        'turn': {'id': 'turn-a', 'status': 'completed'}}}
    rpc, events, _ = read_frames(json.dumps(notification) + '\n' + json.dumps(terminal) + '\n')
    assert len(before.encode()) > owner.CODEX_RPC_MAX_EVENT_BYTES
    assert json.dumps(original) == before
    assert events == [notification, terminal, None]
    assert rpc.read_failure is None


@pytest.mark.parametrize('method', ['rawResponseItem/completed', 'item/rawResponseItem/completed'])
def test_native_raw_tool_pairs_keep_evidence_and_async_question_acceptance(method):
    arguments = {'questions': [{'title': 'Choose a time?', 'options': ['Morning', 'Evening']}]}
    items = [
        {'type': 'function_call', 'name': 'request_user_input_async', 'call_id': 'question-a',
         'arguments': json.dumps(arguments)},
        {'type': 'function_call_output', 'call_id': 'question-a',
         'output': json.dumps({'accepted': True})}]
    events = [owner.codex_event({'method': method, 'params': {
        'threadId': 'session-a', 'turnId': 'turn-a', 'item': item}}, 'session-a', 'turn-a')
        for item in items]
    assert [event['payload'] for event in events] == items
    log = '\n'.join(json.dumps(event) for event in events)
    assert _native_tool_evidence('codex-cli', log)['results'][0]['name'] == 'request_user_input_async'
    assert 'Choose a time?' in _codex_async_question_content(log, 'Useful answer.')


@pytest.mark.parametrize('data,code', [
    ('{"secret":"' + 'private' * 50 + '"}\n', 'native_event_oversized'),
    ('{"private":"value",\n', 'native_event_invalid'),
    ('["private"]\n', 'native_event_invalid'),
    ('{"value":"' + '🌲' * 32 + '"}\n', 'native_event_oversized')])
def test_reader_bound_exposes_safe_specific_failure_and_wakes_control_waiters(monkeypatch, data, code):
    monkeypatch.setattr(owner, 'CODEX_RPC_MAX_EVENT_BYTES', 128)
    rpc, events, waiter = read_frames(data)
    assert events == [None] and waiter.get_nowait() is None
    assert rpc.closed_reason == code and rpc.read_failure['code'] == code
    assert 'private' not in json.dumps(rpc.read_failure)
    if code == 'native_event_oversized':
        assert rpc.read_failure['limit_bytes'] == 128
        assert rpc.read_failure['observed_bytes_at_least'] > 128
    with pytest.raises(owner.NativeInputError, match=code):
        rpc.send({'method': 'turn/steer'})
    with pytest.raises(owner.NativeInputError, match=code):
        rpc.call('turn/steer', {})


def test_genuine_eof_and_rpc_response_keep_original_meaning():
    rpc, events, waiter = read_frames('{"id":"waiting","result":{"ok":true}}\n')
    assert waiter.get_nowait() == {'id': 'waiting', 'result': {'ok': True}}
    assert waiter.get_nowait() is None
    assert events == [None] and rpc.closed_reason == 'native_control_closed'
    assert rpc.read_failure is None


def test_io_failure_does_not_report_provider_or_terminal_success():
    class FailingStream:
        def readline(self, _size):
            raise OSError('private host detail')
    rpc, events, _ = read_frames('', stream=FailingStream())
    assert events == [None]
    assert rpc.read_failure == {'code': 'native_event_read_failed'}


def test_relay_reports_reader_failure_with_safe_transport_metadata(tmp_path, monkeypatch, capsys):
    from workers_projects_runtime import grok_control
    failure = {'code': 'native_event_oversized', 'limit_bytes': 128,
               'observed_bytes_at_least': 129}
    class Rpc:
        closed_reason = failure['code']
        read_failure = failure
        events = queue.Queue()
        def call(self, method, _params):
            return {'thread': {'id': 'session-a'}} if method == 'thread/start' else {'turn': {'id': 'turn-a'}}
        def close(self):
            self.did_close = True
    class Mailbox:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
        def close(self): pass
    monkeypatch.setattr(grok_control, 'ControlMailbox', Mailbox)
    rpc = Rpc()
    rpc.events.put(None)
    context_path = tmp_path / 'native-input-context.json'
    owner.publish(context_path, {'runId': 'run-a', 'attemptId': 'attempt-a',
        'launch': {'thread': {}, 'turn': {}, 'images': [], 'resumed': False}})
    assert owner.codex_relay(context_path, [], initialized_rpc=rpc, instruction='Exact input') == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    diagnostic = next(event for event in events if event.get('method') == 'stdout/read')
    assert {key: diagnostic[key] for key in failure} == failure
    assert diagnostic['phase'] == 'failed'
    assert events[-1] == {'type': 'turn.failed', 'error': {'message': 'native_event_oversized'}}
    assert owner.read_object(context_path)['nativeSessionActive'] is False
    assert rpc.did_close is True
