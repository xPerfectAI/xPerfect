from __future__ import annotations

import json
from threading import Event, Lock, Semaphore, Thread

import pytest

from workers_projects_runtime.grok_control import ControlMailbox, submit_control


class Session:
    session_id = 'native-exact'
    def __init__(self):
        self.cancelled = False
        self.messages = []
    def cancel(self):
        self.cancelled = True
    def interject(self, text, message_id):
        self.messages.append((text, message_id))
        return {'status':'queued','message_id':message_id}


def control(action, **rest):
    return dict(run_id='run-1', attempt_id='attempt-1', session_id='native-exact', action=action, **rest)


def test_mailbox_rejects_stale_attempt_without_native_effect(tmp_path):
    session=Session()
    mailbox=ControlMailbox(tmp_path,run_id='run-1',attempt_id='attempt-1',session=session,event=lambda _:None)
    mailbox.start()
    try:
        result=submit_control(tmp_path,{**control('cancel'),'attempt_id':'old'})
        assert result['status']=='rejected'
        assert not session.cancelled
        assert submit_control(tmp_path,control('cancel'))['status']=='cancel_requested'
        assert session.cancelled
    finally:
        mailbox.close()


def test_mailbox_reports_native_interject_as_queued_only(tmp_path):
    session=Session()
    mailbox=ControlMailbox(tmp_path,run_id='run-1',attempt_id='attempt-1',session=session,event=lambda _:None)
    mailbox.start()
    try:
        result=submit_control(tmp_path,control('interject',text='change'))
        assert result['status']=='queued'
        assert session.messages==[('change',result['message_id'])]
    finally:
        mailbox.close()


def test_permission_roundtrip_and_expiry(tmp_path):
    events=[];request_seen=Event();result={}
    def emit(event):
        events.append(event)
        if event['type']=='grok.permission.requested':request_seen.set()
    mailbox=ControlMailbox(tmp_path,run_id='run-1',attempt_id='attempt-1',session=Session(),event=emit,permission_timeout=1)
    mailbox.start()
    thread=Thread(target=lambda:result.update(option=mailbox.permission({'sessionId':'native-exact','options':[{'optionId':'allow','kind':'allow_once'}]})))
    thread.start()
    try:
        assert request_seen.wait(1)
        request_id=events[0]['request_id']
        reply=submit_control(tmp_path,control('permission',request_id=request_id,option_id='allow'))
        assert reply['status']=='permission_submitted'
        thread.join(1)
        assert result['option']=='allow'
        assert submit_control(tmp_path,control('permission',request_id=request_id,option_id='allow'))['status']=='rejected'
    finally:
        mailbox.close()
        thread.join(2)


def test_run_bound_coordinator_tool_uses_one_turn_permission_only(tmp_path):
    events = []
    name = 'xperfect-coordinator__coordinator_accept_goals'
    mailbox = ControlMailbox(
        tmp_path, run_id='run-1', attempt_id='attempt-1', session=Session(),
        event=events.append, permission_timeout=.01, auto_allow_tools={name},
    )
    params = {
        'sessionId': 'native-exact',
        'toolCall': {
            'title': name,
            'rawInput': {'variant': 'UseTool', 'tool_name': name, 'tool_input': {}},
            '_meta': {'x.ai/tool': {'name': 'use_tool', 'kind': 'use_tool'}},
        },
        'options': [
            {'optionId': 'always', 'kind': 'allow_always'},
            {'optionId': 'once', 'kind': 'allow_once'},
            {'optionId': 'reject', 'kind': 'reject_once'},
        ],
    }
    assert mailbox.permission(params) == 'once'
    assert events == [{'type': 'grok.permission.auto_granted',
                       'session_id': 'native-exact', 'tool_name': name}]
    assert not list(tmp_path.glob('*.pending'))
    assert mailbox.permission({**params, 'toolCall': {
        **params['toolCall'], 'rawInput': {**params['toolCall']['rawInput'],
                                          'tool_name': 'private-server__dangerous'}}}) is None
    assert any(event['type'] == 'grok.permission.requested' for event in events)
    mailbox.cancel_turn()
    assert mailbox.permission(params) is None


def test_oversized_control_is_rejected_before_persistence(tmp_path):
    with pytest.raises(ValueError,match='exceeds'):
        submit_control(tmp_path,control('interject',text='x'*(256*1024)))
    assert not list(tmp_path.iterdir())


def _awaiting_request(tmp_path, timeout):
    events=[];seen=Event();result={};session=Session()
    def emit(event):
        events.append(event)
        if event['type']=='grok.permission.requested':seen.set()
    mailbox=ControlMailbox(tmp_path,run_id='run-1',attempt_id='attempt-1',session=session,event=emit,permission_timeout=timeout)
    mailbox.start()
    thread=Thread(target=lambda:result.update(option=mailbox.permission({'sessionId':'native-exact','options':[{'optionId':'allow','kind':'allow_once'}]})))
    thread.start()
    assert seen.wait(1)
    return mailbox,session,thread,events,result


