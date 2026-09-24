"""Private exact-attempt mailbox between supervisor controls and native ACP.

This is local IPC inside the existing run, not an API or scheduler. Public
handlers must retain their ordinary owner/worker authorization before calling it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid
from threading import Event, Lock, Thread


def write_private(path, value, *, managed_acl=False):
    encoded = json.dumps(value, separators=(',', ':'))
    if len(encoded.encode()) > 256 * 1024:
        raise ValueError('Grok control message exceeds the configured limit')
    temporary = path.with_suffix('.tmp-' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660 if managed_acl else 0o600)
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(encoded)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_message(path):
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError('Invalid Grok control message')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Invalid Grok control object')
    return value


def _offered_kind(params, option_id):
    for option in params.get('options') or []:
        if isinstance(option, dict) and option.get('optionId') == option_id:
            return str(option.get('kind') or '')
    return ''


# Declared negative interaction outcomes (ACP option kinds and extension
# outcomes); anything else answered is a positive selection.
_DECLINED_OUTCOMES = {'decline'}
_DISMISSED_OUTCOMES = {'cancel', 'cancelled', 'abandoned'}


def _answered_outcome(method, params, selected):
    if method == 'session/request_permission':
        return 'declined' if _offered_kind(params, selected).startswith('reject') else 'selected'
    declared = str(selected.get('outcome') or '') if isinstance(selected, dict) else ''
    if declared in _DECLINED_OUTCOMES:
        return 'declined'
    if declared in _DISMISSED_OUTCOMES:
        return 'dismissed'
    return 'selected'


class ControlMailbox:
    def __init__(self, root, *, run_id, attempt_id, session, event, permission_timeout=60,
                 managed_acl=False, auto_allow_tools=frozenset()):
        self.root = Path(root)
        self.managed_acl = managed_acl
        self.auto_allow_tools = frozenset(auto_allow_tools)
        if managed_acl:
            if self.root.is_symlink() or not self.root.is_dir():
                raise ValueError('Managed Grok control directory must be pre-provisioned')
        else:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
        self.run_id, self.attempt_id = run_id, attempt_id
        self.session, self.event = session, event
        self.permission_timeout = permission_timeout
        self.stopped = Event()
        self._lock = Lock()
        self._permissions = {}
        # Why each request ended without an answer: expired, dismissed,
        # turn_cancelled or stopped. The runner reports the turn from these.
        self.unanswered = []
        # Every typed negative outcome, in order; a later allow never erases one.
        self.negative_outcomes = []
        self.last_outcome = None
        # Monotonic: once the turn is cancelled no request may register again.
        self._turn_cancelled = False
        self._thread = Thread(target=self._loop, daemon=True, name='grok-control-mailbox')

    def start(self):
        self._thread.start()

    def permission(self, params):
        # An exact run-bound xPerfect coordinator grant already authorizes these
        # internal tools. Select only this turn's allow-once option. Every other
        # native tool or MCP server keeps the ordinary owner prompt.
        call = params.get('toolCall') if isinstance(params, dict) else None
        raw = call.get('rawInput') if isinstance(call, dict) else None
        tool_name = raw.get('tool_name') if isinstance(raw, dict) else None
        meta = call.get('_meta') if isinstance(call, dict) else None
        native = meta.get('x.ai/tool') if isinstance(meta, dict) else None
        with self._lock:
            can_allow = not self._turn_cancelled and not self.stopped.is_set()
        if (can_allow and isinstance(params, dict) and params.get('sessionId') == self.session.session_id
                and isinstance(tool_name, str) and tool_name in self.auto_allow_tools
                and isinstance(native, dict) and native.get('name') == 'use_tool'
                and native.get('kind') == 'use_tool'):
            option = next((item.get('optionId') for item in params.get('options') or []
                           if isinstance(item, dict) and item.get('kind') == 'allow_once'
                           and isinstance(item.get('optionId'), str)), None)
            if option:
                self._record('selected')
                self.event({'type': 'grok.permission.auto_granted',
                            'session_id': self.session.session_id, 'tool_name': tool_name})
                return option
        return self.interaction('session/request_permission', params)

    def interaction(self, method, params):
        if params.get('sessionId') != self.session.session_id:
            return None
        request_id = uuid.uuid4().hex
        result = {'resolved':Event(), 'option':None, 'reason':None}
        with self._lock:
            fenced = self._turn_cancelled
            if not fenced:
                self._permissions[request_id] = result
        if fenced:
            # A callback received before Stop but scheduled after it gets the
            # typed cancelled answer and never publishes a pending request.
            self._record('turn_cancelled')
            self.event({'type':'grok.permission.response_submitted', 'session_id':self.session.session_id,
                        'request_id':request_id, 'outcome':'cancelled', 'option_id':None,
                        'reason':'turn_cancelled'})
            return None
        pending_path = self.root / (request_id + '.pending')
        write_private(pending_path, {'request_id':request_id, 'method':method, 'request':params,
                      'expires_at':time.time()+self.permission_timeout}, managed_acl=self.managed_acl)
        self.event({'type':'grok.permission.requested', 'session_id':self.session.session_id,
                    'request_id':request_id, 'method':method, 'request':params})
        deadline = time.monotonic() + self.permission_timeout
        try:
            while not result['resolved'].wait(.1):
                if self.stopped.is_set() or time.monotonic() >= deadline:
                    # Close under the lock so a racing answer is either taken or rejected.
                    self._resolve_unanswered([result], 'stopped' if self.stopped.is_set() else 'expired')
            selected = result['option']
            reason = None if selected else result['reason'] or 'dismissed'
            if reason:
                self.unanswered.append(reason)
            outcome = reason or _answered_outcome(method, params, selected)
            self._record(outcome)
            negative = outcome if outcome != 'selected' else None
            self.event({'type':'grok.permission.response_submitted', 'session_id':self.session.session_id,
                        'request_id':request_id, 'outcome':'selected' if selected else 'cancelled',
                        'option_id':selected if isinstance(selected, str) else None,
                        **({'reason':negative} if negative else {})})
            return selected
        finally:
            pending_path.unlink(missing_ok=True)
            with self._lock:
                self._permissions.pop(request_id, None)

    def _record(self, outcome):
        with self._lock:
            self.last_outcome = outcome
            if outcome != 'selected':
                self.negative_outcomes.append(outcome)

    def terminal_cause(self):
        """The latest typed negative request outcome, if any."""
        with self._lock:
            return self.negative_outcomes[-1] if self.negative_outcomes else None

    def _resolve_unanswered(self, pending, reason):
        with self._lock:
            for result in pending:
                if not result['resolved'].is_set() and result['reason'] is None:
                    result['reason'] = reason
                    result['resolved'].set()

    def cancel_turn(self):
        # ACP: after session/cancel the client answers every pending request as
        # cancelled. Close registered requests under the same lock as the fence,
        # then release their waiting callbacks after the notification is sent.
        with self._lock:
            self._turn_cancelled = True
            pending = [result for result in self._permissions.values() if not result['resolved'].is_set()]
            for result in pending:
                result['reason'] = 'turn_cancelled'
        try:
            self.session.cancel()
        finally:
            with self._lock:
                for result in pending:
                    result['resolved'].set()

    def _handle(self, request):
        if (request.get('run_id') != self.run_id or request.get('attempt_id') != self.attempt_id
                or request.get('session_id') != self.session.session_id):
            raise ValueError('Stale Grok control generation')
        action = request.get('action')
        if action == 'cancel':
            self.cancel_turn()
            return {'status':'cancel_requested'}
        if action == 'interject':
            text = request.get('text')
            if not isinstance(text, str) or not text:
                raise ValueError('Grok interjection text is required')
            return self.session.interject(text, request['message_id'])
        if action == 'permission':
            with self._lock:
                pending = self._permissions.get(request.get('request_id'))
                if self._turn_cancelled or not pending or pending['resolved'].is_set():
                    raise ValueError('Grok permission is absent, expired or already resolved')
                # Offered-option validation remains in AcpClient, never in a prompt.
                pending['option'] = request.get('response', request.get('option_id'))
                pending['resolved'].set()
            return {'status':'permission_submitted'}
        raise ValueError('Unsupported Grok native control')

    def _loop(self):
        while not self.stopped.wait(.05):
            for path in list(self.root.glob('*.request'))[:32]:
                response = path.with_suffix('.response')
                try:
                    if response.exists():
                        continue
                    request = read_message(path)
                    result = self._handle(request)
                except Exception as exc:
                    result = {'status':'rejected', 'error':str(exc)}
                try:
                    write_private(response, result, managed_acl=self.managed_acl)
                finally:
                    path.unlink(missing_ok=True)

    def close(self):
        self.stopped.set()
        self._thread.join(timeout=1)


def submit_control(root, message, *, timeout=5, managed_acl=False):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('Grok native control is not ready for this attempt')
    if len(list(root.glob('*.request'))) >= 32:
        raise ValueError('Grok native control capacity reached')
    message_id = uuid.uuid4().hex
    path = root / (message_id + '.request')
    response = path.with_suffix('.response')
    write_private(path, {**message, 'message_id':message_id}, managed_acl=managed_acl)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response.is_file():
            result = read_message(response)
            response.unlink(missing_ok=True)
            return result
        time.sleep(.05)
    # Retain an unacknowledged request; do not label it rejected or resend it.
    return {'status':'pending', 'message_id':message_id}
