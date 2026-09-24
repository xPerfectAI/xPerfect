"""Bounded ACP v1 stdio transport for Grok Build; no model or routing policy.

Wire contracts reviewed against xai-org/grok-build commit
4247f661689354b831191f11eeeac8424993fe3d. Private extensions require an
explicit reviewed artifact flag; ACP initialization does not advertise interject.
"""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import Future, TimeoutError
from threading import Lock, Semaphore, Thread
from typing import Callable


class AcpError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, stop_reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.stop_reason = stop_reason


class AcpClient:
    """One reader, serialized writes, bounded frames and concurrent requests.

    The owning runtime owns process-group lifetime. close() stops this exact
    child only, and confirms exit before returning.
    """
    def __init__(self, process: subprocess.Popen, *, notification=None,
                 permission: Callable | None = None, interaction: Callable | None = None, permission_timeout=60.0,
                 max_frame_bytes=4 * 1024 * 1024):
        self.process = process
        self.notification = notification
        self.permission = permission
        self.interaction = interaction
        self.permission_timeout = permission_timeout
        self.max_frame_bytes = max_frame_bytes
        self._lock = Lock()
        self._write_lock = Lock()
        self._pending: dict[int, Future] = {}
        self._next_id = 0
        self._failure = None
        self._reverse_slots = Semaphore(4)
        self._reader = Thread(target=self._read, daemon=True, name='grok-acp-reader')
        self._reader.start()

    def _send(self, message):
        encoded = (json.dumps(message, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
        if len(encoded) > self.max_frame_bytes:
            raise AcpError('ACP outgoing frame exceeds the configured limit')
        with self._write_lock:
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AcpError('Grok ACP transport closed') from exc

    def request(self, method, params, timeout=30):
        future = Future()
        with self._lock:
            if self._failure:
                raise self._failure
            if len(self._pending) >= 32:
                raise AcpError('Grok ACP request capacity reached')
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = future
        try:
            self._send({'jsonrpc':'2.0','id':request_id,'method':method,'params':params})
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            raise AcpError(f'Grok ACP {method} timed out') from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method, params):
        self._send({'jsonrpc':'2.0','method':method,'params':params})

    def _fail(self, error):
        with self._lock:
            self._failure = error
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    def _reverse(self, message):
        request_id, method = message['id'], message['method'].removeprefix('_')
        cancellations = {
            'session/request_permission': {'outcome':{'outcome':'cancelled'}},
            'x.ai/ask_user_question': {'outcome':'cancelled'},
            'x.ai/mcp/elicit': {'outcome':'cancel'},
            'x.ai/exit_plan_mode': {'outcome':'cancelled'},
        }
        if method not in cancellations:
            self._send({'jsonrpc':'2.0','id':request_id,'error':{
                'code':-32601,'message':'This client does not support this interaction'}})
            return
        params = message.get('params') or {}
        cancelled = cancellations[method]
        callback = self.permission if method == 'session/request_permission' else self.interaction
        if not callback or not self._reverse_slots.acquire(blocking=False):
            self._send({'jsonrpc':'2.0','id':request_id,'result':cancelled})
            return
        # A slow UI callback must never block the ACP reader, including cancellation.
        future = Future()
        def decide():
            try:
                future.set_result(callback(params) if method == 'session/request_permission' else callback(method, params))
            except Exception:
                future.set_result(None)
            finally:
                self._reverse_slots.release()
        def respond():
            result = cancelled
            try:
                selected = future.result(timeout=self.permission_timeout)
                options = params.get('options') or []
                if method == 'session/request_permission' and isinstance(selected, str) and any(
                    isinstance(option, dict) and option.get('optionId') == selected
                    for option in options
                ):
                    result = {'outcome':{'outcome':'selected','optionId':selected}}
                elif isinstance(selected, dict):
                    outcome = selected.get('outcome')
                    if method == 'x.ai/ask_user_question' and outcome == 'accepted' and isinstance(selected.get('answers'), dict):
                        answers = selected['answers']
                        if all(isinstance(key, str) and isinstance(values, list) and all(isinstance(value, str) for value in values) for key, values in answers.items()):
                            result = selected
                    elif method == 'x.ai/mcp/elicit' and outcome in ('accept', 'decline', 'cancel'):
                        result = selected
                    elif method == 'x.ai/exit_plan_mode' and outcome in ('approved', 'cancelled', 'abandoned'):
                        result = selected
            except TimeoutError:
                pass
            try:
                self._send({'jsonrpc':'2.0','id':request_id,'result':result})
            except AcpError:
                pass
        Thread(target=decide, daemon=True).start()
        Thread(target=respond, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(self.max_frame_bytes + 1)
                if not line:
                    raise AcpError('Grok ACP process ended before transport close')
                if len(line) > self.max_frame_bytes or not line.endswith(b'\n'):
                    raise AcpError('Grok ACP frame exceeds the configured limit')
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise AcpError('Malformed Grok ACP JSON frame') from exc
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise AcpError('Malformed Grok ACP envelope')
                if 'method' in message:
                    if 'id' in message:
                        self._reverse(message)
                    elif self.notification:
                        self.notification(message['method'], message.get('params') or {})
                    continue
                with self._lock:
                    future = self._pending.get(message.get('id'))
                    if future is None or future.done():
                        continue  # Timed-out/duplicate response is never replayed.
                    if 'error' in message:
                        error = message['error']
                        if not isinstance(error, dict):
                            raise AcpError('Malformed Grok ACP error')
                        # Provider data may include secrets or machine paths; retain code only.
                        future.set_exception(AcpError('Grok ACP request rejected', code=error.get('code')))
                    elif isinstance(message.get('result'), dict):
                        future.set_result(message['result'])
                    else:
                        future.set_exception(AcpError('Malformed Grok ACP result'))
        except Exception as exc:
            self._fail(exc if isinstance(exc, AcpError) else AcpError('Grok ACP reader failed'))

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._fail(AcpError('Grok ACP transport closed'))
        self._reader.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class GrokSession:
    def __init__(self, client, *, event=None, reviewed_interject=False, max_output_chars=8_000_000):
        self.client = client
        self.event = event
        self.reviewed_interject = reviewed_interject
        self.max_output_chars = max_output_chars
        self.session_id = None
        self.initialization = {}
        self.session_state = {}
        self.configured_model = None
        self._model_changed = False
        self._prompt_lock = Lock()
        self._collecting = False
        self._parts = []
        self._chars = 0
        self._output_error = None
        self.cancel_requested = False
        client.notification = self._update

    def _update(self, method, params):
        if not isinstance(params, dict) or params.get('sessionId') != self.session_id:
            return
        if method not in ('session/update', '_x.ai/session/update'):
            return
        update = params.get('update')
        if not isinstance(update, dict):
            return
        if self.event:
            self.event({'type':'grok.session.update', 'session_id':self.session_id,
                        'method':method, 'update':update, 'meta':params.get('_meta', {})})
        if update.get('sessionUpdate') == 'config_option_update' and self.configured_model:
            current = self._options(update, 'model')
            if current and current.get('currentValue') != self.configured_model:
                self._model_changed = True
                if self._collecting:
                    self.client.notify('session/cancel', {'sessionId': self.session_id})
        # ACP sends progress and the final reply as the same chunk type. A
        # tool call ends the preceding assistant segment; retain its exact
        # events above, but return only the segment after the last call.
        if self._collecting and update.get('sessionUpdate') == 'tool_call':
            self._parts = []
        content = update.get('content')
        if self._collecting and update.get('sessionUpdate') == 'agent_message_chunk' and isinstance(content, dict) and content.get('type') == 'text':
            text = content.get('text')
            if not isinstance(text, str):
                self._output_error = AcpError('Malformed Grok message content')
            elif self._chars + len(text) > self.max_output_chars:
                self._output_error = AcpError('Grok output exceeds the configured limit')
            else:
                self._parts.append(text)
                self._chars += len(text)

    @staticmethod
    def _options(state, config_id):
        return next((option for option in state.get('configOptions', [])
                     if isinstance(option, dict) and option.get('id') == config_id), None)

    def open(self, *, cwd, model, session_id=None, effort=None, mcp_servers=None, auth_method=None):
        self.initialization = self.client.request('initialize', {
            'protocolVersion':1,'clientInfo':{'name':'xperfect','version':'1'},
            'clientCapabilities':{'fs':{'readTextFile':False,'writeTextFile':False},'terminal':False}})
        if self.initialization.get('protocolVersion') != 1:
            raise AcpError('Unsupported Grok ACP protocol version')
        if self.initialization.get('_meta', {}).get('grokShell') is not True:
            raise AcpError('The configured process did not identify as Grok Build')
        auth_method = auth_method or self.initialization.get('_meta', {}).get('defaultAuthMethodId')
        methods = self.initialization.get('authMethods') or []
        if auth_method not in ('xai.api_key', 'cached_token') or not any(
            isinstance(method, dict) and method.get('id') == auth_method for method in methods
        ):
            raise AcpError('Grok authentication is unavailable; connect the selected native account', code=-32000)
        self.client.request('authenticate', {'methodId':auth_method, '_meta':{'headless':True}})
        capabilities = self.initialization.get('agentCapabilities', {}).get('mcpCapabilities', {})
        for server in mcp_servers or []:
            kind = server.get('type', 'stdio')
            if kind not in ('stdio', 'http', 'sse') or (kind != 'stdio' and capabilities.get(kind) is not True):
                raise AcpError('Grok does not advertise the requested MCP transport')
        params = {'cwd':cwd, 'mcpServers':mcp_servers or []}
        if session_id:
            if self.initialization.get('agentCapabilities', {}).get('loadSession') is not True:
                raise AcpError('Grok does not advertise exact session resume')
            params['sessionId'] = session_id
        self.session_id = session_id  # Fence load-history notifications too.
        state = self.client.request('session/load' if session_id else 'session/new', params)
        actual_id = state.get('sessionId') or session_id
        if not isinstance(actual_id, str) or not actual_id or (session_id and actual_id != session_id):
            raise AcpError('Grok native session identity mismatch')
        self.session_id = actual_id
        model_option = self._options(state, 'model') or {}
        actual_model = state.get('models', {}).get('currentModelId') or model_option.get('currentValue')
        if actual_model != model:
            offered = []
            for item in model_option.get('options', []):
                if isinstance(item, dict):
                    offered.extend(item.get('options', [item]))
            if model not in [item.get('value') for item in offered if isinstance(item, dict)]:
                raise AcpError('Grok native model does not match the exact configured model')
            configured = self.client.request('session/set_config_option', {
                'sessionId': self.session_id, 'configId': 'model', 'value': model})
            if (self._options(configured, 'model') or {}).get('currentValue') != model:
                raise AcpError('Grok did not retain the exact configured model')
            state.update(configured)
        if effort:
            option = self._options(state, 'reasoning_effort') or {}
            values = []
            for item in option.get('options', []):
                if isinstance(item, dict):
                    values.extend(item.get('options', [item]))
            if effort not in [item.get('value') for item in values if isinstance(item, dict)]:
                raise AcpError('Requested Grok reasoning effort is unsupported')
            configured = self.client.request('session/set_config_option', {
                'sessionId':self.session_id, 'configId':'reasoning_effort','value':effort})
            if (self._options(configured, 'reasoning_effort') or {}).get('currentValue') != effort:
                raise AcpError('Grok did not retain the configured reasoning effort')
            state.update(configured)
        self.session_state = state
        self.configured_model = model
        self._model_changed = False
        return self.session_id

    def prompt(self, instruction, *, timeout=None):
        if not self.session_id:
            raise AcpError('Grok session is not initialized')
        if self._model_changed:
            raise AcpError('Grok session changed the exact configured model')
        if not self._prompt_lock.acquire(blocking=False):
            raise AcpError('Grok session already has an active prompt')
        try:
            self._parts, self._chars, self._output_error = [], 0, None
            self._collecting = True
            result = self.client.request('session/prompt', {'sessionId':self.session_id,
                'prompt':[{'type':'text','text':instruction}]}, timeout=timeout)
            if self._model_changed:
                raise AcpError('Grok session changed the exact configured model')
            if self._output_error:
                raise self._output_error
            if result.get('stopReason') != 'end_turn':
                raise AcpError('Grok turn ended with stop reason: ' + str(result.get('stopReason', 'missing')),
                               stop_reason=result.get('stopReason'))
            return ''.join(self._parts)
        finally:
            self._collecting = False
            self._prompt_lock.release()

    def cancel(self):
        if self.session_id:
            self.cancel_requested = True
            self.client.notify('session/cancel', {'sessionId':self.session_id})

    def interject(self, text, message_id):
        if not self.reviewed_interject:
            raise AcpError('Grok interject is not verified for the configured artifact')
        if not self._collecting:
            raise AcpError('Grok session has no active prompt to steer')
        result = self.client.request('_x.ai/interject', {'sessionId':self.session_id,
            'text':text,'interjectionId':message_id})
        # Grok's to_ext_response wraps the handler payload in ExtMethodResult,
        # inside JSON-RPC's own result. A successful request alone is not a receipt.
        payload = result.get('result')
        if result.get('error') is not None:
            raise AcpError('Grok rejected the interjection')
        if not isinstance(payload, dict) or payload.get('status') != 'queued':
            raise AcpError('Grok did not acknowledge a queued interjection')
        return {'status':'queued', 'message_id':message_id}