def test_unanswered_request_expires_truthfully_and_rejects_a_late_answer(tmp_path):
    mailbox,session,thread,events,result=_awaiting_request(tmp_path,.3)
    try:
        request_id=events[0]['request_id']
        thread.join(2)
        assert result['option'] is None
        assert (events[-1]['outcome'],events[-1]['reason'])==('cancelled','expired')
        assert mailbox.unanswered==['expired']
        assert not list(tmp_path.glob('*.pending'))
        assert submit_control(tmp_path,control('permission',request_id=request_id,option_id='allow'))['status']=='rejected'
        assert not session.cancelled
    finally:
        mailbox.close();thread.join(2)


@pytest.mark.parametrize('action,reason',[('permission','dismissed'),('cancel','turn_cancelled')])
def test_owner_cancellation_resolves_the_pending_request_at_once(tmp_path,action,reason):
    # ACP requires the client to answer pending requests after session/cancel.
    mailbox,session,thread,events,result=_awaiting_request(tmp_path,30)
    try:
        extra={'request_id':events[0]['request_id']} if action=='permission' else {}
        reply=submit_control(tmp_path,control(action,**extra))
        assert reply['status']==('permission_submitted' if action=='permission' else 'cancel_requested')
        thread.join(2)
        assert not thread.is_alive() and result['option'] is None
        assert (events[-1]['outcome'],events[-1]['reason'])==('cancelled',reason)
        assert mailbox.unanswered==[reason]
        assert session.cancelled is (action=='cancel')
    finally:
        mailbox.close();thread.join(2)


# O03.b correction: cancellation fence and typed settlement (independent review F2/F3).
from types import SimpleNamespace
from workers_projects_runtime.grok_acp import AcpClient, AcpError, GrokSession
from workers_projects_runtime.grok_acp_runner import _stopped_turn


class _NativeSession(Session):
    def __init__(self):
        super().__init__()
        self.cancel_requested = False

    def cancel(self):
        super().cancel()
        self.cancel_requested = True


def _options(label):
    return {'sessionId': 'native-exact', 'toolCall': {'title': label},
            'options': [{'optionId': 'allow', 'kind': 'allow_once'},
                        {'optionId': 'reject', 'kind': 'reject_once'}]}


def test_cancel_fences_a_request_received_before_it_registered(tmp_path):
    events = []
    entered, release, sent = Event(), Event(), Event()
    session = _NativeSession()
    mailbox = ControlMailbox(tmp_path / 'mailbox', run_id='run-1', attempt_id='attempt-1',
                             session=session, event=events.append, permission_timeout=30)
    mailbox.start()
    wire = []

    def permission(params):
        entered.set()
        assert release.wait(3)
        return mailbox.permission(params)

    transport = SimpleNamespace(permission=permission, interaction=None, _reverse_slots=Semaphore(4),
                                permission_timeout=35, _send=lambda message: (wire.append(message), sent.set()))
    try:
        # Real reverse dispatch; the callback is scheduled only after Stop.
        AcpClient._reverse(transport, {'jsonrpc': '2.0', 'id': 'native-request',
                                      'method': 'session/request_permission', 'params': _options('late')})
        assert entered.wait(1)
        assert submit_control(mailbox.root, control('cancel'))['status'] == 'cancel_requested'
        release.set()
        assert sent.wait(2), 'the delayed request must be answered at once'
        assert wire == [{'jsonrpc': '2.0', 'id': 'native-request',
                         'result': {'outcome': {'outcome': 'cancelled'}}}]
        assert not any(event['type'] == 'grok.permission.requested' for event in events)
        assert not list(mailbox.root.glob('*.pending'))
        assert events[-1]['outcome'] == 'cancelled' and events[-1]['reason'] == 'turn_cancelled'
    finally:
        release.set()
        mailbox.close()


