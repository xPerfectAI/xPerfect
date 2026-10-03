"""Private exact-attempt mailbox between supervisor controls and native ACP.

This is local IPC inside the existing run, not an API or scheduler. Public
handlers must retain their ordinary owner/worker authorization before calling it.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import time
import uuid
from threading import Event, Lock, Thread


def write_private(path, value, *, managed_acl=False, first_writer=False):
    encoded = json.dumps(value, separators=(',', ':'))
    if len(encoded.encode()) > 256 * 1024:
        raise ValueError('Grok control message exceeds the configured limit')
    temporary = path.with_suffix('.tmp-' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660 if managed_acl else 0o600)
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(encoded)
        if first_writer:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if read_message(path) != value:
                    raise ValueError('native_input_conflict')
        else:
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


def control_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def permission_message_id(run_id, attempt_id, request_id, fingerprint):
    return control_digest([run_id, attempt_id, request_id, fingerprint])


def control_receipt(root, message_id, expected):
    """Replay a retained native ACK only for its exact submitted control body."""
    path = Path(root) / (message_id + '.response')
    if not path.is_file():
        return None
    result = read_message(path)
    submitted = result.get('_request')
    if (not isinstance(submitted, dict) or submitted.get('message_id') != message_id
            or result.get('_request_digest') != control_digest(submitted)
            or any(submitted.get(key) != value for key, value in expected.items())):
        raise ValueError('native_input_conflict')
    return {key: value for key, value in result.items() if not key.startswith('_')}


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
                 managed_acl=False, auto_allow_tools=frozenset(), permission_deadline_at=None):
        if permission_deadline_at is not None and (
                isinstance(permission_deadline_at, bool) or not isinstance(permission_deadline_at, (int, float))
                or not math.isfinite(permission_deadline_at) or permission_deadline_at <= 0):
            raise ValueError('Invalid Grok permission deadline')
        self.permission_deadline_at = permission_deadline_at
        self._permission_deadline = None
        if permission_deadline_at is not None:
            permission_timeout = max(0, permission_deadline_at - time.time())
            self._permission_deadline = time.monotonic() + permission_timeout
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
            expired = self._permission_deadline is not None and time.monotonic() >= self._permission_deadline
            if not fenced and not expired:
                self._permissions[request_id] = result
        if fenced or expired:
            # A callback received before Stop but scheduled after it gets the
            # typed cancelled answer and never publishes a pending request.
            reason = 'turn_cancelled' if fenced else 'expired'
            if expired and not fenced:
                self.unanswered.append(reason)
            self._record(reason)
            self.event({'type':'grok.permission.response_submitted', 'session_id':self.session.session_id,
                        'request_id':request_id, 'outcome':'cancelled', 'option_id':None,
                        'reason':reason})
            return None
        pending_path = self.root / (request_id + '.pending')
        write_private(pending_path, {'request_id':request_id, 'method':method, 'request':params,
                      'expires_at':self.permission_deadline_at if self.permission_deadline_at is not None
                                   else time.time()+self.permission_timeout}, managed_acl=self.managed_acl)
        self.event({'type':'grok.permission.requested', 'session_id':self.session.session_id,
                    'request_id':request_id, 'method':method, 'request':params})
        deadline = self._permission_deadline if self._permission_deadline is not None else time.monotonic() + self.permission_timeout
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
            # The resolved observer must see the remaining live question, not this one.
            pending_path.unlink(missing_ok=True)
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
                if (self._turn_cancelled or not pending or pending['resolved'].is_set()
                        or (self._permission_deadline is not None and time.monotonic() >= self._permission_deadline)):
                    raise ValueError('Grok permission is absent, expired or already resolved')
                if request.get('request_fingerprint'):
                    published = read_message(self.root / (request['request_id'] + '.pending'))
                    if (published.get('expires_at', 0) <= time.time()
                            or control_digest(published) != request['request_fingerprint']):
                        raise ValueError('native_input_stale')
                    option = request.get('option_id')
                    if option is not None and not any(
                            isinstance(item, dict) and item.get('optionId') == option
                            for item in published['request'].get('options') or []):
                        raise ValueError('native_input_invalid')
                # Offered-option validation remains in AcpClient, never in a prompt.
                pending['option'] = request.get('response', request.get('option_id'))
                pending['resolved'].set()
            return {'status':'permission_submitted'}
        raise ValueError('Unsupported Grok native control')

    def _loop(self):
        while not self.stopped.wait(.05):
            for path in list(self.root.glob('*.request'))[:32]:
                response = path.with_suffix('.response')
                request = None
                try:
                    if response.exists():
                        existing = read_message(path)
                        if existing.get('request_fingerprint'):
                            control_receipt(self.root, path.stem, existing)
                            path.unlink(missing_ok=True)
                        continue
                    request = read_message(path)
                    result = self._handle(request)
                except Exception as exc:
                    if response.exists():
                        # An earlier exact ACK must never be replaced by a conflicting replay.
                        path.unlink(missing_ok=True)
                        continue
                    result = {'status':'rejected', 'error':str(exc)}
                if isinstance(request, dict) and request.get('request_fingerprint'):
                    result = {**result, '_request': request, '_request_digest': control_digest(request)}
                try:
                    write_private(response, result, managed_acl=self.managed_acl)
                finally:
                    path.unlink(missing_ok=True)

    def close(self):
        self.stopped.set()
        self._thread.join(timeout=1)


def submit_control(root, message, *, timeout=5, managed_acl=False, message_id=None, replay_only=False):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('Grok native control is not ready for this attempt')
    retained = bool(message_id and message.get('request_fingerprint'))
    message_id = message_id or uuid.uuid4().hex
    if not isinstance(message_id, str) or not message_id or not all(c in '0123456789abcdef' for c in message_id):
        raise ValueError('Invalid Grok control message identity')
    path = root / (message_id + '.request')
    response = path.with_suffix('.response')
    body = {**message, 'message_id': message_id}
    if retained:
        receipt = control_receipt(root, message_id, body)
        if receipt is not None:
            return receipt
    if path.is_file():
        if read_message(path) != body:
            raise ValueError('native_input_conflict')
    elif not replay_only:
        if len(list(root.glob('*.request'))) >= 32:
            raise ValueError('Grok native control capacity reached')
        write_private(path, body, managed_acl=managed_acl, first_writer=retained)
    elif not response.is_file():
        raise ValueError('native_input_stale')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response.is_file():
            result = control_receipt(root, message_id, body) if retained else read_message(response)
            if not retained:
                response.unlink(missing_ok=True)
            return result
        time.sleep(.05)
    # Retain an unacknowledged request; do not label it rejected or resend it.
    return {'status':'pending', 'message_id':message_id}