def test_registered_permission_cannot_allow_while_cancel_notification_is_in_flight(tmp_path):
    events, wire = [], []
    published, cancel_written, resume_cancel, response_sent = Event(), Event(), Event(), Event()

    def emit(event):
        events.append(event)
        if event['type'] == 'grok.permission.requested':
            published.set()

    class Pipe:
        def write(self, encoded):
            message = json.loads(encoded)
            wire.append(message)
            if message.get('id') == 'permission':
                response_sent.set()

        def flush(self):
            pass

    transport = SimpleNamespace(_reverse_slots=Semaphore(4), permission_timeout=35,
                                process=SimpleNamespace(stdin=Pipe()), max_frame_bytes=1024 * 1024,
                                _write_lock=Lock())
    transport._send = lambda message: AcpClient._send(transport, message)

    def notify(method, params):
        AcpClient.notify(transport, method, params)
        cancel_written.set()
        assert resume_cancel.wait(3)

    transport.notify = notify
    session = SimpleNamespace(session_id='native-exact', cancel_requested=False, client=transport)
    session.cancel = lambda: GrokSession.cancel(session)
    mailbox = ControlMailbox(tmp_path, run_id='run-1', attempt_id='attempt-1',
                             session=session, event=emit, permission_timeout=30)
    transport.permission, transport.interaction = mailbox.permission, mailbox.interaction
    mailbox.start()
    cancellation = Thread(target=mailbox.cancel_turn)
    try:
        AcpClient._reverse(transport, {'jsonrpc': '2.0', 'id': 'permission',
                                      'method': 'session/request_permission',
                                      'params': _options('Pending at signal')})
        assert published.wait(1)
        request_id = events[0]['request_id']
        cancellation.start()
        assert cancel_written.wait(1) and session.cancel_requested and mailbox._turn_cancelled
        with mailbox._lock:
            pending = mailbox._permissions[request_id]
        mailbox._resolve_unanswered([pending], 'expired')
        assert pending['reason'] == 'turn_cancelled'
        answer = submit_control(tmp_path, control('permission', request_id=request_id, option_id='allow'))
        assert answer['status'] == 'rejected'
        assert not response_sent.is_set(), 'ACP response follows the cancel notification'
        resume_cancel.set()
        cancellation.join(1)
        assert not cancellation.is_alive() and response_sent.wait(1)
        assert wire[0]['method'] == 'session/cancel'
        assert wire[1]['result'] == {'outcome': {'outcome': 'cancelled'}}
        assert events[-1]['outcome'] == 'cancelled' and events[-1]['reason'] == 'turn_cancelled'
        assert mailbox.unanswered == ['turn_cancelled']
        assert not list(tmp_path.glob('*.pending'))
        assert submit_control(tmp_path, control('permission', request_id=request_id, option_id='allow'))['status'] == 'rejected'
    finally:
        resume_cancel.set()
        cancellation.join(2)
        mailbox.close()


def test_a_decline_survives_a_later_concurrent_allow(tmp_path):
    events, result = [], {}
    seen = {'denied': Event(), 'allowed': Event()}
    session = _NativeSession()

    def emit(event):
        events.append(event)
        if event['type'] == 'grok.permission.requested':
            seen[event['request']['toolCall']['title']].set()

    mailbox = ControlMailbox(tmp_path / 'mailbox', run_id='run-1', attempt_id='attempt-1',
                             session=session, event=emit, permission_timeout=30)
    mailbox.start()
    threads = {label: Thread(target=lambda label=label: result.update({label: mailbox.permission(_options(label))}))
               for label in seen}
    try:
        for thread in threads.values():
            thread.start()
        for event in seen.values():
            assert event.wait(1)
        ids = {event['request']['toolCall']['title']: event['request_id'] for event in events
               if event['type'] == 'grok.permission.requested'}
        submit_control(mailbox.root, control('permission', request_id=ids['denied'], option_id='reject'))
        threads['denied'].join(1)
        submit_control(mailbox.root, control('permission', request_id=ids['allowed'], option_id='allow'))
        threads['allowed'].join(1)
        assert result == {'denied': 'reject', 'allowed': 'allow'}
        classified = _stopped_turn(AcpError('native cancelled', stop_reason='cancelled'), session, mailbox)
        assert classified[0] == 'native_input_declined'
    finally:
        mailbox.close()
        for thread in threads.values():
            thread.join(2)


@pytest.mark.parametrize('response,failure_class', [
    ({'outcome': 'decline'}, 'native_input_declined'),
    ({'outcome': 'cancel'}, 'native_input_cancelled'),
    ({'outcome': 'accept', 'content': {}}, None),
])
def test_declared_elicitation_outcomes_are_typed(tmp_path, response, failure_class):
    events, result = [], {}
    published = Event()
    session = _NativeSession()

    def emit(event):
        events.append(event)
        if event['type'] == 'grok.permission.requested':
            published.set()

    mailbox = ControlMailbox(tmp_path / 'mailbox', run_id='run-1', attempt_id='attempt-1',
                             session=session, event=emit, permission_timeout=30)
    mailbox.start()
    thread = Thread(target=lambda: result.update(answer=mailbox.interaction(
        'x.ai/mcp/elicit', {'sessionId': 'native-exact', 'mode': 'form',
                            'requestedSchema': {'type': 'object', 'properties': {}}})))
    thread.start()
    try:
        assert published.wait(1)
        submit_control(mailbox.root, control('permission', request_id=events[0]['request_id'], response=response))
        thread.join(1)
        assert result['answer'] == response
        classified = _stopped_turn(AcpError('native cancelled', stop_reason='cancelled'), session, mailbox)
        assert classified[0] == failure_class
    finally:
        mailbox.close()
        thread.join(2)
